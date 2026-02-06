import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter


class TestHyperliquidAdapter:
    @pytest.fixture
    def mock_info(self):
        mock = MagicMock()
        mock.meta_and_asset_ctxs.return_value = [
            {"universe": [{"name": "BTC"}, {"name": "ETH"}]},
            [{"funding": "0.0001"}],
        ]
        mock.spot_meta = {"tokens": [], "universe": []}
        mock.funding_history.return_value = [
            {"time": 1700000000000, "coin": "ETH", "fundingRate": "0.0001"}
        ]
        mock.candles_snapshot.return_value = [
            {"t": 1700000000000, "o": "2000", "h": "2050", "l": "1980", "c": "2020"}
        ]
        mock.l2_snapshot.return_value = {
            "levels": [[{"px": "2000", "sz": "10", "n": 5}]]
        }
        mock.user_state.return_value = {"assetPositions": [], "crossMarginSummary": {}}
        mock.spot_user_state.return_value = {"balances": []}
        mock.post.return_value = []
        mock.asset_to_sz_decimals = {0: 4, 1: 3, 10000: 6}
        mock.coin_to_asset = {"BTC": 0, "ETH": 1}
        return mock

    @pytest.fixture
    def mock_constants(self):
        return SimpleNamespace(MAINNET_API_URL="https://api.hyperliquid.xyz")

    @pytest.fixture
    def adapter(self, mock_info, mock_constants):
        with patch(
            "wayfinder_paths.adapters.hyperliquid_adapter.adapter.Info",
            return_value=mock_info,
        ):
            with patch(
                "wayfinder_paths.adapters.hyperliquid_adapter.adapter.constants",
                mock_constants,
            ):
                adapter = HyperliquidAdapter(config={})
                adapter.info = mock_info
                return adapter

    @pytest.mark.asyncio
    async def test_get_meta_and_asset_ctxs(self, adapter):
        success, data = await adapter.get_meta_and_asset_ctxs()
        assert success
        assert "universe" in data[0]

    @pytest.mark.asyncio
    async def test_get_spot_meta(self, adapter):
        success, data = await adapter.get_spot_meta()
        assert success

    @pytest.mark.asyncio
    async def test_get_l2_book(self, adapter):
        success, data = await adapter.get_l2_book("ETH")
        assert success
        assert "levels" in data

    @pytest.mark.asyncio
    async def test_get_user_state(self, adapter):
        success, data = await adapter.get_user_state("0x1234")
        assert success
        assert "assetPositions" in data

    def test_get_sz_decimals(self, adapter):
        decimals = adapter.get_sz_decimals(0)
        assert decimals == 4

    def test_get_sz_decimals_unknown_asset(self, adapter):
        with pytest.raises(ValueError, match="Unknown asset_id"):
            adapter.get_sz_decimals(99999)

    @pytest.mark.asyncio
    async def test_get_full_user_state(self, adapter):
        adapter.info.frontend_open_orders.return_value = [{"oid": 1}]

        ok, state = await adapter.get_full_user_state(account="0x1234")
        assert ok is True
        assert state["protocol"] == "hyperliquid"
        assert state["account"] == "0x1234"
        assert state["perp"] is not None
        assert state["spot"] is not None
        assert state["openOrders"] == [{"oid": 1}]

    @pytest.mark.asyncio
    async def test_wait_for_deposit_confirms_via_ledger_if_already_credited(
        self, adapter
    ):
        address = "0x" + "11" * 20
        adapter.info.user_state.return_value = {
            "marginSummary": {"accountValue": "100.0"}
        }
        adapter.info.post.return_value = [
            {
                "time": int(time.time() * 1000),
                "hash": "0xabc",
                "delta": {"type": "deposit", "usdc": "100.0"},
            }
        ]

        with patch(
            "wayfinder_paths.adapters.hyperliquid_adapter.adapter.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep_mock:
            ok, final_balance = await adapter.wait_for_deposit(
                address,
                expected_increase=100.0,
                timeout_s=60,
                poll_interval_s=5,
            )

        assert ok is True
        assert final_balance == 100.0
        sleep_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_wait_for_deposit_confirms_on_margin_increase(self, adapter):
        address = "0x" + "22" * 20
        adapter.info.post.return_value = []
        adapter.info.user_state.side_effect = [
            {"marginSummary": {"accountValue": "0.0"}},  # initial baseline
            {"marginSummary": {"accountValue": "0.0"}},  # first check
            {"marginSummary": {"accountValue": "100.0"}},  # credited
        ]

        with patch(
            "wayfinder_paths.adapters.hyperliquid_adapter.adapter.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep_mock:
            ok, final_balance = await adapter.wait_for_deposit(
                address,
                expected_increase=100.0,
                timeout_s=60,
                poll_interval_s=5,
            )

        assert ok is True
        assert final_balance == 100.0
        assert sleep_mock.await_count == 1
