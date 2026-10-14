"""Current-vintage euro-area macro context from official public feeds."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
from datetime import date, datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

EUROSTAT_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
ECB_URL = "https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.DFR.LEV"
ECB_SERIES = "FM.D.U2.EUR.4F.KR.DFR.LEV"
UNEMPLOYMENT_VINTAGE_SERIES = "ei_lm_m_vtg"
HICP_FIRST_PUBLISHED_SERIES = "prc_hicp_fpd"
HICP_FIRST_PUBLISHED_FILTERS = {"geo": "EA21", "coicop18": "TOTAL", "unit": "RCH_A"}
UNEMPLOYMENT_VINTAGE_FILTERS = {
    "geo": "EA", "unit": "PC_ACT", "s_adj": "SA", "sex": "T", "age": "TOTAL",
}

SERIES = (
    ("prc_hicp_minr", "Euro-area HICP, annual change", "eurostat", "Monthly", "Percent", {
        "geo": "EA21", "coicop18": "TOTAL", "unit": "RCH_A",
    }),
    ("une_rt_m", "Euro-area unemployment", "eurostat", "Monthly", "Percent of labour force", {
        "geo": "EA21", "s_adj": "SA", "age": "TOTAL", "unit": "PC_ACT", "sex": "T",
    }),
    (ECB_SERIES, "ECB deposit facility rate", "ecb", "Daily", "Percent per annum", {}),
)


def _month_start(value: str) -> date:
    if len(value) != 7 or value[4] != "-":
        raise ValueError("Invalid monthly period")
    return date.fromisoformat(f"{value}-01")


def _finite(value: Any) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Non-finite observation")
    return parsed


def _eurostat_observations(data: Any, filters: dict[str, str], start: date, end: date) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("dimension"), dict):
        raise ValueError("Invalid Eurostat response")
    dimensions = data["dimension"]
    ids, sizes = data.get("id"), data.get("size")
    if not isinstance(ids, list) or not isinstance(sizes, list) or len(ids) != len(sizes):
        raise ValueError("Invalid Eurostat dimensions")
    if set(ids) != {"freq", *filters, "time"} or any(size != 1 for dim, size in zip(ids, sizes) if dim != "time"):
        raise ValueError("Unexpected Eurostat selection")
    for name, code in {"freq": "M", **filters}.items():
        if dimensions.get(name, {}).get("category", {}).get("index") != {code: 0}:
            raise ValueError("Unexpected Eurostat series identity")
    time_index = dimensions.get("time", {}).get("category", {}).get("index")
    values = data.get("value")
    if not isinstance(time_index, dict) or not isinstance(values, dict):
        raise ValueError("Invalid Eurostat observations")
    statuses = data.get("status") or {}
    if not isinstance(statuses, dict):
        raise ValueError("Invalid Eurostat observation flags")
    observations = []
    for period, index in time_index.items():
        month = _month_start(period)
        # A monthly observation belongs to its reference month, even if the
        # pair window starts after the first day of that month.
        if (month.year == start.year and month.month == start.month) or start <= month <= end:
            raw = values.get(str(index))
            if raw is not None:
                flag = statuses.get(str(index))
                if flag is not None and not isinstance(flag, str):
                    raise ValueError("Invalid Eurostat observation flag")
                observations.append({"reference_date": month, "value": _finite(raw),
                                     "flag": flag})
    return sorted(observations, key=lambda row: row["reference_date"])


def _ecb_observations(body: str, start: date, end: date) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(body))
    if not reader.fieldnames or not {"KEY", "TIME_PERIOD", "OBS_VALUE", "UNIT"} <= set(reader.fieldnames):
        raise ValueError("Invalid ECB CSV schema")
    values: dict[date, float] = {}
    for row in reader:
        if row["KEY"] != ECB_SERIES or row["UNIT"] != "PCPA":
            raise ValueError("Unexpected ECB series identity or unit")
        observed_on = date.fromisoformat(row["TIME_PERIOD"])
        if start <= observed_on <= end and row["OBS_VALUE"]:
            value = _finite(row["OBS_VALUE"])
            if observed_on in values and values[observed_on] != value:
                raise ValueError("Conflicting ECB observations")
            values[observed_on] = value
    return [{"reference_date": day, "value": value, "flag": None} for day, value in sorted(values.items())]


async def get_euro_area_macro_observations(start: date, end: date) -> dict[str, Any]:
    """Return reference-period data, never sample or point-in-time claims."""
    retrieved_at = datetime.now(timezone.utc)

    async def fetch(client: httpx.AsyncClient, spec: tuple) -> dict[str, Any]:
        series_id, label, source, frequency, units, filters = spec
        if source == "eurostat":
            response = await client.get(f"{EUROSTAT_URL}/{series_id}", params={
                **filters, "sinceTimePeriod": start.strftime("%Y-%m"),
                "untilTimePeriod": end.strftime("%Y-%m"),
            })
            response.raise_for_status()
            observations = _eurostat_observations(response.json(), filters, start, end)
        else:
            response = await client.get(ECB_URL, params={
                "startPeriod": start.isoformat(), "endPeriod": end.isoformat(), "format": "csvdata",
            })
            response.raise_for_status()
            observations = _ecb_observations(response.text, start, end)
        return {"series_id": series_id, "label": label, "source": source, "units": units,
                "frequency": frequency, "status": "available" if observations else "no_observations",
                "observations": observations}

    async with httpx.AsyncClient(timeout=10.0) as client:
        results = await asyncio.gather(*(fetch(client, spec) for spec in SERIES), return_exceptions=True)
    groups = []
    for spec, result in zip(SERIES, results):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                raise result
            logger.warning("Euro-area macro context failed for %s (%s)", spec[0], type(result).__name__)
            groups.append({"series_id": spec[0], "label": spec[1], "source": spec[2],
                           "units": spec[4], "frequency": spec[3], "status": "feed_error", "observations": []})
        else:
            groups.append(result)
    available = any(group["status"] != "feed_error" for group in groups)
    return {"retrieved_at": retrieved_at, "region": "EA21", "status": "available" if available else "unavailable",
            "reason": None if available else "provider_error", "series": groups}


async def get_euro_area_hicp_first_published(start: date, end: date) -> dict[str, Any]:
    """Read Eurostat's current first-published archive, not release timestamps."""
    retrieved_at = datetime.now(timezone.utc)

    async def fetch(client: httpx.AsyncClient, release: str) -> dict[str, Any]:
        filters = {**HICP_FIRST_PUBLISHED_FILTERS, "release": release}
        response = await client.get(f"{EUROSTAT_URL}/{HICP_FIRST_PUBLISHED_SERIES}", params={
            **filters, "sinceTimePeriod": start.strftime("%Y-%m"),
            "untilTimePeriod": end.strftime("%Y-%m"),
        })
        response.raise_for_status()
        observations = _eurostat_observations(response.json(), filters, start, end)
        return {"release": release, "status": "available" if observations else "no_observations",
                "observations": observations}

    releases = ("FIN", "FLS")
    async with httpx.AsyncClient(timeout=10.0) as client:
        results = await asyncio.gather(*(fetch(client, release) for release in releases), return_exceptions=True)
    groups = []
    for release, result in zip(releases, results):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                raise result
            logger.warning("Euro-area HICP first-published %s failed (%s)", release, type(result).__name__)
            groups.append({"release": release, "status": "feed_error", "observations": []})
        else:
            groups.append(result)
    available = any(group["status"] != "feed_error" for group in groups)
    return {"retrieved_at": retrieved_at, "status": "available" if available else "unavailable",
            "reason": None if available else "provider_error", "releases": groups}


def _unemployment_vintage_rows(data: Any, start: date, end: date, as_of: date) -> list[dict[str, Any]]:
    """Select the latest archived release no later than as_of per reference month.

    Eurostat stores only changed vintages, so absent later revision dates do not
    make an earlier observation disappear. This reconstructs from the archive
    currently available, not from what the archive exposed on the as-of date.
    """
    if not isinstance(data, dict) or not isinstance(data.get("dimension"), dict):
        raise ValueError("Invalid Eurostat vintage response")
    ids, sizes = data.get("id"), data.get("size")
    expected = {"freq", *UNEMPLOYMENT_VINTAGE_FILTERS, "revdate", "time"}
    if not isinstance(ids, list) or not isinstance(sizes, list) or len(ids) != len(sizes) or set(ids) != expected:
        raise ValueError("Invalid Eurostat vintage dimensions")
    if any(not isinstance(size, int) or size < 0 for size in sizes):
        raise ValueError("Invalid Eurostat vintage shape")
    dimensions = data["dimension"]
    for name, code in {"freq": "M", **UNEMPLOYMENT_VINTAGE_FILTERS}.items():
        if sizes[ids.index(name)] != 1 or dimensions.get(name, {}).get("category", {}).get("index") != {code: 0}:
            raise ValueError("Unexpected Eurostat vintage identity")
    rev_index = dimensions.get("revdate", {}).get("category", {}).get("index")
    time_index = dimensions.get("time", {}).get("category", {}).get("index")
    values = data.get("value")
    if not isinstance(rev_index, dict) or not isinstance(time_index, dict) or not isinstance(values, dict):
        raise ValueError("Invalid Eurostat vintage observations")
    statuses = data.get("status") or {}
    if not isinstance(statuses, dict):
        raise ValueError("Invalid Eurostat vintage flags")
    revisions = {position: date.fromisoformat(raw) for raw, position in rev_index.items()}
    months = {position: _month_start(raw) for raw, position in time_index.items()}
    if len(revisions) != sizes[ids.index("revdate")] or len(months) != sizes[ids.index("time")]:
        raise ValueError("Invalid Eurostat vintage axis")
    if any(not isinstance(position, int) for position in (*revisions, *months)):
        raise ValueError("Invalid Eurostat vintage positions")

    strides: dict[str, int] = {}
    stride = 1
    for name, size in reversed(list(zip(ids, sizes))):
        strides[name] = stride
        stride *= size
    chosen: dict[date, dict[str, Any]] = {}
    for raw_position, raw_value in values.items():
        position = int(raw_position)
        if not 0 <= position < stride:
            raise ValueError("Eurostat vintage value outside dimensions")
        rev_position = (position // strides["revdate"]) % sizes[ids.index("revdate")]
        time_position = (position // strides["time"]) % sizes[ids.index("time")]
        revision, month = revisions[rev_position], months[time_position]
        if revision > as_of or not ((month.year, month.month) == (start.year, start.month) or start <= month <= end):
            continue
        value = _finite(raw_value)
        flag = statuses.get(raw_position)
        if flag is not None and not isinstance(flag, str):
            raise ValueError("Invalid Eurostat vintage flag")
        previous = chosen.get(month)
        if previous is None or revision > previous["vintage_date"]:
            chosen[month] = {"reference_date": month, "value": value, "vintage_date": revision, "flag": flag}
        elif revision == previous["vintage_date"] and value != previous["value"]:
            raise ValueError("Conflicting Eurostat vintage")
    return [chosen[month] for month in sorted(chosen)]


async def get_euro_area_unemployment_vintage(start: date, end: date, as_of: date) -> dict[str, Any]:
    """Check the currently available Eurostat archive; never fabricate gaps."""
    retrieved_at = datetime.now(timezone.utc)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{EUROSTAT_URL}/{UNEMPLOYMENT_VINTAGE_SERIES}", params={
                **UNEMPLOYMENT_VINTAGE_FILTERS,
                "sinceTimePeriod": start.strftime("%Y-%m"),
                "untilTimePeriod": end.strftime("%Y-%m"),
            })
            response.raise_for_status()
            observations = _unemployment_vintage_rows(response.json(), start, end, as_of)
    except Exception as exc:
        logger.warning("Euro-area unemployment vintage failed (%s)", type(exc).__name__)
        return {"retrieved_at": retrieved_at, "status": "unavailable", "reason": "provider_error", "observations": []}
    return {"retrieved_at": retrieved_at, "status": "available" if observations else "no_vintages",
            "reason": None, "observations": observations}
