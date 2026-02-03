from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
from wayfinder_paths.adapters.hyperliquid_adapter.exchange import Exchange


class TestHyperliquidCancelOrder:
    @pytest.mark.asyncio
    async def test_exchange_cancel_order_uses_int_oid(self):
        ex = Exchange(
            info=SimpleNamespace(),
            util=SimpleNamespace(),
            sign_callback=AsyncMock(return_value="0x"),
            signing_type="eip712",
        )
        ex.sign_and_broadcast_hypecore = AsyncMock(return_value={"status": "ok"})

        await ex.cancel_order(asset_id=10210, order_id=306356655993, address="0xabc")

        args, _ = ex.sign_and_broadcast_hypecore.await_args
        action = args[0]
        assert action["type"] == "cancel"
        assert action["cancels"][0]["a"] == 10210
        assert isinstance(action["cancels"][0]["o"], int)
        assert action["cancels"][0]["o"] == 306356655993

    @pytest.mark.asyncio
    async def test_adapter_cancel_order_parses_string_oid(self):
        adapter = object.__new__(HyperliquidAdapter)
        adapter._exchange = SimpleNamespace()
        adapter._exchange.cancel_order = AsyncMock(return_value={"status": "ok"})

        ok, _ = await adapter.cancel_order(
            asset_id=10210, order_id="306356655993", address="0xabc"
        )
        assert ok is True

        adapter._exchange.cancel_order.assert_awaited_once_with(
            asset_id=10210, order_id=306356655993, address="0xabc"
        )

    @pytest.mark.asyncio
    async def test_adapter_cancel_order_rejects_bad_oid(self):
        adapter = object.__new__(HyperliquidAdapter)
        adapter._exchange = SimpleNamespace()
        adapter._exchange.cancel_order = AsyncMock(return_value={"status": "ok"})

        ok, res = await adapter.cancel_order(
            asset_id=1, order_id="not-a-number", address="0xabc"
        )
        assert ok is False
        assert res["status"] == "err"
