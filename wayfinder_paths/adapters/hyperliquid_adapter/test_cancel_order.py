from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter


class TestHyperliquidCancelOrder:
    @pytest.mark.asyncio
    async def test_adapter_cancel_order_uses_int_oid(self):
        with patch(
            "wayfinder_paths.adapters.hyperliquid_adapter.adapter.get_info",
            return_value=SimpleNamespace(),
        ):
            adapter = HyperliquidAdapter(
                config={},
                sign_callback=AsyncMock(return_value="0x" + "00" * 65),
            )
            adapter._sign_and_broadcast_hypecore = AsyncMock(
                return_value={"status": "ok"}
            )

            await adapter.cancel_order(
                asset_id=10210, order_id=306356655993, address="0xabc"
            )

            args, _ = adapter._sign_and_broadcast_hypecore.await_args
            action = args[0]
            assert action["type"] == "cancel"
            assert action["cancels"][0]["a"] == 10210
            assert isinstance(action["cancels"][0]["o"], int)
            assert action["cancels"][0]["o"] == 306356655993

    @pytest.mark.asyncio
    async def test_adapter_cancel_order_parses_string_oid(self):
        adapter = HyperliquidAdapter(config={})
        adapter._sign_and_broadcast_hypecore = AsyncMock(return_value={"status": "ok"})

        ok, _ = await adapter.cancel_order(
            asset_id=10210, order_id="306356655993", address="0xabc"
        )
        assert ok is True

        args, _ = adapter._sign_and_broadcast_hypecore.await_args
        action = args[0]
        assert action["cancels"][0]["o"] == 306356655993

    @pytest.mark.asyncio
    async def test_adapter_cancel_order_rejects_bad_oid(self):
        adapter = HyperliquidAdapter(config={})
        adapter._sign_and_broadcast_hypecore = AsyncMock(return_value={"status": "ok"})

        ok, res = await adapter.cancel_order(
            asset_id=1, order_id="not-a-number", address="0xabc"
        )
        assert ok is False
        assert res["status"] == "err"
