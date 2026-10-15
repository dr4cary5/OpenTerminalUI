"""Dated, pairwise market comparisons without inferred causality."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from backend.api.routes.chart import _parse_yahoo_chart

PERIOD_DAYS = {"1M": 30, "3M": 90, "6M": 180}
FETCH_RANGES = {"1M": "3mo", "3M": "6mo", "6M": "1y"}


def yahoo_reported_quote_unit(raw: Any) -> str | None:
    """Keep the selected chart's quote unit without treating it as ISO FX."""
    if not isinstance(raw, dict):
        return None
    chart = raw.get("chart")
    results = chart.get("result") if isinstance(chart, dict) else None
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return None
    meta = results[0].get("meta")
    unit = meta.get("currency") if isinstance(meta, dict) else None
    if not isinstance(unit, str):
        return None
    unit = unit.strip()
    return unit if re.fullmatch(r"[A-Za-z0-9]{2,12}", unit) else None


def yahoo_exchange_date_disclosure(raw: Any, accepted: dict[date, float]) -> dict[str, Any]:
    """Compare accepted UTC bar dates with chart-reported exchange-local dates.

    This only discloses date-label divergence; it does not infer sessions or
    alter the primary UTC-date comparison.
    """
    unavailable = {"status": "unavailable", "exchange_timezone": None, "checked_utc_dates": 0,
                   "shifted_utc_date_count": 0, "ambiguous_utc_date_count": 0, "examples": []}
    if not isinstance(raw, dict) or not accepted:
        return unavailable
    chart = raw.get("chart")
    results = chart.get("result") if isinstance(chart, dict) else None
    result = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
    meta = result.get("meta") if result else None
    name = meta.get("exchangeTimezoneName") if isinstance(meta, dict) else None
    if not isinstance(name, str):
        return unavailable
    try:
        exchange_zone = ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return unavailable
    frame = _parse_yahoo_chart(raw)
    if frame.empty or "Close" not in frame:
        return unavailable
    local_dates: dict[date, set[date]] = {}
    for timestamp, value in frame["Close"].items():
        try:
            moment = pd.Timestamp(timestamp)
            utc_day = moment.date()
            if utc_day not in accepted or float(value) != accepted[utc_day]:
                continue
            local_day = moment.tz_convert(exchange_zone).date()
        except (TypeError, ValueError, OverflowError):
            continue
        local_dates.setdefault(utc_day, set()).add(local_day)
    shifted = [(utc_day, next(iter(days))) for utc_day, days in local_dates.items()
               if len(days) == 1 and next(iter(days)) != utc_day]
    ambiguous = sum(len(days) > 1 for days in local_dates.values())
    return {"status": "available", "exchange_timezone": name, "checked_utc_dates": len(local_dates),
            "shifted_utc_date_count": len(shifted), "ambiguous_utc_date_count": ambiguous,
            "examples": [{"utc_date": utc_day, "exchange_local_date": local_day}
                         for utc_day, local_day in sorted(shifted, reverse=True)[:5]]}


def yahoo_exchange_local_closes(raw: Any, accepted: dict[date, float]) -> dict[str, Any]:
    """Relabel accepted Yahoo closes without claiming verified trading sessions.

    Ambiguous UTC mappings and local dates reached by multiple UTC dates are
    withheld, even when their prices agree.
    """
    unavailable = {"status": "unavailable", "exchange_timezone": None, "closes": {}, "utc_by_local": {},
                   "ambiguous_utc_dates": 0, "colliding_local_dates": 0}
    if not isinstance(raw, dict) or not accepted:
        return unavailable
    chart = raw.get("chart")
    results = chart.get("result") if isinstance(chart, dict) else None
    result = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
    meta = result.get("meta") if result else None
    name = meta.get("exchangeTimezoneName") if isinstance(meta, dict) else None
    if not isinstance(name, str):
        return unavailable
    try:
        zone = ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return unavailable
    frame = _parse_yahoo_chart(raw)
    if frame.empty or "Close" not in frame:
        return unavailable
    utc_to_local: dict[date, set[date]] = {}
    for timestamp, value in frame["Close"].items():
        try:
            moment = pd.Timestamp(timestamp)
            utc_day = moment.date()
            if utc_day not in accepted or float(value) != accepted[utc_day]:
                continue
            local_day = moment.tz_convert(zone).date()
        except (TypeError, ValueError, OverflowError):
            continue
        utc_to_local.setdefault(utc_day, set()).add(local_day)
    local_to_utc: dict[date, set[date]] = {}
    for utc_day, local_days in utc_to_local.items():
        if len(local_days) == 1:
            local_to_utc.setdefault(next(iter(local_days)), set()).add(utc_day)
    collisions = {day for day, utc_days in local_to_utc.items() if len(utc_days) > 1}
    closes = {day: accepted[next(iter(utc_days))] for day, utc_days in local_to_utc.items()
              if day not in collisions}
    utc_by_local = {day: next(iter(utc_days)) for day, utc_days in local_to_utc.items()
                    if day not in collisions}
    return {"status": "available", "exchange_timezone": name, "closes": closes,
            "utc_by_local": utc_by_local,
            "ambiguous_utc_dates": sum(len(days) > 1 for days in utc_to_local.values()),
            "colliding_local_dates": len(collisions)}


def yahoo_exchange_local_adjusted_closes(
    raw: Any, local: dict[str, Any], accepted_adjusted: dict[date, float],
) -> dict[date, float]:
    """Take adjusted values from the accepted close rows, not merely their UTC date."""
    if local["status"] != "available" or not accepted_adjusted:
        return {}
    result = raw["chart"]["result"][0]
    timestamps = result.get("timestamp")
    indicators = result.get("indicators")
    quote = indicators.get("quote") if isinstance(indicators, dict) else None
    adjusted = indicators.get("adjclose") if isinstance(indicators, dict) else None
    if not (isinstance(timestamps, list) and isinstance(quote, list) and quote
            and isinstance(quote[0], dict) and isinstance(adjusted, list) and adjusted
            and isinstance(adjusted[0], dict)):
        return {}
    fields = [quote[0].get(key) for key in ("open", "high", "low", "close")]
    values = adjusted[0].get("adjclose")
    if any(not isinstance(field, list) or len(field) != len(timestamps) for field in fields):
        return {}
    if not isinstance(values, list):
        return {}
    try:
        zone = ZoneInfo(local["exchange_timezone"])
    except (ValueError, ZoneInfoNotFoundError):
        return {}
    candidates: dict[date, float] = {}
    invalid: set[date] = set()
    for index, timestamp in enumerate(timestamps):
        try:
            moment = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
            utc_day = moment.date()
            local_day = moment.astimezone(zone).date()
            if local["utc_by_local"].get(local_day) != utc_day:
                continue
            ohlc = [field[index] for field in fields]
            if None in ohlc or float(ohlc[-1]) != local["closes"][local_day]:
                continue
            try:
                value = float(values[index])
            except (TypeError, ValueError, IndexError):
                invalid.add(local_day)
                continue
            if not math.isfinite(value) or value <= 0 or accepted_adjusted.get(utc_day) != value:
                invalid.add(local_day)
                continue
        except (OSError, OverflowError, TypeError, ValueError, IndexError):
            continue
        if local_day in candidates and candidates[local_day] != value:
            invalid.add(local_day)
        else:
            candidates[local_day] = value
    return {day: value for day, value in candidates.items() if day not in invalid}


def exchange_local_pair_comparison(anchor: dict[str, Any], comparison: dict[str, Any],
                                   *, period: str) -> dict[str, Any]:
    """An independent calendar-date view; never substitute for UTC alignment."""
    base = {"basis": "shared_exchange_local_calendar_date_provider_closes",
            "anchor_exchange_timezone": anchor["exchange_timezone"],
            "comparison_exchange_timezone": comparison["exchange_timezone"],
            "anchor_ambiguous_utc_dates": anchor["ambiguous_utc_dates"],
            "comparison_ambiguous_utc_dates": comparison["ambiguous_utc_dates"],
            "anchor_colliding_local_dates": anchor["colliding_local_dates"],
            "comparison_colliding_local_dates": comparison["colliding_local_dates"]}
    if anchor["status"] != "available" or comparison["status"] != "available":
        return {**base, "status": "unavailable", "reason": "timezone_unavailable"}
    result = compare_closes(anchor["closes"], comparison["closes"], period=period)
    if result["status"] != "available":
        return {**base, "status": "unavailable", "reason": result["reason"]}
    return {**base, "status": "available", "reason": None,
            **{key: result[key] for key in ("start_date", "end_date", "observations",
                                               "anchor_return_pct", "comparison_return_pct",
                                               "relative_return_pp", "points")}}


def exchange_local_adjusted_comparison(
    local_anchor: dict[str, Any], local_comparison: dict[str, Any],
    adjusted_anchor: dict[date, float], adjusted_comparison: dict[date, float],
    local_pair: dict[str, Any],
) -> dict[str, Any] | None:
    """Require adjusted closes on every selected local pair date, never a subset."""
    if local_pair["status"] != "available":
        return None
    shared = [date.fromisoformat(point["date"]) for point in local_pair["points"]]

    def covered(adjusted: dict[date, float]) -> tuple[dict[date, float], dict[str, Any]]:
        values = {day: adjusted[day] for day in shared if day in adjusted}
        count = len(values)
        return values, {
            "status": "complete" if count == len(shared) else "partial" if count else "unavailable",
            "source": "yahoo_adjclose" if count else None,
            "available_observations": count,
            "shared_observations": len(shared),
        }

    anchor, anchor_coverage = covered(adjusted_anchor)
    comparison, comparison_coverage = covered(adjusted_comparison)
    result = adjusted_pair_comparison(
        anchor, comparison, shared,
        provider_anchor=local_anchor["closes"], provider_comparison=local_comparison["closes"],
    )
    if result is not None:
        result["basis"] = "shared_exchange_local_calendar_date_provider_adjusted_closes"
    return {"coverage": {"anchor": anchor_coverage, "comparison": comparison_coverage},
            "adjusted_pair": result}


def _record_daily_close(
    closes: dict[date, float], conflicts: set[date], day: date, close: float
) -> None:
    if day in conflicts:
        return
    if day in closes and closes[day] != close:
        conflicts.add(day)
        del closes[day]
    else:
        closes[day] = close


def yahoo_adjusted_closes_with_conflicts(raw: Any) -> tuple[dict[date, float], set[date]]:
    """Read provider-supplied adjusted closes without assuming their vintage."""
    if not isinstance(raw, dict):
        return {}, set()
    chart = raw.get("chart")
    if not isinstance(chart, dict):
        return {}, set()
    results = chart.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return {}, set()
    result = results[0]
    timestamps = result.get("timestamp")
    indicators = result.get("indicators")
    if not isinstance(timestamps, list) or not isinstance(indicators, dict):
        return {}, set()
    adjusted = indicators.get("adjclose")
    if not isinstance(adjusted, list) or not adjusted or not isinstance(adjusted[0], dict):
        return {}, set()
    values = adjusted[0].get("adjclose")
    if not isinstance(values, list):
        return {}, set()
    closes: dict[date, float] = {}
    conflicts: set[date] = set()
    for timestamp, value in zip(timestamps, values):
        try:
            day = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).date()
            close = float(value)
        except (OSError, OverflowError, TypeError, ValueError):
            continue
        if math.isfinite(close) and close > 0:
            _record_daily_close(closes, conflicts, day, close)
    return closes, conflicts


def yahoo_adjusted_closes(raw: Any) -> dict[date, float]:
    return yahoo_adjusted_closes_with_conflicts(raw)[0]


def yahoo_action_metadata_present(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    chart = raw.get("chart")
    if not isinstance(chart, dict):
        return False
    results = chart.get("result")
    return (
        isinstance(results, list)
        and bool(results)
        and isinstance(results[0], dict)
        and isinstance(results[0].get("events"), dict)
    )


def yahoo_reported_actions(raw: Any) -> list[dict[str, str]]:
    """Extract dated split/dividend markers from a Yahoo chart response only."""
    if not isinstance(raw, dict):
        return []
    chart = raw.get("chart")
    if not isinstance(chart, dict):
        return []
    results = chart.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return []
    events = results[0].get("events") or {}
    if not isinstance(events, dict):
        return []
    actions: set[tuple[str, str]] = set()
    for event_key, action_type in (("splits", "split"), ("dividends", "dividend")):
        group = events.get(event_key) or {}
        if not isinstance(group, dict):
            continue
        for key, item in group.items():
            if not isinstance(item, dict):
                continue
            try:
                timestamp = int(item.get("date") or key)
                day = datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
            except (OSError, OverflowError, TypeError, ValueError):
                continue
            actions.add((day, action_type))
    return [{"date": day, "type": action_type} for day, action_type in sorted(actions, reverse=True)]


def _technical_measures(closes: dict[date, float], shared: list[date]) -> dict[str, Any]:
    """Descriptive measures on the exact observed dates used by a pair."""
    peak = closes[shared[0]]
    peak_date = shared[0]
    worst_drawdown = 0.0
    worst_peak_date: date | None = None
    worst_trough_date: date | None = None
    for day in shared[1:]:
        close = closes[day]
        if close > peak:
            peak, peak_date = close, day
        drawdown = (peak - close) / peak * 100.0
        if drawdown > worst_drawdown:
            worst_drawdown = drawdown
            worst_peak_date, worst_trough_date = peak_date, day

    sma20_gap: float | None = None
    if len(shared) >= 20:
        mean = sum(closes[day] for day in shared[-20:]) / 20.0
        sma20_gap = (closes[shared[-1]] / mean - 1.0) * 100.0
    return {
        "max_drawdown_pct": round(worst_drawdown, 4),
        "max_drawdown_peak_date": worst_peak_date.isoformat() if worst_peak_date else None,
        "max_drawdown_trough_date": worst_trough_date.isoformat() if worst_trough_date else None,
        "sma20_gap_pct": round(sma20_gap, 4) if sma20_gap is not None else None,
    }


def adjusted_observations(
    provider: dict[date, float], adjusted: dict[date, float], shared: list[date]
) -> dict[str, Any] | None:
    """Contrast complete adjusted and provider-close series on identical dates."""
    if len(shared) < 2 or any(day not in provider or day not in adjusted for day in shared):
        return None
    provider_return = (provider[shared[-1]] / provider[shared[0]] - 1.0) * 100.0
    adjusted_return = (adjusted[shared[-1]] / adjusted[shared[0]] - 1.0) * 100.0
    return {
        "return_pct": round(adjusted_return, 4),
        "provider_return_pct": round(provider_return, 4),
        "adjusted_minus_provider_return_pp": round(adjusted_return - provider_return, 4),
        "technical_measures": _technical_measures(adjusted, shared),
    }


def adjusted_pair_comparison(
    anchor: dict[date, float], comparison: dict[date, float], shared: list[date],
    *, provider_anchor: dict[date, float], provider_comparison: dict[date, float],
) -> dict[str, Any] | None:
    """Compare two complete adjusted series on the primary pair's exact dates."""
    if len(shared) < 2 or any(
        day not in anchor or day not in comparison or day not in provider_anchor or day not in provider_comparison
        for day in shared
    ):
        return None
    start, end = shared[0], shared[-1]
    anchor_return = (anchor[end] / anchor[start] - 1.0) * 100.0
    comparison_return = (comparison[end] / comparison[start] - 1.0) * 100.0
    provider_anchor_return = (provider_anchor[end] / provider_anchor[start] - 1.0) * 100.0
    provider_comparison_return = (provider_comparison[end] / provider_comparison[start] - 1.0) * 100.0
    provider_relative = provider_anchor_return - provider_comparison_return
    adjusted_relative = anchor_return - comparison_return
    return {
        "basis": "shared_utc_date_provider_adjusted_closes",
        "source": "yahoo_adjclose",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "observations": len(shared),
        "anchor_return_pct": round(anchor_return, 4),
        "comparison_return_pct": round(comparison_return, 4),
        "relative_return_pp": round(adjusted_relative, 4),
        "provider_relative_return_pp": round(provider_relative, 4),
        "adjusted_minus_provider_relative_return_pp": round(adjusted_relative - provider_relative, 4),
        "points": [
            {
                "date": day.isoformat(),
                "anchor_index": round(anchor[day] / anchor[start] * 100.0, 4),
                "comparison_index": round(comparison[day] / comparison[start] * 100.0, 4),
            }
            for day in shared
        ],
    }


def _native_technical_observations(
    closes: dict[date, float], shared: list[date]
) -> dict[str, Any]:
    """Measure one asset's own dated closes inside the pair's observed window."""
    start, end = shared[0], shared[-1]
    native = sorted(day for day in closes if start <= day <= end)
    return {
        "start_date": native[0].isoformat(),
        "end_date": native[-1].isoformat(),
        "observations": len(native),
        "additional_dates_vs_pair": len(set(native) - set(shared)),
        "technical_measures": _technical_measures(closes, native),
    }


def daily_closes_with_conflicts(raw: Any) -> tuple[dict[date, float], set[date]]:
    frame = _parse_yahoo_chart(raw if isinstance(raw, dict) else {})
    if frame.empty or "Close" not in frame:
        return {}, set()
    closes: dict[date, float] = {}
    conflicts: set[date] = set()
    for timestamp, value in frame["Close"].items():
        try:
            close = float(value)
            day = pd.Timestamp(timestamp).date()
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(close) and close > 0:
            _record_daily_close(closes, conflicts, day, close)
    return closes, conflicts


def _daily_closes(raw: Any) -> dict[date, float]:
    return daily_closes_with_conflicts(raw)[0]


def compare_closes(
    anchor: dict[date, float],
    comparison: dict[date, float],
    *,
    period: str,
    today: date | None = None,
) -> dict[str, Any]:
    """Compare closes on shared supplied dates; the primary caller supplies UTC dates."""
    if not anchor or not comparison:
        return {"status": "unavailable", "reason": "missing_history"}

    end = min(max(anchor), max(comparison))
    cutoff = end - timedelta(days=PERIOD_DAYS[period])
    shared = sorted(day for day in anchor.keys() & comparison.keys() if cutoff <= day <= end)
    if len(shared) < 2 or shared[0] > cutoff + timedelta(days=7):
        return {
            "status": "unavailable",
            "reason": "insufficient_overlap",
            "anchor_latest_date": max(anchor).isoformat(),
            "comparison_latest_date": max(comparison).isoformat(),
        }

    start, end = shared[0], shared[-1]
    anchor_return = (anchor[end] / anchor[start] - 1.0) * 100.0
    comparison_return = (comparison[end] / comparison[start] - 1.0) * 100.0
    points = [
        {
            "date": day.isoformat(),
            "anchor_index": round(anchor[day] / anchor[start] * 100.0, 4),
            "comparison_index": round(comparison[day] / comparison[start] * 100.0, 4),
        }
        for day in shared
    ]
    observed_today = today or datetime.now(timezone.utc).date()
    return {
        "status": "available",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "anchor_latest_date": max(anchor).isoformat(),
        "comparison_latest_date": max(comparison).isoformat(),
        "observations": len(shared),
        "freshness": "stale" if observed_today - end > timedelta(days=7) else "current",
        "anchor_return_pct": round(anchor_return, 4),
        "comparison_return_pct": round(comparison_return, 4),
        "relative_return_pp": round(anchor_return - comparison_return, 4),
        "technical_observations": {
            "basis": "shared_utc_date_provider_closes",
            "as_of_date": end.isoformat(),
            "anchor": _technical_measures(anchor, shared),
            "comparison": _technical_measures(comparison, shared),
        },
        "native_technical_observations": {
            "basis": "per_asset_utc_date_provider_closes_within_pair_window",
            "anchor": _native_technical_observations(anchor, shared),
            "comparison": _native_technical_observations(comparison, shared),
        },
        "points": points,
    }
