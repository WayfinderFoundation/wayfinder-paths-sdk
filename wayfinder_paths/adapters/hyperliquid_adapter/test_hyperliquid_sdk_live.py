import os
from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter

if os.getenv("RUN_HYPERLIQUID_LIVE_TESTS", "").lower() not in ("1", "true", "yes"):
    pytest.skip(
        "Hyperliquid live tests are disabled (set RUN_HYPERLIQUID_LIVE_TESTS=1 to enable).",
        allow_module_level=True,
    )


@pytest.fixture
def live_adapter():
    return HyperliquidAdapter(config={})


class TestAdapterUsesLiveMids:
    @pytest.mark.asyncio
    async def test_place_market_order_builds_ioc_limit(self, live_adapter):
        asset_id = live_adapter.coin_to_asset["HYPE"]

        adapter = HyperliquidAdapter(
            config={},
            sign_callback=AsyncMock(return_value="0x" + "00" * 65),
        )

        async def _no_broadcast(action, address):
            return {"status": "ok", "action": action}

        adapter._sign_and_broadcast_hypecore = _no_broadcast

        success, result = await adapter.place_market_order(
            asset_id=asset_id,
            is_buy=True,
            slippage=0.01,
            size=1.0,
            address="0x0000000000000000000000000000000000000000",
        )

        action = result["action"]
        assert action["type"] == "order"
        assert action["orders"][0]["a"] == asset_id
        assert action["orders"][0]["b"] is True

        ok, mids = await live_adapter.get_all_mid_prices()
        assert ok
        mid = mids["HYPE"]
        px = float(action["orders"][0]["p"])
        assert px >= mid * 0.999
