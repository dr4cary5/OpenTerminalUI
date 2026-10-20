"""No provider calls: reproduce the unsupported-symbol zero quote regression."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.api.routes import quotes
from backend.services import marketdata_hub


def fetcher():
    return SimpleNamespace(
        finnhub=SimpleNamespace(api_key="test", get_quote=AsyncMock()),
        yahoo=SimpleNamespace(get_quotes=AsyncMock()),
    )


@pytest.mark.parametrize("price", [0, None, -1, float("nan"), float("inf")])
def test_hub_does_not_publish_invalid_finnhub_prices(monkeypatch, price):
    f = fetcher()
    f.finnhub.get_quote.return_value = {"c": price, "d": None, "dp": None, "t": 0}
    monkeypatch.setattr(marketdata_hub, "get_unified_fetcher", AsyncMock(return_value=f))
    hub = marketdata_hub.MarketDataHub.__new__(marketdata_hub.MarketDataHub)
    assert asyncio.run(hub._fetch_ticks(["NASDAQ:BTC-USD"])) == {}


def test_hub_retains_real_equity_quotes(monkeypatch):
    f = fetcher()
    f.finnhub.get_quote.return_value = {"c": 205, "d": -2, "dp": -0.96, "t": 1792525572}
    monkeypatch.setattr(marketdata_hub, "get_unified_fetcher", AsyncMock(return_value=f))
    hub = marketdata_hub.MarketDataHub.__new__(marketdata_hub.MarketDataHub)
    out = asyncio.run(hub._fetch_ticks(["NASDAQ:AAPL"]))
    assert out["NASDAQ:AAPL"]["ltp"] == 205
    assert out["NASDAQ:AAPL"]["change"] == -2


def test_crypto_poll_fallback_uses_unsuffixed_yahoo_symbols(monkeypatch):
    f = fetcher()
    f.yahoo.get_quotes.return_value = [
        {"symbol": "BTC-USD", "regularMarketPrice": 82521.45, "regularMarketChangePercent": 0.92},
        {"symbol": "ETH-USD", "regularMarketPrice": 0},
    ]
    monkeypatch.setattr(marketdata_hub, "get_unified_fetcher", AsyncMock(return_value=f))
    hub = marketdata_hub.MarketDataHub.__new__(marketdata_hub.MarketDataHub)
    out = asyncio.run(hub._fetch_ticks(["CRYPTO:BTC-USD", "CRYPTO:ETH-USD"]))
    assert list(out) == ["CRYPTO:BTC-USD"]
    assert out["CRYPTO:BTC-USD"]["ltp"] == 82521.45
    f.yahoo.get_quotes.assert_awaited_once_with(["BTC-USD", "ETH-USD"])
    f.finnhub.get_quote.assert_not_called()


@pytest.mark.parametrize("price", [0, None, -1, float("nan"), float("inf")])
def test_quote_endpoint_rejects_finnhub_no_data(monkeypatch, price):
    f = fetcher()
    f.finnhub.get_quote.return_value = {"c": price, "d": None, "dp": None, "t": 0}
    registry = SimpleNamespace(invoke=AsyncMock(return_value=None))
    monkeypatch.setattr(quotes, "get_unified_fetcher", AsyncMock(return_value=f))
    monkeypatch.setattr(quotes, "get_adapter_registry", lambda: registry)
    assert asyncio.run(quotes.get_quotes(market="NASDAQ", symbols="BTC-USD"))["quotes"] == []


def test_crypto_quote_endpoint_accepts_adapter_quote(monkeypatch):
    f = fetcher()
    quote = SimpleNamespace(price=82521.45, change=535.14, change_pct=0.65, ts="2026-10-20T19:00:00Z")
    registry = SimpleNamespace(invoke=AsyncMock(return_value=quote))
    monkeypatch.setattr(quotes, "get_unified_fetcher", AsyncMock(return_value=f))
    monkeypatch.setattr(quotes, "get_adapter_registry", lambda: registry)
    out = asyncio.run(quotes.get_quotes(market="CRYPTO", symbols="BTC-USD"))
    assert out["quotes"][0]["last"] == 82521.45
    registry.invoke.assert_awaited_once_with("CRYPTO", "get_quote", "CRYPTO:BTC-USD")
    f.finnhub.get_quote.assert_not_called()


@pytest.mark.parametrize("adapter", [None, SimpleNamespace(price=0)])
def test_crypto_quote_failure_never_uses_equity_providers(monkeypatch, adapter):
    f = fetcher()
    f.yahoo.get_quotes.return_value = [{"symbol": "BTC-USD", "regularMarketPrice": 82521.45}]
    registry = SimpleNamespace(invoke=AsyncMock(return_value=adapter))
    monkeypatch.setattr(quotes, "get_unified_fetcher", AsyncMock(return_value=f))
    monkeypatch.setattr(quotes, "get_adapter_registry", lambda: registry)
    out = asyncio.run(quotes.get_quotes(market="CRYPTO", symbols="BTC-USD"))
    assert out["quotes"][0]["last"] == 82521.45
    f.yahoo.get_quotes.assert_awaited_once_with(["BTC-USD"])
    f.finnhub.get_quote.assert_not_called()
