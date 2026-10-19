"""Authenticated comparison evidence, synthesis and cancellable progress."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.api.deps import get_unified_fetcher
from backend.api.routes.market_context import (
    MarketComparisonRequest,
    MarketComparisonResponse,
    _symbol,
    compare_market_context,
)
from backend.auth.deps import get_current_user
from backend.config.settings import get_settings
from backend.models import User
from backend.services.comparison_briefing import (
    BriefingPhase, ComparisonBriefingResult, generate_comparison_briefing, unavailable_briefing,
)
from backend.services.insight_lifecycle import stream_insight_lifecycle
from backend.services.market_briefing_evidence import (
    ComparisonBriefingEvidence,
    build_comparison_evidence,
)

router = APIRouter(prefix="/api/market-context", tags=["market-context"])
EVIDENCE_TIMEOUT_SECONDS = 45.0
logger = logging.getLogger(__name__)


class ComparisonBriefingEvidenceRequest(BaseModel):
    # Accept selections, not browser-supplied returns, prose or citation claims.
    model_config = ConfigDict(extra="forbid")
    anchor: str = Field(min_length=1, max_length=40, examples=["AAPL"])
    comparisons: list[str] = Field(min_length=1, max_length=6, examples=[["SPY", "BTC-USD"]])
    period: Literal["1M", "3M", "6M"] = "1M"


def _normalized_payload(payload: ComparisonBriefingEvidenceRequest) -> ComparisonBriefingEvidenceRequest:
    anchor = _symbol(payload.anchor)
    comparisons = list(dict.fromkeys(_symbol(symbol) for symbol in payload.comparisons))
    if anchor in comparisons:
        raise HTTPException(status_code=422, detail="Anchor cannot be a comparison symbol")
    return ComparisonBriefingEvidenceRequest(anchor=anchor, comparisons=comparisons, period=payload.period)


@router.post(
    "/briefing-evidence", response_model=ComparisonBriefingEvidence,
    responses={504: {"description": "Evidence preparation timed out; no packet was created."}},
)
async def prepare_comparison_briefing_evidence(
    payload: ComparisonBriefingEvidenceRequest,
    user: User = Depends(get_current_user),
    fetcher: Any = Depends(get_unified_fetcher),
) -> ComparisonBriefingEvidence:
    """Fetch a fresh comparison and project citable, bounded evidence only.

    This does not generate a briefing, persist a snapshot or reuse the browser's
    previous response. Optional provider panels and private notes are excluded.
    """
    normalized = _normalized_payload(payload)
    selection = MarketComparisonRequest(
        anchor=normalized.anchor, comparisons=normalized.comparisons, period=normalized.period,
        # This packet intentionally covers only native provider-close returns;
        # avoid optional FX fetches even if a page has a reporting currency set.
        reporting_currency=None,
    )
    try:
        async with asyncio.timeout(EVIDENCE_TIMEOUT_SECONDS):
            result = await compare_market_context(selection, user, fetcher)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail={
            "code": "evidence_timed_out",
            "message": "Comparison evidence preparation timed out; no packet was created.",
        }) from exc
    return build_comparison_evidence(MarketComparisonResponse.model_validate(result))


async def _run_briefing(
    payload: ComparisonBriefingEvidenceRequest, user: User, fetcher: Any,
    on_phase: Callable[[BriefingPhase], None] | None = None,
) -> ComparisonBriefingResult:
    if on_phase is not None:
        on_phase("preparing_evidence")
    try:
        evidence = await prepare_comparison_briefing_evidence(payload, user, fetcher)
    except HTTPException as exc:
        if exc.status_code != 504:
            raise
        logger.warning("Comparison briefing evidence preparation reached its deadline")
        return unavailable_briefing("evidence_timed_out")
    except Exception as exc:
        logger.warning("Comparison briefing evidence preparation failed (%s)", type(exc).__name__)
        raise
    return await generate_comparison_briefing(evidence, on_phase=on_phase)


@router.post("/briefing", response_model=ComparisonBriefingResult)
async def create_comparison_briefing(
    payload: ComparisonBriefingEvidenceRequest,
    user: User = Depends(get_current_user),
    fetcher: Any = Depends(get_unified_fetcher),
) -> ComparisonBriefingResult:
    """Prepare fresh evidence and generate a citation-linked descriptive briefing.

    Structure, snapshot identity and references are validated, not the truth of
    model prose. Evidence and its mandatory caveats remain attached on failure.
    This invokes no optional provider panels and saves/indexes nothing.
    """
    return await _run_briefing(_normalized_payload(payload), user, fetcher)


@router.post(
    "/briefing/stream", response_class=StreamingResponse,
    responses={200: {"content": {"application/x-ndjson": {"schema": {
        "type": "string", "description": (
            "NDJSON lifecycle: start (with evidence/model deadline budgets), "
            "progress (preparing_evidence/checking_model/"
            "generating/validating/retrying), and one final result containing a "
            "ComparisonBriefingResult, or an unexpected error. No raw prose deltas."
        ),
    }}}}},
)
async def stream_comparison_briefing(
    payload: ComparisonBriefingEvidenceRequest,
    user: User = Depends(get_current_user),
    fetcher: Any = Depends(get_unified_fetcher),
) -> StreamingResponse:
    """Stream progress and a final checked response; disconnect cancels work.

    Uses completion requests, not provider token streaming, so providers without
    streaming support work too. Unvalidated model text never enters this stream.
    See the synchronous briefing endpoint for the final result schema.
    """
    normalized = _normalized_payload(payload)  # Reject invalid symbols before 200 headers.
    phase: BriefingPhase = "preparing_evidence"

    def set_phase(value: BriefingPhase) -> None:
        nonlocal phase
        phase = value

    async def operation() -> dict[str, Any]:
        result = await _run_briefing(normalized, user, fetcher, set_phase)
        return result.model_dump(mode="json")

    async def events():
        async for event in stream_insight_lifecycle(operation):
            if event["type"] == "start":
                event = {**event, "deadlines": {
                    "evidence_seconds": EVIDENCE_TIMEOUT_SECONDS,
                    "model_seconds": get_settings().llm_timeout_seconds,
                }}
            elif event["type"] == "progress":
                event = {**event, "phase": phase}
            yield json.dumps(event, separators=(",", ":")) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    })
