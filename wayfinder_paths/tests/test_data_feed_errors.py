"""Data-feed failure surface: one polite 429 retry at the client, and
structured out_of_credits / rate_limited causes so journals and the decision
log say the actionable truth instead of a generic exception string."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from wayfinder_paths.core.clients.HyperliquidDataClient import (
    DataFeedError,
    HyperliquidDataClient,
)


def _client_with_responses(responses: list[httpx.Response]) -> HyperliquidDataClient:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        resp = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return resp

    client = HyperliquidDataClient()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._calls = calls  # type: ignore[attr-defined]
    return client


def test_429_retries_once_after_retry_after() -> None:
    client = _client_with_responses(
        [
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"rows": [{"t": 1, "c": "1.0"}]}),
        ]
    )
    rows = asyncio.run(client.get_candles("BTC", start_ms=0, end_ms=1))
    assert rows == [{"t": 1, "c": "1.0"}]
    assert client._calls["n"] == 2  # type: ignore[attr-defined]


def test_429_exhausted_surfaces_rate_limited() -> None:
    client = _client_with_responses([httpx.Response(429, headers={"Retry-After": "0"})])
    with pytest.raises(DataFeedError) as err:
        asyncio.run(client.get_candles("BTC", start_ms=0, end_ms=1))
    assert err.value.cause == "rate_limited"
    assert "rate_limited" in str(err.value)
    # One original + exactly one retry — no hammering.
    assert client._calls["n"] == 2  # type: ignore[attr-defined]


def test_402_surfaces_out_of_credits() -> None:
    client = _client_with_responses([httpx.Response(402)])
    with pytest.raises(DataFeedError) as err:
        asyncio.run(client.get_candles("BTC", start_ms=0, end_ms=1))
    assert err.value.cause == "out_of_credits"
    assert "out_of_credits" in str(err.value)
    assert client._calls["n"] == 1  # type: ignore[attr-defined]
