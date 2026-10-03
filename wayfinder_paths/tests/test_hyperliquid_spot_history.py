from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.core.clients.HyperliquidDataClient import (
    HYPERLIQUID_INFO_CLIENT,
    HyperliquidDataClient,
)

META = {
    "tokens": [
        {"index": 0, "name": "USDC"},
        {"index": 1, "name": "PURR"},
        {"index": 900, "name": "HYPE"},
    ],
    "universe": [
        {"index": 0, "tokens": [1, 0]},
        {"index": 107, "tokens": [900, 0]},
    ],
}


def candle(t):
    return {"t": t, "T": t + 3_599_999, "o": "1", "h": "2", "l": "1", "c": "2"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pair,coin,asset_id",
    [("HYPE/USDC", "@107", 10107), ("PURR/USDC", "PURR/USDC", 10000)],
)
async def test_spot_resolves_exact_pair_and_returns_only_completed_bars(
    monkeypatch, pair, coin, asset_id
):
    post = AsyncMock(return_value=META)
    post.side_effect = [
        META,
        [candle(7_200_000), candle(0), candle(3_600_000), candle(-3_600_000)],
    ]
    monkeypatch.setattr(HYPERLIQUID_INFO_CLIENT, "post", post)
    client = HyperliquidDataClient()
    gateway = AsyncMock(side_effect=AssertionError("Spot must not need gateway auth"))
    monkeypatch.setattr(client, "_authed_request", gateway)
    result = await client.get_candles_response(pair, 0, 7_200_000)
    assert result["asset_id"] == asset_id
    assert result["quote_currency"] == "USDC"
    assert [r["t"] for r in result["rows"]] == [0, 3_600_000]
    assert post.await_args.args[0] == {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1h", "startTime": 0, "endTime": 7_200_000},
    }
    gateway.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_spot_history_stays_empty(monkeypatch):
    post = AsyncMock(side_effect=[META, []])
    monkeypatch.setattr(HYPERLIQUID_INFO_CLIENT, "post", post)
    assert await HyperliquidDataClient().get_candles("HYPE/USDC", 0, 7_200_000) == []
    assert post.await_count == 2  # No synthetic current-mid candle or perp fallback.


@pytest.mark.asyncio
async def test_unknown_pair_does_not_fall_back_to_underlying_perp(monkeypatch):
    post = AsyncMock(return_value=META)
    monkeypatch.setattr(HYPERLIQUID_INFO_CLIENT, "post", post)
    with pytest.raises(ValueError, match="Unknown Hyperliquid spot pair"):
        await HyperliquidDataClient().get_candles("FAKE/USDC", 0, 7_200_000)
    assert post.await_count == 1


@pytest.mark.asyncio
async def test_spot_excludes_forming_candle_even_with_future_end(monkeypatch):
    import importlib

    module = importlib.import_module(
        "wayfinder_paths.core.clients.HyperliquidDataClient"
    )
    monkeypatch.setattr(module.time, "time", lambda: 4000)
    monkeypatch.setattr(
        HYPERLIQUID_INFO_CLIENT,
        "post",
        AsyncMock(side_effect=[META, [candle(0), candle(3_600_000)]]),
    )
    rows = await HyperliquidDataClient().get_candles("HYPE/USDC", 0, 7_200_000)
    assert [r["t"] for r in rows] == [0]
