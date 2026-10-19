"""Authenticated, on-demand evidence preparation for comparison briefings."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.api.deps import get_unified_fetcher
from backend.api.routes.market_context import (
    MarketComparisonRequest,
    MarketComparisonResponse,
    compare_market_context,
)
from backend.auth.deps import get_current_user
from backend.models import User
from backend.services.market_briefing_evidence import (
    ComparisonBriefingEvidence,
    build_comparison_evidence,
)

router = APIRouter(prefix="/api/market-context", tags=["market-context"])
EVIDENCE_TIMEOUT_SECONDS = 45.0


class ComparisonBriefingEvidenceRequest(BaseModel):
    # Accept selections, not browser-supplied returns, prose or citation claims.
    model_config = ConfigDict(extra="forbid")
    anchor: str = Field(min_length=1, max_length=40, examples=["AAPL"])
    comparisons: list[str] = Field(min_length=1, max_length=6, examples=[["SPY", "BTC-USD"]])
    period: Literal["1M", "3M", "6M"] = "1M"


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
    selection = MarketComparisonRequest(
        anchor=payload.anchor, comparisons=payload.comparisons, period=payload.period,
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
