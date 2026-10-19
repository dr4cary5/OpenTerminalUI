"""Bounded, deterministic citation inputs for comparison briefings.

This is an evidence projection, not synthesis. It deliberately excludes other
views/panels: availability on the comparison page does not make them evidence
in this packet. Citation IDs are local to the fingerprinted snapshot.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from backend.api.routes.market_context import MarketComparisonResponse


class BriefingAssetEvidence(BaseModel):
    symbol: str
    history_adapter: str | None
    history_feed: str | None
    latest_date: date | None
    quote_unit: str | None
    quote_unit_source: Literal["yahoo_chart_meta", "unavailable"]
    reported_adjustment_basis: Literal["raw", "non_split_adjusted", "unspecified"] | None
    withheld_provider_close_count: int = Field(ge=0)
    # Counts cover the selected payload, not necessarily the compared window.
    withheld_date_scope: Literal["selected_history_payload"] = "selected_history_payload"


class BriefingReturnEvidence(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    start_date: date
    end_date: date
    observations: int = Field(ge=2)
    anchor_return_pct: float
    comparison_return_pct: float
    relative_return_pp: float
    freshness: Literal["current", "stale"]


BriefingCaveat = Literal[
    "native_quote_not_fx_normalized", "utc_dates_not_simultaneous_sessions",
    "no_causal_or_investment_conclusion", "stale_history",
    "mixed_reported_price_basis", "unverified_price_basis",
    "reported_basis_not_quality_verification", "unknown_quote_unit",
    "withheld_conflicting_closes", "comparison_unavailable",
]


class BriefingPairEvidence(BaseModel):
    citation_id: str = Field(pattern=r"^P[1-6]$")
    status: Literal["available", "unavailable"]
    reason: Literal["missing_history", "insufficient_overlap", "provider_error"] | None
    anchor: BriefingAssetEvidence
    comparison: BriefingAssetEvidence
    pair_reported_basis_status: Literal["matching_reported", "mixed_reported", "unverified", "unavailable"]
    returns: BriefingReturnEvidence | None
    caveats: list[BriefingCaveat]


ExcludedEvidence = Literal[
    "headlines", "fundamentals", "sec_filing_checks", "macro", "technical_measures",
    "fx_views", "adjusted_views", "exchange_local_views", "private_research",
]


class ComparisonBriefingEvidence(BaseModel):
    schema_version: Literal["1"] = "1"
    citation_scope: Literal["packet_local"] = "packet_local"
    packet_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["available", "partial", "unavailable"]
    anchor: str
    period: Literal["1M", "3M", "6M"]
    retrieved_at: datetime
    data_source: Literal["unified_history"] = "unified_history"
    method: Literal["same_utc_date_daily_closes"] = "same_utc_date_daily_closes"
    return_basis: Literal["native_quote_currency_provider_closes"] = "native_quote_currency_provider_closes"
    included_evidence: Literal["shared_utc_provider_close_returns"] = "shared_utc_provider_close_returns"
    not_included: list[ExcludedEvidence]
    comparisons: list[BriefingPairEvidence] = Field(min_length=1, max_length=6)


def build_comparison_evidence(comparison: MarketComparisonResponse) -> ComparisonBriefingEvidence:
    """Project one server-calculated response; never infer missing evidence.

    No providers, model, persistence or private retrieval are invoked here. The
    fingerprint covers the entire returned packet except packet_id itself,
    including source time, omissions, units, caveats and ordering.
    """
    pairs: list[BriefingPairEvidence] = []
    for index, row in enumerate(comparison.comparisons, start=1):
        caveats: list[BriefingCaveat] = [
            "native_quote_not_fx_normalized", "utc_dates_not_simultaneous_sessions",
            "no_causal_or_investment_conclusion",
        ]
        assets: list[BriefingAssetEvidence] = []
        for role, symbol in (("anchor", comparison.anchor), ("comparison", row.symbol)):
            quote = getattr(row, f"{role}_quote_unit")
            conflicts = getattr(row, f"{role}_close_date_conflicts")
            assets.append(BriefingAssetEvidence(
                symbol=symbol,
                history_adapter=getattr(row, f"{role}_history_source"),
                history_feed=getattr(row, f"{role}_history_feed"),
                latest_date=getattr(row, f"{role}_latest_date"),
                quote_unit=quote.unit,
                quote_unit_source=quote.source,
                reported_adjustment_basis=getattr(row, f"{role}_reported_adjustment_basis"),
                withheld_provider_close_count=conflicts.provider_close_count,
            ))
        if any(asset.quote_unit is None for asset in assets):
            caveats.append("unknown_quote_unit")
        if any(asset.withheld_provider_close_count for asset in assets):
            caveats.append("withheld_conflicting_closes")
        if row.pair_reported_basis_status == "mixed_reported":
            caveats.append("mixed_reported_price_basis")
        elif row.pair_reported_basis_status == "unverified":
            caveats.append("unverified_price_basis")
        elif row.pair_reported_basis_status == "matching_reported":
            caveats.append("reported_basis_not_quality_verification")

        returns = None
        if row.status == "available":
            # Validation fails closed on an incomplete internal available row:
            # never invent dates, zeros, freshness or an observation count.
            returns = BriefingReturnEvidence(
                start_date=row.start_date, end_date=row.end_date,
                observations=row.observations, freshness=row.freshness,
                anchor_return_pct=row.anchor_return_pct,
                comparison_return_pct=row.comparison_return_pct,
                relative_return_pp=row.relative_return_pp,
            )
            if row.freshness == "stale":
                caveats.append("stale_history")
        else:
            caveats.append("comparison_unavailable")
        pairs.append(BriefingPairEvidence(
            citation_id=f"P{index}", status=row.status, reason=row.reason,
            anchor=assets[0], comparison=assets[1],
            pair_reported_basis_status=row.pair_reported_basis_status,
            returns=returns, caveats=caveats,
        ))

    available = sum(pair.status == "available" for pair in pairs)
    packet = ComparisonBriefingEvidence(
        packet_id="0" * 64,
        status="unavailable" if not available else "available" if available == len(pairs) else "partial",
        anchor=comparison.anchor, period=comparison.period,
        retrieved_at=comparison.retrieved_at, comparisons=pairs,
        not_included=[
            "headlines", "fundamentals", "sec_filing_checks", "macro", "technical_measures",
            "fx_views", "adjusted_views", "exchange_local_views", "private_research",
        ],
    )
    canonical = json.dumps(packet.model_dump(mode="json", exclude={"packet_id"}),
                           sort_keys=True, separators=(",", ":"), allow_nan=False)
    return packet.model_copy(update={"packet_id": hashlib.sha256(canonical.encode()).hexdigest()})
