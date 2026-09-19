"""The token client's candle calls send the window on the wire, in ms."""

from __future__ import annotations

import asyncio
import json

import httpx

from wayfinder_paths.core.clients import TokenClient as token_client_module
from wayfinder_paths.core.clients.TokenClient import TokenClient

ROW = {"t": 0, "T": 3_600_000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10"}


def _client(seen: list[httpx.Request], payload: dict) -> TokenClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=json.dumps(payload).encode())

    client = TokenClient()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def test_get_candles_window_sends_ms_bounds_and_returns_the_payload(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        token_client_module, "get_api_base_url", lambda: "https://api.test"
    )
    seen: list[httpx.Request] = []
    client = _client(
        seen,
        {
            "rows": [ROW],
            "chain_id": 8453,
            "address": "0x4200000000000000000000000000000000000006",
            "history_start_ms": 0,
        },
    )

    window = asyncio.run(
        client.get_candles_window(
            "ethereum", "1h", chain_id=8453, start_ms=0, end_ms=3_600_000 * 48
        )
    )

    (request,) = seen
    assert request.url.path == "/blockchain/tokens/candles/"
    assert dict(request.url.params) == {
        "coin": "ethereum",
        "interval": "1h",
        "chain_id": "8453",
        "start_ms": "0",
        "end_ms": str(3_600_000 * 48),
    }
    assert window == {
        "rows": [ROW],
        "chain_id": 8453,
        "address": "0x4200000000000000000000000000000000000006",
        "history_start_ms": 0,
    }


def test_get_candles_keeps_the_legacy_cursor_and_omits_absent_bounds(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        token_client_module, "get_api_base_url", lambda: "https://api.test"
    )
    seen: list[httpx.Request] = []
    client = _client(seen, {"rows": [ROW]})

    rows = asyncio.run(
        client.get_candles("0xabc", "5m", chain_id=1, before_timestamp=1_700_000_000)
    )

    (request,) = seen
    assert dict(request.url.params) == {
        "coin": "0xabc",
        "interval": "5m",
        "chain_id": "1",
        "before_timestamp": "1700000000",
    }
    assert rows == [ROW]
