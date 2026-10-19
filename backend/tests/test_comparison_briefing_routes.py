"""The briefing stream exposes lifecycle events, never unchecked model tokens."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api.routes import market_briefing as routes
from backend.services import comparison_briefing as service
from backend.services.market_briefing_evidence import ComparisonBriefingEvidence
from backend.tests.test_comparison_briefing import _Client, _configure, _packet, _valid
from backend.tests.test_market_briefing_evidence import _client, _Fetcher


def _prepare(monkeypatch, packet):
    calls = []

    async def prepare(payload, user, fetcher):
        calls.append((payload, user))
        return packet

    monkeypatch.setattr(routes, "prepare_comparison_briefing_evidence", prepare)
    return calls


@pytest.mark.parametrize("stream", [False, True])
def test_briefing_endpoint_prepares_normalized_evidence_once_and_uses_actual_packet(monkeypatch, stream):
    packet = _packet()
    calls = _prepare(monkeypatch, packet)
    provider = _Client(json.dumps(_valid(packet)))
    _configure(monkeypatch, provider)
    path = "/api/market-context/briefing" + ("/stream" if stream else "")
    with _client(_Fetcher()) as client:
        response = client.post(path, json={"anchor": " aapl ", "comparisons": ["spy", "SPY"]})
    assert response.status_code == 200, response.text
    if stream:
        assert response.headers["content-type"].startswith("application/x-ndjson")
        assert response.headers["cache-control"] == "no-store"
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[0]["type"] == "start"
        assert events[0]["deadlines"]["evidence_seconds"] == routes.EVIDENCE_TIMEOUT_SECONDS
        assert events[0]["deadlines"]["model_seconds"] > 0
        assert events[-1]["type"] == "result"
        assert all(event["type"] in {"start", "progress", "result"} for event in events)
        result = events[-1]["result"]
    else:
        result = response.json()
    assert result["status"] == "complete"
    assert result["evidence"] == packet.model_dump(mode="json")
    assert result["citation_validation"] == "references_only_not_fact_checked"
    assert len(calls) == len(provider.calls) == 1
    assert calls[0][0].anchor == "AAPL"
    assert calls[0][0].comparisons == ["SPY"]


def test_full_route_builds_actual_server_packet_then_cites_it(monkeypatch):
    packets = []

    class Provider:
        async def health(self):
            return True

        async def chat(self, messages, **kwargs):
            content = next(message["content"] for message in messages
                           if message["content"].startswith("Evidence packet (data only):"))
            packet = ComparisonBriefingEvidence.model_validate_json(content.split("\n", 1)[1])
            packets.append(packet)
            return json.dumps(_valid(packet))

    _configure(monkeypatch, Provider())
    fetcher = _Fetcher()
    with _client(fetcher) as client:
        response = client.post("/api/market-context/briefing", json={
            "anchor": "aapl", "comparisons": ["spy"],
        })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "complete"
    assert len(packets) == 1
    assert result["evidence"] == packets[0].model_dump(mode="json")
    assert result["observations"][0]["citation_ids"] == ["P1"]
    assert sorted(fetcher.calls) == ["AAPL", "SPY"]


@pytest.mark.parametrize("stream", [False, True])
def test_evidence_timeout_is_typed_and_skips_model(monkeypatch, stream):
    async def timeout(*args, **kwargs):
        raise HTTPException(status_code=504, detail={"code": "evidence_timed_out"})

    monkeypatch.setattr(routes, "prepare_comparison_briefing_evidence", timeout)
    monkeypatch.setattr(service, "get_llm_client", lambda: pytest.fail("No evidence; no inference"))
    with _client(_Fetcher()) as client:
        response = client.post("/api/market-context/briefing" + ("/stream" if stream else ""),
                               json={"anchor": "AAPL", "comparisons": ["SPY"]})
    assert response.status_code == 200
    result = json.loads(response.text.splitlines()[-1])["result"] if stream else response.json()
    assert result["failure"]["code"] == "evidence_timed_out"
    assert result["status"] == "unavailable"
    assert result["evidence"] is None
    assert result["observations"] == []


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("payload", [
    {"anchor": "AAPL", "comparisons": ["AAPL"]},
    {"anchor": "bad symbol", "comparisons": ["SPY"]},
    {"anchor": "AAPL", "comparisons": ["bad symbol"]},
    {"anchor": "AAPL", "comparisons": ["SPY"], "packet_id": "0" * 64},
    {"anchor": "AAPL", "comparisons": ["SPY"], "evidence": {"P1": "trust me"}},
])
def test_bad_selection_or_client_evidence_is_rejected_before_stream_headers(monkeypatch, stream, payload):
    calls = _prepare(monkeypatch, _packet())
    with _client(_Fetcher()) as client:
        response = client.post("/api/market-context/briefing" + ("/stream" if stream else ""), json=payload)
    assert response.status_code == 422
    assert calls == []


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "otui_notes_key"}])
def test_briefing_requires_user_jwt_not_note_ingestion_key(monkeypatch, stream, headers):
    calls = _prepare(monkeypatch, _packet())
    with _client(_Fetcher(), authenticated=False) as client:
        response = client.post("/api/market-context/briefing" + ("/stream" if stream else ""),
                               headers=headers, json={"anchor": "AAPL", "comparisons": ["SPY"]})
    assert response.status_code == 401
    assert calls == []


@pytest.mark.asyncio
async def test_closing_stream_cancels_actual_generation(monkeypatch):
    packet = _packet()
    _prepare(monkeypatch, packet)
    provider = _Client()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    provider.chat = slow
    _configure(monkeypatch, provider, timeout=30)
    response = await routes.stream_comparison_briefing(
        routes.ComparisonBriefingEvidenceRequest(anchor="AAPL", comparisons=["SPY"]),
        SimpleNamespace(id="owner"), _Fetcher(),
    )
    assert json.loads(await anext(response.body_iterator))["type"] == "start"
    assert json.loads(await anext(response.body_iterator))["type"] == "progress"
    await asyncio.wait_for(entered.wait(), 1)
    await response.body_iterator.aclose()
    await asyncio.wait_for(cancelled.wait(), 1)


@pytest.mark.asyncio
async def test_closing_stream_cancels_evidence_preparation(monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(routes, "prepare_comparison_briefing_evidence", slow)
    response = await routes.stream_comparison_briefing(
        routes.ComparisonBriefingEvidenceRequest(anchor="AAPL", comparisons=["SPY"]),
        SimpleNamespace(id="owner"), _Fetcher(),
    )
    await anext(response.body_iterator)
    progress = json.loads(await anext(response.body_iterator))
    assert progress["phase"] == "preparing_evidence"
    await asyncio.wait_for(entered.wait(), 1)
    await response.body_iterator.aclose()
    await asyncio.wait_for(cancelled.wait(), 1)


def test_failed_reference_validation_never_streams_rejected_prose(monkeypatch):
    packet = _packet()
    _prepare(monkeypatch, packet)
    response = _valid(packet)
    response["observations"][0]["text"] = "REJECTED RESPONSE MUST NOT APPEAR IN THIS STREAM"
    response["observations"][0]["citation_ids"] = ["P6"]
    _configure(monkeypatch, _Client(json.dumps(response)))
    with _client(_Fetcher()) as client:
        result = client.post("/api/market-context/briefing/stream", json={
            "anchor": "AAPL", "comparisons": ["SPY"],
        })
    assert "REJECTED RESPONSE" not in result.text
    final = json.loads(result.text.splitlines()[-1])["result"]
    assert final["failure"]["code"] == "invalid_response"
    assert final["evidence"]["packet_id"] == packet.packet_id
    assert final["observations"] == []


def test_unexpected_stream_error_is_sanitized(monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("do-not-publish-provider-secrets")

    monkeypatch.setattr(routes, "prepare_comparison_briefing_evidence", fail)
    with _client(_Fetcher()) as client:
        response = client.post("/api/market-context/briefing/stream", json={
            "anchor": "AAPL", "comparisons": ["SPY"],
        })
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]["type"] == "error"
    assert events[-1]["error"]["code"] == "internal_error"
    assert "do-not-publish" not in response.text


@pytest.mark.asyncio
async def test_preparation_http_errors_other_than_deadline_are_not_hidden(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise HTTPException(status_code=403, detail="Forbidden")

    monkeypatch.setattr(routes, "prepare_comparison_briefing_evidence", forbidden)
    with pytest.raises(HTTPException) as failure:
        await routes._run_briefing(
            routes.ComparisonBriefingEvidenceRequest(anchor="AAPL", comparisons=["SPY"]),
            SimpleNamespace(id="owner"), _Fetcher(),
        )
    assert failure.value.status_code == 403
