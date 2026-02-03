import os
from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
from wayfinder_paths.adapters.hyperliquid_adapter.exchange import Exchange

if os.getenv("RUN_HYPERLIQUID_LIVE_TESTS", "").lower() not in ("1", "true", "yes"):
    pytest.skip(
        "Hyperliquid live tests are disabled (set RUN_HYPERLIQUID_LIVE_TESTS=1 to enable).",
        allow_module_level=True,
    )


@pytest.fixture
def live_adapter():
    return HyperliquidAdapter(config={})


class TestHyperliquidSdkCompat:
    @pytest.mark.asyncio
    async def test_util_mid_prices_live(self, live_adapter):
        mids = await live_adapter.util.get_hypecore_all_dex_mid_prices()
        assert isinstance(mids, dict)
        assert len(mids) > 0

        # Sanity check some common keys exist (perp coins or spot tickers like @107).
        assert "HYPE" in mids or "BTC" in mids or any(k.startswith("@") for k in mids)

    @pytest.mark.asyncio
    async def test_util_meta_live(self, live_adapter):
        meta = await live_adapter.util.get_hypecore_all_dex_meta_universe()
        assert isinstance(meta, dict)
        assert "universe" in meta


class TestExchangeUsesLiveMids:
    @pytest.mark.asyncio
    async def test_place_market_order_builds_ioc_limit(self, live_adapter):
        # Use a perp id to avoid spot naming edge-cases.
        asset_id = live_adapter.coin_to_asset["HYPE"]

        ex = Exchange(
            info=live_adapter.info,
            util=live_adapter.util,
            sign_callback=AsyncMock(return_value="0x"),
            signing_type="eip712",
        )

        async def _no_broadcast(action, address):
            return action

        ex.sign_and_broadcast_hypecore = _no_broadcast

        action = await ex.place_market_order(
            asset_id=asset_id,
            is_buy=True,
            slippage=0.01,
            size=1.0,
            address="0x0000000000000000000000000000000000000000",
        )

        assert action["type"] == "order"
        assert action["orders"][0]["a"] == asset_id
        assert action["orders"][0]["b"] is True

        # Price should be at/above current mid for a buy (within rounding tolerance).
        mid = float(live_adapter.info.all_mids()["HYPE"])
        px = float(action["orders"][0]["p"])
        assert px >= mid * 0.999
