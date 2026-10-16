"""Filing-level corroboration requires exact evidence in the actual instance."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.market_sec_facts import router
from backend.auth.deps import get_current_user
from backend.core.sec_edgar_client import SecDocumentTooLarge, SecEdgarClient, get_sec_edgar_client
from backend.services.sec_filing_verification import archive_base, filing_document, verify_instance


CIK = 320193
ACCESSION = "0000320193-25-000001"
BASE = archive_base(CIK, ACCESSION)


def _claim(**changes: object) -> dict:
    return {"accession": ACCESSION, "taxonomy": "us-gaap", "concept": "Revenues", "unit": "USD",
            "form": "10-Q", "filed_date": date(2025, 5, 2), "period_start": date(2025, 1, 1),
            "period_end": date(2025, 3, 31), "value": 100.0, **changes}


def _index(*, instance: str = "issuer-20250331_htm.xml", extra: str = "", form: str = "10-Q",
           filed: str = "2025-05-02") -> bytes:
    return f"""<html><div id="secNum">SEC Accession No. {ACCESSION}</div>
    <div class="formGrouping"><div class="infoHead">Filing Date</div><div class="info">{filed}</div></div>
    <table class="tableFile" summary="Document Format Files">
    <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
    <tr><td>1</td><td>{form}</td><td><a href="{BASE}issuer.htm">issuer.htm</a></td><td>{form}</td><td>100</td></tr></table>
    <table class="tableFile" summary="Data Files">
    <tr><td>10</td><td>EXTRACTED XBRL INSTANCE DOCUMENT</td><td><a href="{BASE}{instance}">{instance}</a></td><td>XML</td><td>100</td></tr>
    {extra}</table></html>""".encode()


def _instance(*, value: str = "100", extra: str = "", context_extra: str = "", cik: int = CIK,
              unit: str = "currency:USD", end: str = "2025-03-31", namespace: str = "http://fasb.org/us-gaap/2025",
              fact_attrs: str = "") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:gaap="{namespace}"
      xmlns:currency="http://www.xbrl.org/2003/iso4217" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <xbrli:context id="duration"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{cik:010d}</xbrli:identifier>{context_extra}</xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:unit id="USD"><xbrli:measure>{unit}</xbrli:measure></xbrli:unit>
      <gaap:Revenues contextRef="duration" unitRef="USD" decimals="-6" {fact_attrs}>{value}</gaap:Revenues>
      {extra}
    </xbrli:xbrl>""".encode()


def test_index_selects_only_listed_instance_and_validates_filing_metadata() -> None:
    selected = filing_document(_index(), CIK, _claim())
    assert selected["status"] == "selected"
    assert selected["document_url"] == BASE + "issuer-20250331_htm.xml"
    assert selected["filing_date"] == date(2025, 5, 2)
    assert filing_document(_index(form="10-Q/A"), CIK, _claim())["reason"] == "filing_metadata_differs"
    assert filing_document(_index(filed="2025-05-03"), CIK, _claim())["status"] == "mismatched"
    assert filing_document(_index().replace(ACCESSION.encode(), b"0000320193-25-999999"), CIK, _claim())["status"] == "unparseable"


@pytest.mark.parametrize("name", ["../foreign.xml", "test.xml?redirect=https://evil.example", "nested/test.xml"])
def test_index_rejects_unsafe_or_nonlocal_instance_links(name: str) -> None:
    assert filing_document(_index(instance=name), CIK, _claim())["status"] == "unsupported"


def test_index_distinguishes_missing_and_multiple_instances() -> None:
    absent = _index().replace(b"EXTRACTED XBRL INSTANCE DOCUMENT", b"LABEL LINKBASE DOCUMENT")
    assert filing_document(absent, CIK, _claim())["reason"] == "instance_not_listed"
    extra = f'<tr><td>11</td><td>XBRL INSTANCE DOCUMENT</td><td><a href="{BASE}second.xml">second.xml</a></td><td>EX-101.INS</td></tr>'
    assert filing_document(_index(extra=extra), CIK, _claim())["reason"] == "multiple_instances"
    foreign = _index().replace(BASE.encode(), b"https://evil.example/")
    assert filing_document(foreign, CIK, _claim())["reason"] == "unsupported_instance_link"
    legacy = _index().replace(b"EXTRACTED XBRL INSTANCE DOCUMENT", b"XBRL INSTANCE DOCUMENT").replace(b"<td>XML</td>", b"<td>EX-101.INS</td>")
    assert filing_document(legacy, CIK, _claim())["status"] == "selected"


def test_exact_value_is_corroborated_and_duplicate_equal_values_are_counted() -> None:
    extra = '<gaap:Revenues contextRef="duration" unitRef="USD" decimals="INF">1E2</gaap:Revenues>'
    result = verify_instance(_instance(extra=extra), CIK, _claim())
    assert result == {"status": "corroborated", "reason": "exact_fact_matches", "observed_values": [100.0],
                      "matching_fact_count": 2, "distinct_value_count": 1}


def test_different_value_and_conflicting_values_remain_distinct() -> None:
    mismatch = verify_instance(_instance(value="120"), CIK, _claim())
    assert (mismatch["status"], mismatch["observed_values"]) == ("mismatched", [120.0])
    extra = '<gaap:Revenues contextRef="duration" unitRef="USD" decimals="0">120</gaap:Revenues>'
    conflict = verify_instance(_instance(extra=extra), CIK, _claim())
    assert (conflict["status"], conflict["observed_values"]) == ("ambiguous", [100.0, 120.0])


@pytest.mark.parametrize("changes", [
    {"cik": 42}, {"end": "2025-03-30"}, {"unit": "currency:EUR"},
    {"namespace": "https://custom.example/us-gaap/2025"},
    {"context_extra": '<xbrli:segment><dimension>business unit</dimension></xbrli:segment>'},
])
def test_wrong_issuer_period_unit_namespace_or_dimension_cannot_corroborate(changes: dict) -> None:
    assert verify_instance(_instance(**changes), CIK, _claim())["status"] == "missing"


def test_share_unit_division_and_namespace_aliases_are_resolved() -> None:
    xml = _instance().replace(b"gaap:Revenues", b"gaap:EarningsPerShareDiluted")
    xml = xml.replace(b"<xbrli:measure>currency:USD</xbrli:measure>", b"""
      <xbrli:divide><xbrli:unitNumerator><xbrli:measure>currency:USD</xbrli:measure></xbrli:unitNumerator>
      <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide>""")
    assert verify_instance(xml, CIK, _claim(concept="EarningsPerShareDiluted", unit="USD/shares"))["status"] == "corroborated"
    # A prefix rebound on the measure cannot borrow a valid root namespace.
    xml = xml.replace(b"<xbrli:measure>currency:USD", b'<xbrli:measure xmlns:currency="https://evil.example">currency:USD')
    assert verify_instance(xml, CIK, _claim(concept="EarningsPerShareDiluted", unit="USD/shares"))["status"] == "missing"


@pytest.mark.parametrize("xml", [b"not XML", b"<html/>",
    b'<!DOCTYPE xbrl [<!ENTITY x "100">]><xbrl>&x;</xbrl>',
    _instance(value="NaN"), _instance(value="Infinity"), _instance(value="1E99999"),
    _instance(value="", fact_attrs='xsi:nil="true"'),
    _instance(extra='<xbrli:unit id="USD"><xbrli:measure>currency:EUR</xbrli:measure></xbrli:unit>'),
    ('<!DOCTYPE xbrl [<!ENTITY x "100">]><xbrl>&x;</xbrl>').encode("utf-16"),
    b"<x>" * 101 + b"</x>" * 101,
    b'<xbrl>' + b'<x/>' * 150_001 + b'</xbrl>',
    b'<?xml version="1.0" encoding="unknown"?><xbrl/>',
])
def test_unparseable_or_unsafe_instances_never_corroborate(xml: bytes) -> None:
    assert verify_instance(xml, CIK, _claim())["status"] == "unparseable"


def _app(client: SecEdgarClient) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="owner")
    app.dependency_overrides[get_sec_edgar_client] = lambda: client
    return TestClient(app)


def _body(**changes: object) -> dict:
    claim = _claim(**changes)
    return {"symbol": "AAPL", "fact": {key: value.isoformat() if isinstance(value, date) else value for key, value in claim.items()}}


def _respond(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("company_tickers.json"):
        return httpx.Response(200, json={"0": {"ticker": "AAPL", "cik_str": CIK}})
    if "/companyfacts/" in request.url.path:
        return httpx.Response(200, json={"cik": CIK, "facts": {"us-gaap": {"Revenues": {"units": {"USD": [{
            "accn": ACCESSION, "start": "2025-01-01", "end": "2025-03-31", "filed": "2025-05-02",
            "form": "10-Q", "val": 100,
        }]}}}}})
    if "/submissions/" in request.url.path:
        return httpx.Response(200, json={"cik": CIK, "filings": {"recent": {
            "accessionNumber": [ACCESSION], "form": ["10-Q"], "filingDate": ["2025-05-02"],
            "acceptanceDateTime": ["2025-05-02T15:30:00Z"],
        }}})
    return httpx.Response(200, content=_index() if request.url.path.endswith("-index.html") else _instance())


def test_route_returns_filing_evidence_and_acceptance_without_mutating_fact() -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _respond(request)

    client = _app(SecEdgarClient(user_agent="test@example.com", transport=httpx.MockTransport(respond), pace_seconds=0))
    response = client.post("/api/market-context/sec-filing-fact-check", json=_body())
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "corroborated"
    assert data["fact"] == _body()["fact"]
    assert data["accepted_at"] == "2025-05-02T15:30:00Z"
    assert data["filing_url"] == BASE + ACCESSION + "-index.html"
    assert data["document_url"] == BASE + "issuer-20250331_htm.xml"
    assert data["observed_values"] == [100]
    assert len(calls) == 5
    assert all(request.headers["User-Agent"] == "test@example.com" for request in calls)


@pytest.mark.parametrize("mode,expected", [("match_index_only", "unsupported"), ("different", "mismatched"),
    ("bad_xml", "unparseable"), ("failure", "provider_error"), ("missing", "missing"), ("conflict", "ambiguous")])
def test_route_preserves_distinct_filing_outcomes(mode: str, expected: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("-index.html") and mode == "match_index_only":
            return httpx.Response(200, content=_index().replace(b"EXTRACTED XBRL INSTANCE DOCUMENT", b"OTHER"))
        if request.url.path.endswith("_htm.xml"):
            if mode == "failure":
                return httpx.Response(503, text="secret-upstream-url")
            content = {"different": _instance(value="120"), "bad_xml": b"broken XML",
                       "missing": _instance(cik=42), "conflict": _instance(extra='<gaap:Revenues contextRef="duration" unitRef="USD">120</gaap:Revenues>')}
            return httpx.Response(200, content=content[mode])
        return _respond(request)

    client = _app(SecEdgarClient(user_agent="test@example.com", transport=httpx.MockTransport(respond), pace_seconds=0))
    response = client.post("/api/market-context/sec-filing-fact-check", json=_body())
    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected
    assert response.json()["fact"]["value"] == 100
    assert "secret-upstream-url" not in response.text


def test_route_gates_configuration_scope_and_current_claim_before_archive_requests() -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _respond(request)

    transport = httpx.MockTransport(respond)
    disabled = _app(SecEdgarClient(user_agent="", transport=transport, pace_seconds=0))
    assert disabled.post("/api/market-context/sec-filing-fact-check", json=_body()).json()["status"] == "configuration_required"
    assert calls == []
    client = _app(SecEdgarClient(user_agent="test@example.com", transport=transport, pace_seconds=0))
    unsupported = client.post("/api/market-context/sec-filing-fact-check", json=_body(concept="Assets"))
    assert unsupported.json()["status"] == "unsupported"
    assert calls == []
    missing = client.post("/api/market-context/sec-filing-fact-check", json=_body(value=101))
    assert missing.json()["reason"] == "claim_not_in_current_company_facts"
    assert len(calls) == 2 and not any("Archives" in path for path in calls)
    invalid = client.post("/api/market-context/sec-filing-fact-check", json=_body(accession="../bad"))
    assert invalid.status_code == 422
    assert client.post("/api/market-context/sec-filing-fact-check", json=_body(period_end=date(2026, 1, 1))).status_code == 422


def test_amendment_and_original_are_checked_independently_without_supersession() -> None:
    amended = "0000320193-25-000002"

    def respond(request: httpx.Request) -> httpx.Response:
        if "/companyfacts/" in request.url.path:
            data = _respond(request).json()
            rows = data["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
            rows.append({**rows[0], "accn": amended, "form": "10-Q/A", "filed": "2025-08-01", "val": 120})
            return httpx.Response(200, json=data)
        if "/submissions/" in request.url.path:
            # The original remains in recent; absence of the amendment is not disproof.
            return _respond(request)
        if amended.replace("-", "") in request.url.path:
            if request.url.path.endswith("-index.html"):
                content = _index(form="10-Q/A", filed="2025-08-01").replace(BASE.encode(), archive_base(CIK, amended).encode()).replace(ACCESSION.encode(), amended.encode())
            else:
                content = _instance(value="120")
            return httpx.Response(200, content=content)
        return _respond(request)

    client = _app(SecEdgarClient(user_agent="test@example.com", transport=httpx.MockTransport(respond), pace_seconds=0))
    original = client.post("/api/market-context/sec-filing-fact-check", json=_body()).json()
    amendment = client.post("/api/market-context/sec-filing-fact-check", json=_body(
        accession=amended, form="10-Q/A", filed_date=date(2025, 8, 1), value=120)).json()
    assert original["status"] == amendment["status"] == "corroborated"
    assert original["observed_values"] == [100] and amendment["observed_values"] == [120]
    assert amendment["accepted_at"] is None
    assert original["fact"]["period_start"] == amendment["fact"]["period_start"]
    assert original["fact"]["period_end"] == amendment["fact"]["period_end"]
    assert original["fact"]["accession"] != amendment["fact"]["accession"]


@pytest.mark.parametrize("failure,expected_reason", [(TimeoutError, "check_timed_out"), (SecDocumentTooLarge, "document_size_limit")])
def test_route_reports_time_and_size_limits(monkeypatch, failure: type[Exception], expected_reason: str) -> None:
    client = SecEdgarClient(user_agent="test@example.com", transport=httpx.MockTransport(_respond), pace_seconds=0)

    async def fail(*args):
        raise failure("private diagnostics")

    monkeypatch.setattr(client, "filing_index", fail)
    response = _app(client).post("/api/market-context/sec-filing-fact-check", json=_body())
    assert response.json()["reason"] == expected_reason
    assert "private diagnostics" not in response.text


@pytest.mark.parametrize("tickers,status", [({}, "not_covered"),
    ({"0": {"ticker": "AAPL", "cik_str": 1}, "1": {"ticker": "AAPL", "cik_str": 2}}, "ambiguous_ticker")])
def test_route_requires_unique_exact_ticker_mapping(tickers: dict, status: str) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=tickers)

    client = _app(SecEdgarClient(user_agent="test@example.com", transport=httpx.MockTransport(respond), pace_seconds=0))
    assert client.post("/api/market-context/sec-filing-fact-check", json=_body()).json()["status"] == status
    assert len(calls) == 1


def test_document_reader_bounds_streams_and_never_follows_redirects_or_caches_failure() -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(302, headers={"Location": "https://evil.example/secret"})
        return httpx.Response(200, content=b"12345")

    client = SecEdgarClient(user_agent="test@example.com", transport=httpx.MockTransport(respond), pace_seconds=0)

    async def exercise() -> None:
        with pytest.raises(httpx.HTTPStatusError):
            await client.filing_index(CIK, ACCESSION)
        with pytest.raises(SecDocumentTooLarge):
            await client._document(BASE + "instance.xml", 4)
        assert await client._document(BASE + "instance.xml", 5) == b"12345"
        with pytest.raises(ValueError):
            await client.filing_instance(CIK, ACCESSION, "../foreign.xml")

    asyncio.run(exercise())
    assert len(calls) == 3
