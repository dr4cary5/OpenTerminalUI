"""Briefings must start with bounded server evidence, not browser assertions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.api.deps import get_unified_fetcher
from backend.api.routes import market_briefing
from backend.api.routes.market_context import MarketComparisonResponse
from backend.auth.deps import get_current_user
from backend.services.market_briefing_evidence import build_comparison_evidence


def _row(symbol: str = "SPY", **changes: object) -> dict:
    conflict = {"provider_close_count": 0, "provider_close_dates": [],
                "adjusted_close_count": 0, "adjusted_close_dates": [], "display_limit": 20}
    return {
        "symbol": symbol, "status": "available", "reason": None,
        "start_date": "2026-09-01", "end_date": "2026-09-30", "observations": 22,
        "anchor_latest_date": "2026-09-30", "comparison_latest_date": "2026-09-30",
        "anchor_history_source": "yahoo", "comparison_history_source": "alpaca",
        "anchor_history_feed": "yahoo_chart", "comparison_history_feed": "alpaca_stocks_bars:iex:raw",
        "anchor_reported_adjustment_basis": "unspecified", "comparison_reported_adjustment_basis": "raw",
        "pair_reported_basis_status": "unverified",
        "anchor_quote_unit": {"unit": "USD", "source": "yahoo_chart_meta"},
        "comparison_quote_unit": {"unit": None, "source": "unavailable"},
        "anchor_close_date_conflicts": conflict, "comparison_close_date_conflicts": conflict,
        "anchor_return_pct": 12.3456, "comparison_return_pct": -2.3456,
        "relative_return_pp": 14.6912, "freshness": "current", **changes,
    }


def _comparison(*rows: dict) -> MarketComparisonResponse:
    return MarketComparisonResponse.model_validate({
        "anchor": "AAPL", "period": "1M", "retrieved_at": "2026-10-01T10:00:00Z",
        "data_source": "unified_history", "return_basis": "native_quote_currency_provider_closes",
        "method": "same_utc_date_daily_closes", "comparisons": list(rows) or [_row()],
    })


def test_packet_preserves_exact_pair_scope_and_does_not_invent_source_or_units() -> None:
    evidence = build_comparison_evidence(_comparison())
    assert evidence.citation_scope == "packet_local"
    pair = evidence.comparisons[0]
    assert (pair.citation_id, pair.anchor.symbol, pair.comparison.symbol) == ("P1", "AAPL", "SPY")
    assert pair.returns.anchor_return_pct == 12.3456
    assert pair.returns.relative_return_pp == 14.6912
    assert pair.returns.start_date == date(2026, 9, 1)
    assert pair.returns.end_date == date(2026, 9, 30)
    assert pair.returns.observations == 22
    assert pair.comparison.quote_unit is None  # No USD guess for a US ticker.
    assert pair.comparison.history_feed == "alpaca_stocks_bars:iex:raw"
    assert "unknown_quote_unit" in pair.caveats
    assert "unverified_price_basis" in pair.caveats
    assert "native_quote_not_fx_normalized" in pair.caveats
    assert "utc_dates_not_simultaneous_sessions" in pair.caveats
    assert "no_causal_or_investment_conclusion" in pair.caveats
    assert set(evidence.not_included) == {
        "headlines", "fundamentals", "sec_filing_checks", "macro", "technical_measures",
        "fx_views", "adjusted_views", "exchange_local_views", "private_research",
    }


def test_fingerprint_covers_complete_serialized_packet_and_is_repeatable() -> None:
    original = _comparison()
    first = build_comparison_evidence(original)
    second = build_comparison_evidence(original)
    assert first == second
    canonical = json.dumps(first.model_dump(mode="json", exclude={"packet_id"}),
                           sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert first.packet_id == hashlib.sha256(canonical.encode()).hexdigest()
    assert "points" not in first.model_dump_json()


@pytest.mark.parametrize("change", [
    {"anchor_return_pct": 12.3457},
    {"anchor_history_feed": "another_feed"},
    {"anchor_quote_unit": {"unit": "EUR", "source": "yahoo_chart_meta"}},
    {"freshness": "stale"},
    {"end_date": "2026-09-29"},
])
def test_fingerprint_changes_with_evidence_identity(change: dict) -> None:
    assert build_comparison_evidence(_comparison()).packet_id != build_comparison_evidence(
        _comparison(_row(**change))).packet_id


def test_timestamp_and_comparison_order_are_part_of_snapshot_identity() -> None:
    original = _comparison(_row(), _row("BTC-USD"))
    changed_time = original.model_copy(update={"retrieved_at": datetime(2026, 10, 1, 11, tzinfo=timezone.utc)})
    reversed_pairs = original.model_copy(update={"comparisons": list(reversed(original.comparisons))})
    packets = [build_comparison_evidence(item) for item in (original, changed_time, reversed_pairs)]
    assert len({packet.packet_id for packet in packets}) == 3
    assert [pair.citation_id for pair in packets[0].comparisons] == ["P1", "P2"]
    assert packets[2].comparisons[0].comparison.symbol == "BTC-USD"


def test_unavailable_pair_is_not_zero_return_and_partial_keeps_each_pair_window() -> None:
    unavailable = _row("MISSING", status="unavailable", reason="provider_error")
    second = _row("BTC-USD", start_date="2026-09-04", observations=19)
    packet = build_comparison_evidence(_comparison(_row(), unavailable, second))
    assert packet.status == "partial"
    assert packet.comparisons[1].returns is None  # Even if an invalid caller left numbers in the row.
    assert packet.comparisons[1].reason == "provider_error"
    assert "comparison_unavailable" in packet.comparisons[1].caveats
    assert packet.comparisons[2].returns.start_date == date(2026, 9, 4)
    assert packet.comparisons[2].returns.observations == 19
    assert build_comparison_evidence(_comparison(unavailable)).status == "unavailable"


@pytest.mark.parametrize("status,expected", [
    ("matching_reported", "reported_basis_not_quality_verification"),
    ("mixed_reported", "mixed_reported_price_basis"),
    ("unverified", "unverified_price_basis"),
])
def test_reported_basis_never_becomes_verified_quality(status: str, expected: str) -> None:
    pair = build_comparison_evidence(_comparison(_row(pair_reported_basis_status=status))).comparisons[0]
    assert expected in pair.caveats


def test_stale_and_conflicting_history_caveats_are_preserved() -> None:
    conflict = {"provider_close_count": 2, "provider_close_dates": ["2026-08-01", "2026-08-02"],
                "adjusted_close_count": 0, "adjusted_close_dates": [], "display_limit": 20}
    pair = build_comparison_evidence(_comparison(_row(
        freshness="stale", anchor_close_date_conflicts=conflict))).comparisons[0]
    assert "stale_history" in pair.caveats
    assert "withheld_conflicting_closes" in pair.caveats
    assert pair.anchor.withheld_provider_close_count == 2
    assert pair.anchor.withheld_date_scope == "selected_history_payload"


@pytest.mark.parametrize("change", [
    {"anchor_return_pct": float("nan")}, {"relative_return_pp": float("inf")},
    {"end_date": None}, {"observations": None}, {"freshness": None},
])
def test_incomplete_available_evidence_fails_closed(change: dict) -> None:
    with pytest.raises(ValidationError):
        build_comparison_evidence(_comparison(_row(**change)))


class _Fetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch_history_with_provenance(self, symbol: str, **kwargs: object):
        self.calls.append(symbol)
        if symbol == "FAIL":
            raise RuntimeError("provider down")
        today = datetime.now(timezone.utc).date()
        days = [today - timedelta(days=day) for day in range(40, -1, -1)]
        return ({"chart": {"result": [{
            "meta": {"currency": "USD"},
            "timestamp": [int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp()) for day in days],
            "indicators": {"quote": [{
                **{key: [100 + i for i in range(len(days))]
                   for key in ("open", "high", "low", "close")},
                "volume": [100] * len(days),
            }]},
        }]}}, "yahoo", "yahoo_chart")


def _client(fetcher: _Fetcher, authenticated: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(market_briefing.router)
    app.dependency_overrides[get_unified_fetcher] = lambda: fetcher
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="owner")
    return TestClient(app)


def test_http_fetches_server_comparison_normalizes_deduplicates_and_keeps_failure(monkeypatch) -> None:
    # This real route path uses only injected history. FX must not be requested.
    from backend.api.routes import market_context
    fx_calls: list[bool] = []

    async def no_fx(*args, **kwargs):
        fx_calls.append(True)
        raise AssertionError("An evidence packet must not request optional FX")

    monkeypatch.setattr(market_context.forex_service, "get_historical_valuation_rates", no_fx)
    fetcher = _Fetcher()
    with _client(fetcher) as client:
        response = client.post("/api/market-context/briefing-evidence", json={
            "anchor": " aapl ", "comparisons": ["spy", "SPY", "FAIL"], "period": "1M",
        })
    assert response.status_code == 200, response.text
    packet = response.json()
    assert packet["status"] == "partial"
    assert packet["anchor"] == "AAPL"
    assert sorted(fetcher.calls) == ["AAPL", "FAIL", "SPY"]
    assert len(packet["comparisons"]) == 2
    assert packet["comparisons"][0]["returns"]["anchor_return_pct"] is not None
    assert packet["comparisons"][1]["reason"] == "provider_error"
    assert packet["comparisons"][1]["returns"] is None
    assert fx_calls == []


@pytest.mark.parametrize("payload", [
    {"anchor": "AAPL", "comparisons": ["AAPL"]},
    {"anchor": "invalid symbol", "comparisons": ["SPY"]},
    {"anchor": "AAPL", "comparisons": []},
    {"anchor": "AAPL", "comparisons": [str(i) for i in range(7)]},
    {"anchor": "AAPL", "comparisons": ["SPY"], "period": "1Y"},
    {"anchor": "AAPL", "comparisons": ["SPY"], "anchor_return_pct": 99},
    {"anchor": "AAPL", "comparisons": ["SPY"], "evidence": {"P1": "trust me"}},
    {"anchor": "AAPL", "comparisons": ["SPY"], "reporting_currency": "EUR"},
])
def test_http_rejects_bad_selections_and_browser_evidence(payload: dict) -> None:
    fetcher = _Fetcher()
    with _client(fetcher) as client:
        assert client.post("/api/market-context/briefing-evidence", json=payload).status_code == 422
    assert fetcher.calls == []


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "otui_notes_only"}])
def test_http_requires_user_authentication_before_history_access(headers: dict) -> None:
    fetcher = _Fetcher()
    with _client(fetcher, authenticated=False) as client:
        response = client.post("/api/market-context/briefing-evidence", headers=headers, json={
            "anchor": "AAPL", "comparisons": ["SPY"],
        })
    assert response.status_code in (401, 403)
    assert fetcher.calls == []


def test_http_deadline_cancels_preparation_without_partial_packet(monkeypatch) -> None:
    cancelled: list[bool] = []

    async def slow_comparison(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(market_briefing, "compare_market_context", slow_comparison)
    monkeypatch.setattr(market_briefing, "EVIDENCE_TIMEOUT_SECONDS", 0.001)
    with _client(_Fetcher()) as client:
        response = client.post("/api/market-context/briefing-evidence", json={
            "anchor": "AAPL", "comparisons": ["SPY"],
        })
    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "evidence_timed_out"
    assert "packet_id" not in response.json()
    assert cancelled == [True]
