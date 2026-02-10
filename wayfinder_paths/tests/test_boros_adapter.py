from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.adapters.boros_adapter.adapter import BorosAdapter


def _make_adapter() -> BorosAdapter:
    return BorosAdapter(
        config={
            "boros_adapter": {
                "chain_id": 42161,
                "base_url": "https://example.com",
                "account_id": 0,
            }
        },
        user_address="0x0000000000000000000000000000000000000000",
    )


@pytest.mark.asyncio
async def test_adapter_get_market_history_proxies_to_client():
    adapter = _make_adapter()
    adapter.boros_client.get_market_history = AsyncMock(return_value=[{"t": 1}])  # type: ignore[method-assign]

    ok, history = await adapter.get_market_history(
        123, time_frame="1h", start_ts=1700000000, end_ts=1700003600
    )

    assert ok is True
    assert history == [{"t": 1}]
    adapter.boros_client.get_market_history.assert_awaited_once_with(  # type: ignore[attr-defined]
        123,
        time_frame="1h",
        start_ts=1700000000,
        end_ts=1700003600,
    )


@pytest.mark.asyncio
async def test_list_available_underlyings_filters_active_only_and_aggregates(
    monkeypatch,
):
    adapter = _make_adapter()
    now = 1_700_000_000
    monkeypatch.setattr(
        "wayfinder_paths.adapters.boros_adapter.adapter.time.time", lambda: now
    )

    markets = [
        {
            "marketId": 1,
            "state": "Normal",
            "imData": {"maturity": now + 10},
            "metadata": {"assetSymbol": "eth", "platformName": "Hyperliquid"},
        },
        {
            "marketId": 2,
            "state": "Normal",
            "imData": {"maturity": now + 20},
            "metadata": {"assetSymbol": "ETH", "platformName": "Binance"},
        },
        # Expired
        {
            "marketId": 3,
            "state": "Normal",
            "imData": {"maturity": now - 1},
            "metadata": {"assetSymbol": "BTC", "platformName": "Hyperliquid"},
        },
        # Inactive state
        {
            "marketId": 4,
            "state": "Paused",
            "imData": {"maturity": now + 10},
            "metadata": {"assetSymbol": "SOL", "platformName": "Hyperliquid"},
        },
    ]
    adapter.list_markets_all = AsyncMock(return_value=(True, markets))  # type: ignore[method-assign]

    ok, underlyings = await adapter.list_available_underlyings(active_only=True)

    assert ok is True
    assert underlyings == [
        {"symbol": "ETH", "markets_count": 2, "platforms": ["Binance", "Hyperliquid"]}
    ]


@pytest.mark.asyncio
async def test_list_available_platforms_counts_active_only(monkeypatch):
    adapter = _make_adapter()
    now = 1_700_000_000
    monkeypatch.setattr(
        "wayfinder_paths.adapters.boros_adapter.adapter.time.time", lambda: now
    )

    markets = [
        {
            "marketId": 1,
            "state": "Normal",
            "imData": {"maturity": now + 10},
            "metadata": {"platformName": "Hyperliquid"},
        },
        {
            "marketId": 2,
            "state": "Normal",
            "imData": {"maturity": now + 10},
            "platform": {"name": "Hyperliquid"},
        },
        # Inactive should be excluded
        {
            "marketId": 3,
            "state": "Normal",
            "imData": {"maturity": now - 1},
            "metadata": {"platformName": "Binance"},
        },
    ]
    adapter.list_markets_all = AsyncMock(return_value=(True, markets))  # type: ignore[method-assign]

    ok, platforms = await adapter.list_available_platforms(active_only=True)

    assert ok is True
    assert platforms == [{"platform": "Hyperliquid", "markets_count": 2}]


@pytest.mark.asyncio
async def test_search_markets_filters_and_enriches(monkeypatch):
    adapter = _make_adapter()
    now = 1_700_000_000
    monkeypatch.setattr(
        "wayfinder_paths.adapters.boros_adapter.adapter.time.time", lambda: now
    )

    assets = [
        {"tokenId": 3, "symbol": "USDT", "address": "0xabc", "decimals": 6},
        {"tokenId": 5, "symbol": "HYPE", "address": "0xdef", "decimals": 18},
    ]
    adapter.get_assets = AsyncMock(return_value=(True, assets))  # type: ignore[method-assign]

    markets = [
        {
            "marketId": 1,
            "tokenId": 3,
            "state": "Normal",
            "imData": {
                "maturity": now + 20,
                "symbol": "ETH-2026",
                "underlying": "ETH",
            },
            "metadata": {"platformName": "Hyperliquid"},
            "data": {"midApr": 10, "floatingApr": 0.05, "markApr": 1500},
        },
        # Same filters, earlier maturity (should sort first)
        {
            "marketId": 2,
            "tokenId": 3,
            "state": "Normal",
            "imData": {"maturity": now + 10, "symbol": "ETH-2025", "underlying": "ETH"},
            "metadata": {"platformName": "Hyperliquid"},
            "data": {"midApr": 10, "floatingApr": 0.05, "markApr": 1500},
        },
        # Different collateral
        {
            "marketId": 3,
            "tokenId": 5,
            "state": "Normal",
            "imData": {"maturity": now + 10, "underlying": "ETH"},
            "metadata": {"platformName": "Hyperliquid"},
        },
        # Different platform
        {
            "marketId": 4,
            "tokenId": 3,
            "state": "Normal",
            "imData": {"maturity": now + 10, "underlying": "ETH"},
            "metadata": {"platformName": "Binance"},
        },
        # Inactive (expired)
        {
            "marketId": 5,
            "tokenId": 3,
            "state": "Normal",
            "imData": {"maturity": now - 1, "underlying": "ETH"},
            "metadata": {"platformName": "Hyperliquid"},
        },
    ]
    adapter.list_markets_all = AsyncMock(return_value=(True, markets))  # type: ignore[method-assign]

    ok, out = await adapter.search_markets(
        collateral=3, asset="eth", platform="hyperliquid", active_only=True
    )

    assert ok is True
    assert [m["market_id"] for m in out] == [2, 1]
    assert out[0]["collateral"] == {
        "token_id": 3,
        "symbol": "USDT",
        "address": "0xabc",
        "decimals": 6,
    }
    assert out[0]["mid_apr"] == 0.10
    assert out[0]["floating_apr"] == 0.05
    assert out[0]["mark_apr"] == 0.15


@pytest.mark.asyncio
async def test_list_markets_by_collateral_proxies_to_search_markets():
    adapter = _make_adapter()
    adapter.search_markets = AsyncMock(return_value=(True, [{"market_id": 1}]))  # type: ignore[method-assign]

    ok, markets = await adapter.list_markets_by_collateral(3, active_only=False)

    assert ok is True
    assert markets == [{"market_id": 1}]
    adapter.search_markets.assert_awaited_once_with(collateral=3, active_only=False)  # type: ignore[attr-defined]
