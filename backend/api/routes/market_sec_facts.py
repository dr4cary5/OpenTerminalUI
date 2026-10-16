"""On-demand filed SEC facts; independent of FMP and saved terminal captures."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.api.routes.market_context import _symbol
from backend.auth.deps import get_current_user
from backend.core.sec_edgar_client import SecDocumentTooLarge, SecEdgarClient, get_sec_edgar_client
from backend.models import User
from backend.services.sec_filing_evidence import (
    CANDIDATE_DISPLAY_LIMIT,
    DISCLOSURES_PER_CANDIDATE_LIMIT,
    DISPLAY_LIMIT,
    CONCEPT_UNITS,
    contains_filed_fact,
    disclosure_difference_candidates,
    filed_facts,
)
from backend.services.sec_filing_verification import archive_base, filing_document, verify_instance
from backend.services.sec_submission_evidence import (
    continuation_files, crosscheck_recent_submission, recent_submission_index, submission_index,
)


router = APIRouter(prefix="/api/market-context", tags=["market-context"])
logger = logging.getLogger(__name__)


class SecFiledFactsRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=40, examples=["AAPL"])
    filed_start: date
    filed_end: date


class SecFiledFact(BaseModel):
    filed_date: date
    accession: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    taxonomy: Literal["us-gaap"]
    concept: str
    unit: str
    form: str
    period_start: date
    period_end: date
    value: float = Field(allow_inf_nan=False)


class SecDisclosureDifferenceCandidate(BaseModel):
    concept: str
    unit: str
    period_start: date
    period_end: date
    latest_filed_date: date
    disclosure_count: int
    distinct_accession_count: int
    distinct_value_count: int
    within_accession_conflict: bool
    disclosures: list[SecFiledFact]


class SecFiledFactsResponse(BaseModel):
    contract_version: Literal[1] = 1
    symbol: str
    filed_start: date
    filed_end: date
    retrieved_at: datetime
    status: Literal["available", "no_matching_facts", "not_covered", "ambiguous_ticker", "configuration_required", "provider_error"]
    evidence_scope: Literal["sec_current_companyfacts_accession_tagged"] = "sec_current_companyfacts_accession_tagged"
    cik: int | None = None
    matched_count: int = 0
    examined_count: int = 0
    display_limit: int = DISPLAY_LIMIT
    facts: list[SecFiledFact] = Field(default_factory=list)
    difference_basis: Literal["same_concept_unit_exact_period_values_not_verified_revisions"] = "same_concept_unit_exact_period_values_not_verified_revisions"
    candidate_group_count: int = 0
    candidate_display_limit: int = CANDIDATE_DISPLAY_LIMIT
    disclosures_per_candidate_limit: int = DISCLOSURES_PER_CANDIDATE_LIMIT
    difference_candidates: list[SecDisclosureDifferenceCandidate] = Field(default_factory=list)


class SecSubmissionClaim(BaseModel):
    accession: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    form: Literal["10-K", "10-Q", "10-K/A", "10-Q/A"]
    filed_date: date


class SecSubmissionCrosscheckRequest(BaseModel):
    cik: int = Field(ge=1, le=9_999_999_999)
    claims: list[SecSubmissionClaim] = Field(min_length=1, max_length=100)
    check_continuations: bool = False


class SecSubmissionCrosscheckItem(SecSubmissionClaim):
    status: Literal["matched", "metadata_mismatch", "not_in_recent_index", "ambiguous_in_recent_index",
                    "not_in_checked_continuation", "ambiguous_in_continuation", "continuation_not_checked"]
    submission_form: str | None
    submission_filed_date: date | None
    accepted_at: datetime | None
    index_source: Literal["recent", "continuation"] | None = None


class SecSubmissionCrosscheckResponse(BaseModel):
    contract_version: Literal[1] = 1
    cik: int
    retrieved_at: datetime
    status: Literal["available", "configuration_required", "provider_error"]
    index_scope: Literal["sec_current_recent_submissions_only", "sec_current_recent_plus_bounded_continuations"] = "sec_current_recent_submissions_only"
    continuation_files_checked: int = 0
    checked_count: int = 0
    matched_count: int = 0
    results: list[SecSubmissionCrosscheckItem] = Field(default_factory=list)


class SecFilingFactCheckRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=40, examples=["AAPL"])
    fact: SecFiledFact


class SecFilingFactCheckResponse(BaseModel):
    contract_version: Literal[1] = 1
    symbol: str
    cik: int | None = None
    fact: SecFiledFact
    retrieved_at: datetime
    status: Literal["corroborated", "mismatched", "ambiguous", "missing", "unsupported", "unparseable",
                    "configuration_required", "not_covered", "ambiguous_ticker", "provider_error"]
    reason: Literal[
        "unsupported_concept_unit_or_form", "claim_not_in_current_company_facts",
        "invalid_filing_index", "filing_metadata_ambiguous", "filing_metadata_differs",
        "unsupported_instance_link", "instance_not_listed", "multiple_instances",
        "submission_metadata_conflicts", "invalid_xbrl_instance", "invalid_matching_fact",
        "exact_fact_absent", "conflicting_exact_facts", "exact_fact_matches", "exact_value_differs",
        "document_size_limit", "check_timed_out", "filing_check_failed",
    ] | None = None
    evidence_scope: Literal["filing_xbrl_exact_entity_concept_unit_duration"] = "filing_xbrl_exact_entity_concept_unit_duration"
    filing_url: str | None = None
    document_url: str | None = None
    filing_form: str | None = None
    filing_date: date | None = None
    accepted_at: datetime | None = None
    observed_values: list[float] = Field(default_factory=list)
    matching_fact_count: int = 0
    distinct_value_count: int = 0


@router.post("/sec-filing-fact-check", response_model=SecFilingFactCheckResponse)
async def sec_filing_fact_check(
    payload: SecFilingFactCheckRequest,
    _user: User = Depends(get_current_user),
    client: SecEdgarClient = Depends(get_sec_edgar_client),
) -> dict[str, Any]:
    """Inspect one selected Company Facts claim against its filing's XBRL instance.

    Downloads are opt-in, paced, bounded to one index and one instance, and
    limited to 45 seconds overall. An index match cannot corroborate a value.
    """
    symbol = _symbol(payload.symbol)
    claim = payload.fact.model_dump()
    if not claim["period_start"] <= claim["period_end"] <= claim["filed_date"]:
        raise HTTPException(status_code=422, detail="Fact period must be ordered and end by its filing date")
    result: dict[str, Any] = {
        "symbol": symbol, "fact": claim, "retrieved_at": datetime.now(timezone.utc),
        "status": "configuration_required",
    }
    if CONCEPT_UNITS.get(claim["concept"]) != claim["unit"] or claim["form"] not in {"10-K", "10-Q", "10-K/A", "10-Q/A"}:
        return {**result, "status": "unsupported", "reason": "unsupported_concept_unit_or_form"}
    if not client.configured:
        return result
    try:
        async with asyncio.timeout(45):
            ciks = await client.ticker_ciks(symbol)
            if not ciks:
                return {**result, "status": "not_covered"}
            if len(ciks) > 1:
                return {**result, "status": "ambiguous_ticker"}
            cik = ciks[0]
            result["cik"] = cik
            current = await client.company_facts(cik)
            if not contains_filed_fact(current, claim):
                return {**result, "status": "missing", "reason": "claim_not_in_current_company_facts"}
            base = archive_base(cik, claim["accession"])
            result["filing_url"] = base + f"{claim['accession']}-index.html"
            selection = filing_document(await client.filing_index(cik, claim["accession"]), cik, claim)
            result.update({key: value for key, value in selection.items() if key != "instance_name"})
            if selection["status"] != "selected":
                return result
            # Acceptance is optional metadata, not evidence for the reported value.
            recent = recent_submission_index(await client.recent_submissions(cik), cik)
            metadata = crosscheck_recent_submission(recent, claim["accession"], claim["form"], claim["filed_date"])
            if metadata["status"] == "matched":
                result["accepted_at"] = metadata["accepted_at"]
            elif metadata["status"] in {"metadata_mismatch", "ambiguous_in_recent_index"}:
                return {**result, "status": "ambiguous", "reason": "submission_metadata_conflicts"}
            content = await client.filing_instance(cik, claim["accession"], selection["instance_name"])
            result.update(verify_instance(content, cik, claim))
    except SecDocumentTooLarge:
        result.update(status="unsupported", reason="document_size_limit")
    except TimeoutError:
        result.update(status="provider_error", reason="check_timed_out")
    except Exception as exc:
        logger.warning("SEC filing fact check failed for %s (%s)", symbol, type(exc).__name__)
        result.update(status="provider_error", reason="filing_check_failed")
    return result


@router.post("/sec-filed-facts", response_model=SecFiledFactsResponse)
async def sec_file_facts(
    payload: SecFiledFactsRequest,
    _user: User = Depends(get_current_user),
    client: SecEdgarClient = Depends(get_sec_edgar_client),
) -> dict[str, Any]:
    """Inspect current SEC Company Facts by accession, not an archived vintage."""
    symbol = _symbol(payload.symbol)
    if payload.filed_start > payload.filed_end or (payload.filed_end - payload.filed_start).days > 730:
        raise HTTPException(status_code=422, detail="Filed window must be ordered and at most 730 days")
    result: dict[str, Any] = {
        "symbol": symbol, "filed_start": payload.filed_start, "filed_end": payload.filed_end,
        "retrieved_at": datetime.now(timezone.utc), "status": "configuration_required",
    }
    if not client.configured:
        return result
    try:
        ciks = await client.ticker_ciks(symbol)
        if not ciks:
            result["status"] = "not_covered"
        elif len(ciks) > 1:
            result["status"] = "ambiguous_ticker"
        else:
            result["cik"] = ciks[0]
            raw = await client.company_facts(ciks[0])
            if raw.get("cik") != ciks[0]:
                raise ValueError("SEC CIK mismatch")
            facts, matched, examined = filed_facts(raw, payload.filed_start, payload.filed_end)
            candidates, candidate_count = disclosure_difference_candidates(raw, payload.filed_start, payload.filed_end)
            result.update(status="available" if matched else "no_matching_facts", facts=facts,
                          matched_count=matched, examined_count=examined,
                          difference_candidates=candidates, candidate_group_count=candidate_count)
    except Exception as exc:
        # HTTP exceptions can include upstream URLs. Never expose those to clients or logs.
        logger.warning("SEC filed-facts fetch failed for %s (%s)", symbol, type(exc).__name__)
        result.update(status="provider_error", cik=None, facts=[], matched_count=0, examined_count=0,
                      difference_candidates=[], candidate_group_count=0)
    return result


@router.post("/sec-submission-crosscheck", response_model=SecSubmissionCrosscheckResponse)
async def sec_submission_crosscheck(
    payload: SecSubmissionCrosscheckRequest,
    _user: User = Depends(get_current_user),
    client: SecEdgarClient = Depends(get_sec_edgar_client),
) -> dict[str, Any]:
    """Check claims against current SEC indexes; continuation retrieval is bounded and opt-in."""
    result: dict[str, Any] = {
        "cik": payload.cik, "retrieved_at": datetime.now(timezone.utc), "status": "configuration_required",
    }
    if not client.configured:
        return result
    try:
        raw = await client.recent_submissions(payload.cik)
        index = recent_submission_index(raw, payload.cik)
        seen: set[tuple[str, str, date]] = set()
        checks = []
        for claim in payload.claims:
            identity = (claim.accession, claim.form, claim.filed_date)
            if identity in seen:
                continue
            seen.add(identity)
            check = crosscheck_recent_submission(index, claim.accession, claim.form, claim.filed_date)
            checks.append({**claim.model_dump(), **check,
                           "index_source": "recent" if check["status"] != "not_in_recent_index" else None})
        files_checked = 0
        if payload.check_continuations:
            missing = [row for row in checks if row["status"] == "not_in_recent_index"]
            names, covered = continuation_files(raw, payload.cik, {row["filed_date"] for row in missing})
            older: dict[str, dict[str, Any] | None] = {}
            for name in names:
                current = submission_index(await client.continuation_submissions(name))
                files_checked += 1
                for accession, entry in current.items():
                    if accession not in older:
                        older[accession] = entry
                    elif older[accession] != entry:
                        older[accession] = None
            for row in missing:
                if row["filed_date"] not in covered:
                    row["status"] = "continuation_not_checked"
                else:
                    check = crosscheck_recent_submission(older, row["accession"], row["form"], row["filed_date"])
                    row.update(check)
                    row["status"] = {"not_in_recent_index": "not_in_checked_continuation",
                                     "ambiguous_in_recent_index": "ambiguous_in_continuation"}.get(check["status"], check["status"])
                    row["index_source"] = "continuation" if row["status"] not in ("not_in_checked_continuation",) else None
            result["index_scope"] = "sec_current_recent_plus_bounded_continuations"
        result.update(status="available", checked_count=len(checks),
                      matched_count=sum(row["status"] == "matched" for row in checks),
                      continuation_files_checked=files_checked, results=checks)
    except Exception as exc:
        # Never expose upstream URLs or transport diagnostics in a public response.
        logger.warning("SEC submissions cross-check failed for CIK %s (%s)", payload.cik, type(exc).__name__)
        result["status"] = "provider_error"
    return result
