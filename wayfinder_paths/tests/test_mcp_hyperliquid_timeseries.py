from __future__ import annotations

import pytest

from wayfinder_paths.mcp.tools import hyperliquid


@pytest.mark.asyncio
async def test_hyperliquid_get_candles_returns_bounded_rows(monkeypatch) -> None:
    async def fake_response(coin: str, start_ms: int, end_ms: int, interval: str) -> dict:
        return {
            "coin": "HYPE",
            "interval": interval,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "rows": [
                {"t": 1, "T": 2, "o": "1", "h": "2", "l": "1", "c": "2"},
                {"t": 2, "T": 3, "o": "2", "h": "3", "l": "2", "c": "3"},
            ],
        }

    monkeypatch.setattr(
        hyperliquid.HYPERLIQUID_DATA_CLIENT,
        "get_candles_response",
        fake_response,
    )

    out = await hyperliquid.hyperliquid_get_candles(
        asset_name="HYPE-USDC",
        interval="5m",
        start_ms=1,
        end_ms=3,
        limit=1,
    )

    assert out["ok"] is True
    assert out["result"]["requested_asset_name"] == "HYPE-USDC"
    assert out["result"]["requested_interval"] == "5m"
    assert out["result"]["row_count"] == 1
    assert out["result"]["rows"][0]["t"] == 2


@pytest.mark.asyncio
async def test_hyperliquid_get_funding_history_requires_complete_range() -> None:
    out = await hyperliquid.hyperliquid_get_funding_history(
        asset_name="xyz:SPCX",
        start_ms=1,
        end_ms=None,
    )

    assert out["ok"] is False
    assert "both start_ms and end_ms" in out["error"]["message"]
