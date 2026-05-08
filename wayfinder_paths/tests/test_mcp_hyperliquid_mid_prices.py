"""Live tests for the per-market-type mid-price feed grammar.

`HyperliquidAdapter.get_all_mid_prices()` returns a dict keyed differently
per market type. These tests verify `HyperliquidAdapter.get_mid_price_key`
produces keys that actually resolve in the live feed.
"""

from __future__ import annotations

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter
from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_search_mid_prices


async def _resolved_mid(asset_name: str) -> float | None:
    adapter = HyperliquidAdapter()
    asset_id = await adapter.get_asset_id(asset_name)
    assert asset_id is not None, f"failed to resolve {asset_name!r}"
    ok_mids, mids = await adapter.get_all_mid_prices()
    assert ok_mids and isinstance(mids, dict)
    for key in adapter.get_mid_price_key(asset_name, asset_id):
        v = mids.get(key)
        if v is not None:
            return float(v)
    return None


@pytest.mark.asyncio
async def test_mid_price_core_perp_btc():
    mid = await _resolved_mid("BTC-USDC")
    assert mid is not None and mid > 0


@pytest.mark.asyncio
async def test_mid_price_hip3_perp_xyz_nvda():
    mid = await _resolved_mid("xyz:NVDA")
    assert mid is not None and mid > 0


@pytest.mark.asyncio
async def test_mid_price_spot_kntq_usdh():
    # KNTQ has no canonical-name entry; only "@<spot_index>" works.
    mid = await _resolved_mid("KNTQ/USDH")
    assert mid is not None and mid > 0


@pytest.mark.asyncio
async def test_mid_price_spot_purr_usdc_grandfathered():
    # PURR is grandfathered under its canonical name; "@0" returns None.
    mid = await _resolved_mid("PURR/USDC")
    assert mid is not None and mid > 0


def _first_book_coin(markets):
    first = markets[0]
    if first["class"] == "priceBinary":
        return first["sides"][0]["asset_name"]
    return first["outcomes"][0]["asset_name"]


@pytest.mark.asyncio
async def test_mid_price_hip4_outcome():
    # HIP-4 outcomes rotate daily at 06:00 UTC; pick whatever's on book now.
    adapter = HyperliquidAdapter()
    ok_outs, outcomes = await adapter.get_outcome_markets()
    assert ok_outs and outcomes, "no HIP-4 outcomes on book"

    mid = await _resolved_mid(_first_book_coin(outcomes))
    assert mid is not None and mid > 0


@pytest.mark.asyncio
async def test_search_mid_prices_unfiltered():
    res = await hyperliquid_search_mid_prices()
    assert res["ok"] and res["result"]["success"]
    assert len(res["result"]["prices"]) > 100


@pytest.mark.asyncio
async def test_search_mid_prices_filter_mixed_markets():
    # Pick a HIP-4 outcome that's currently on book.
    adapter = HyperliquidAdapter()
    ok_outs, outcomes = await adapter.get_outcome_markets()
    assert ok_outs and outcomes
    hip4 = _first_book_coin(outcomes)

    res = await hyperliquid_search_mid_prices(
        ["BTC-USDC", "xyz:NVDA", "KNTQ/USDH", hip4, "BOGUS"],
    )
    assert res["ok"]
    prices = res["result"]["prices"]
    assert {"BTC-USDC", "xyz:NVDA", "KNTQ/USDH", hip4} == prices.keys()
    assert all(float(prices[k]) > 0 for k in prices)
