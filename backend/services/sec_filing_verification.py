"""Exact, entity-wide fact checks against one official filing XBRL instance.

This is deliberately not an Inline XBRL renderer or a revision-order engine.
Only a uniquely identified instance and an exact duration/unit are eligible.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from backend.services.sec_filing_evidence import CONCEPT_UNITS

XBRLI = "http://www.xbrl.org/2003/instance"
ISO4217 = "http://www.xbrl.org/2003/iso4217"
CIK_SCHEME = "http://www.sec.gov/CIK"
_GAAP_NAMESPACE = re.compile(r"https?://fasb\.org/us-gaap/\d{4}(?:-\d{2}-\d{2})?")
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\.xml")


def archive_base(cik: int, accession: str) -> str:
    if not 1 <= cik <= 9_999_999_999 or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
        raise ValueError("Invalid SEC archive identity")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/"


def instance_filename(name: str) -> bool:
    return bool(_FILE_NAME.fullmatch(name)) and ".." not in name


class _FilingIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.rows: list[list[dict[str, str]]] = []
        self.row: list[dict[str, str]] | None = None
        self.cell: dict[str, str] | None = None
        self.info_kind: str | None = None
        self.info_text = ""
        self.heading: str | None = None
        self.metadata: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = {"text": "", "href": ""}
        elif tag == "a" and self.cell is not None:
            self.cell["href"] = attributes.get("href") or ""
        elif tag == "div" and attributes.get("class") in {"infoHead", "info"}:
            self.info_kind = attributes["class"]
            self.info_text = ""

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self.cell is not None:
            self.cell["text"] += data
        if self.info_kind is not None:
            self.info_text += data

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.cell["text"] = self.cell["text"].strip()
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "div" and self.info_kind is not None:
            if self.info_kind == "infoHead":
                self.heading = self.info_text.strip()
            elif self.heading:
                self.metadata.setdefault(self.heading, []).append(self.info_text.strip())
                self.heading = None
            self.info_kind = None


def filing_document(index_html: bytes, cik: int, claim: dict[str, Any]) -> dict[str, Any]:
    """Select only an archive-listed instance; never guess a document filename."""
    base = archive_base(cik, claim["accession"])
    parser = _FilingIndexParser()
    try:
        parser.feed(index_html.decode("utf-8"))
    except (UnicodeError, ValueError):
        return {"status": "unparseable", "reason": "invalid_filing_index"}
    accessions = set(re.findall(r"SEC Accession No\.\s*(\d{10}-\d{2}-\d{6})", " ".join(parser.text)))
    if accessions != {claim["accession"]}:
        return {"status": "unparseable", "reason": "invalid_filing_index"}
    dates = parser.metadata.get("Filing Date", [])
    forms = {row[3]["text"] for row in parser.rows if len(row) >= 4 and row[3]["text"] in {"10-K", "10-Q", "10-K/A", "10-Q/A"}}
    if len(dates) != 1 or len(forms) != 1:
        return {"status": "ambiguous", "reason": "filing_metadata_ambiguous"}
    try:
        filed = date.fromisoformat(dates[0])
    except ValueError:
        return {"status": "unparseable", "reason": "invalid_filing_index"}
    metadata = {"filing_form": next(iter(forms)), "filing_date": filed}
    if metadata["filing_form"] != claim["form"] or filed != claim["filed_date"]:
        return {**metadata, "status": "mismatched", "reason": "filing_metadata_differs"}
    names = set()
    for row in parser.rows:
        if len(row) < 4:
            continue
        description, document, kind = row[1]["text"].upper(), row[2], row[3]["text"]
        if kind != "EX-101.INS" and not (kind == "XML" and description == "EXTRACTED XBRL INSTANCE DOCUMENT"):
            continue
        href = urlsplit(document["href"])
        expected_path = urlsplit(base).path
        name = href.path.removeprefix(expected_path)
        if (href.scheme not in {"", "https"} or href.netloc not in {"", "www.sec.gov"}
                or href.query or href.fragment or not href.path.startswith(expected_path)
                or not instance_filename(name)):
            return {**metadata, "status": "unsupported", "reason": "unsupported_instance_link"}
        names.add(name)
    if not names:
        return {**metadata, "status": "unsupported", "reason": "instance_not_listed"}
    if len(names) != 1:
        return {**metadata, "status": "ambiguous", "reason": "multiple_instances"}
    name = next(iter(names))
    return {**metadata, "status": "selected", "instance_name": name, "document_url": base + name}


class _InstanceTreeBuilder(ET.TreeBuilder):
    """Keep QName bindings and reject unsafe/oversized trees during parsing."""

    def __init__(self) -> None:
        super().__init__()
        self.bindings: dict[int, dict[str, str]] = {}
        self.stack: list[dict[str, str]] = []
        self.pending: dict[str, str] = {}

    def start_ns(self, prefix: str | None, uri: str) -> None:
        self.pending[prefix or ""] = uri

    def start(self, tag: str, attrs: dict[str, str]) -> ET.Element:
        if len(self.bindings) >= 150_000 or len(self.stack) >= 100:
            raise ValueError("Instance complexity limit")
        namespaces = self.stack[-1] if self.stack else {}
        if self.pending:
            namespaces = {**namespaces, **self.pending}
        self.pending.clear()
        self.stack.append(namespaces)
        element = super().start(tag, attrs)
        self.bindings[id(element)] = namespaces
        return element

    def end(self, tag: str) -> ET.Element:
        element = super().end(tag)
        self.stack.pop()
        return element

    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ValueError("DTD is unsupported")


def verify_instance(content: bytes, cik: int, claim: dict[str, Any]) -> dict[str, Any]:
    """Require an exact undimensioned issuer/concept/unit/duration match."""
    if CONCEPT_UNITS.get(claim["concept"]) != claim["unit"]:
        return {"status": "unsupported", "reason": "unsupported_concept_or_unit"}
    try:
        builder = _InstanceTreeBuilder()
        root = ET.fromstring(content, parser=ET.XMLParser(target=builder))
        if root.tag != f"{{{XBRLI}}}xbrl":
            raise ValueError("Not an XBRL instance")
    except (ET.ParseError, ValueError, LookupError):
        return {"status": "unparseable", "reason": "invalid_xbrl_instance"}

    def qname(element: ET.Element) -> str | None:
        text = (element.text or "").strip()
        prefix, sep, name = text.partition(":")
        namespace = builder.bindings[id(element)].get(prefix if sep else "")
        return f"{{{namespace}}}{name if sep else prefix}" if namespace else None

    contexts: dict[str, tuple[date, date] | None] = {}
    units: dict[str, str | None] = {}
    try:
        for element in root:
            if element.tag not in {f"{{{XBRLI}}}context", f"{{{XBRLI}}}unit"}:
                continue
            identity = element.get("id")
            target = contexts if element.tag.endswith("}context") else units
            if not identity or identity in target:
                raise ValueError("Duplicate or missing context/unit identity")
            if target is contexts:
                entity = element.find(f"{{{XBRLI}}}entity")
                identifier = element.find(f"{{{XBRLI}}}entity/{{{XBRLI}}}identifier")
                period = element.find(f"{{{XBRLI}}}period")
                contexts[identity] = None
                if (entity is None or identifier is None or period is None
                        or identifier.get("scheme") != CIK_SCHEME
                        or not (identifier.text or "").strip().isdigit()
                        or int(identifier.text.strip()) != cik
                        or len(entity) != 1 or len(identifier) or len(element) != 2
                        or element.find(f"{{{XBRLI}}}scenario") is not None):
                    continue
                start = period.findtext(f"{{{XBRLI}}}startDate")
                end = period.findtext(f"{{{XBRLI}}}endDate")
                if start is not None and end is not None and len(period) == 2:
                    contexts[identity] = (date.fromisoformat(start.strip()), date.fromisoformat(end.strip()))
            else:
                measures = element.findall(f"{{{XBRLI}}}measure")
                unit = None
                if (len(element) == 1 and len(measures) == 1 and not len(measures[0])
                        and qname(measures[0]) == f"{{{ISO4217}}}USD"):
                    unit = "USD"
                divide = element.find(f"{{{XBRLI}}}divide")
                if divide is not None and len(element) == 1 and len(divide) == 2:
                    numerator = divide.find(f"{{{XBRLI}}}unitNumerator")
                    denominator = divide.find(f"{{{XBRLI}}}unitDenominator")
                    if (numerator is not None and denominator is not None and len(numerator) == len(denominator) == 1
                            and numerator[0].tag == denominator[0].tag == f"{{{XBRLI}}}measure"
                            and not len(numerator[0]) and not len(denominator[0])
                            and qname(numerator[0]) == f"{{{ISO4217}}}USD"
                            and qname(denominator[0]) == f"{{{XBRLI}}}shares"):
                        unit = "USD/shares"
                units[identity] = unit
    except (ValueError, TypeError):
        return {"status": "unparseable", "reason": "invalid_xbrl_instance"}

    values: set[Decimal] = set()
    count = 0
    for element in root:
        namespace, _, concept = element.tag.removeprefix("{").partition("}")
        if not _GAAP_NAMESPACE.fullmatch(namespace) or concept != claim["concept"]:
            continue
        if (contexts.get(element.get("contextRef", "")) != (claim["period_start"], claim["period_end"])
                or units.get(element.get("unitRef", "")) != claim["unit"]):
            continue
        try:
            value = Decimal((element.text or "").strip())
            if not value.is_finite() or len(element) or element.get("{http://www.w3.org/2001/XMLSchema-instance}nil") in {"true", "1"}:
                raise InvalidOperation
        except InvalidOperation:
            return {"status": "unparseable", "reason": "invalid_matching_fact"}
        values.add(value)
        count += 1
    observed = [float(value) for value in sorted(values)[:8]]
    if any(not Decimal(str(value)).is_finite() for value in observed):
        return {"status": "unparseable", "reason": "invalid_matching_fact"}
    result = {"observed_values": observed, "matching_fact_count": count, "distinct_value_count": len(values)}
    if not values:
        return {**result, "status": "missing", "reason": "exact_fact_absent"}
    if len(values) > 1:
        return {**result, "status": "ambiguous", "reason": "conflicting_exact_facts"}
    matches = next(iter(values)) == Decimal(str(claim["value"]))
    return {**result, "status": "corroborated" if matches else "mismatched",
            "reason": "exact_fact_matches" if matches else "exact_value_differs"}
