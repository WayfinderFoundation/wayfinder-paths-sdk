from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter.exchange import Exchange


class _InfoStub(SimpleNamespace):
    def all_mids(self):
        return {"HYPE": "1.0"}

    @property
    def asset_to_sz_decimals(self):
        return {7: 0}


class TestExchangeMidPriceFetch:
    @pytest.mark.asyncio
    async def test_place_market_order_uses_all_mids(self):
        info = _InfoStub(asset_to_coin={7: "HYPE"})
        ex = Exchange(
            info=info,
            sign_callback=AsyncMock(return_value="0x"),
            signing_type="eip712",
        )

        async def _no_broadcast(action, address):
            return action

        ex.sign_and_broadcast_hypecore = _no_broadcast

        action = await ex.place_market_order(
            asset_id=7,
            is_buy=True,
            slippage=0.01,
            size=1.0,
            address="0xabc",
        )

        assert action["type"] == "order"
        assert action["orders"][0]["a"] == 7
        assert action["orders"][0]["b"] is True
        assert action["orders"][0]["p"] == "1.01"
