import time
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

        def _post_side_effect(_path, payload):
            req_type = payload.get("type", "")
            if req_type == "clearinghouseState":
                return {"assetPositions": [], "crossMarginSummary": {}}
            if req_type == "metaAndAssetCtxs":
                return [
                    {"universe": [{"name": "BTC"}, {"name": "ETH"}]},
                    [{"funding": "0.0001"}],
                ]
            if req_type == "allMids":
                return {"BTC": "50000.0", "ETH": "3000.0"}
            if req_type == "openOrders":
                return [{"oid": 1}]
            if req_type == "frontendOpenOrders":
                return [{"oid": 1}]
            return []

        mock.post.side_effect = _post_side_effect
        mock.asset_to_sz_decimals = {0: 4, 1: 3, 10000: 6}
        mock.coin_to_asset = {"BTC": 0, "ETH": 1}
        mock.frontend_open_orders.return_value = [{"oid": 1}]
        return mock

    @pytest.fixture
    def adapter(self, mock_info):
        with (
            patch(
                "wayfinder_paths.adapters.hyperliquid_adapter.adapter.get_info",
                return_value=mock_info,
            ),
            patch(
                "wayfinder_paths.adapters.hyperliquid_adapter.adapter.get_perp_dexes",
                return_value=[""],
            ),
        ):
            adapter = HyperliquidAdapter(config={})
            return adapter

    @pytest.fixture
    def _patch_adapter(self, mock_info):
        """Context manager that patches both get_info and get_perp_dexes."""
        return lambda: (
            patch(
                "wayfinder_paths.adapters.hyperliquid_adapter.adapter.get_info",
                return_value=mock_info,
            ),
            patch(
                "wayfinder_paths.adapters.hyperliquid_adapter.adapter.get_perp_dexes",
                return_value=[""],
            ),
        )

    @pytest.mark.asyncio
    async def test_get_meta_and_asset_ctxs(self, adapter, mock_info, _patch_adapter):
        p1, p2 = _patch_adapter()
        with p1, p2:
            success, data = await adapter.get_meta_and_asset_ctxs()
            assert success
            assert "universe" in data[0]

    @pytest.mark.asyncio
    async def test_get_spot_meta(self, adapter, mock_info, _patch_adapter):
        p1, p2 = _patch_adapter()
        with p1, p2:
            success, data = await adapter.get_spot_meta()
            assert success

    @pytest.mark.asyncio
    async def test_get_l2_book(self, adapter, mock_info, _patch_adapter):
        p1, p2 = _patch_adapter()
        with p1, p2:
            success, data = await adapter.get_l2_book("ETH")
            assert success
            assert "levels" in data

    @pytest.mark.asyncio
    async def test_get_user_state(self, adapter, mock_info, _patch_adapter):
        p1, p2 = _patch_adapter()
        with p1, p2:
            success, data = await adapter.get_user_state("0x1234")
            assert success
            assert "assetPositions" in data

    def test_get_sz_decimals(self, adapter, mock_info, _patch_adapter):
        p1, p2 = _patch_adapter()
        with p1, p2:
            decimals = adapter.get_sz_decimals(0)
            assert decimals == 4

    def test_get_sz_decimals_unknown_asset(self, adapter, mock_info, _patch_adapter):
        p1, p2 = _patch_adapter()
        with p1, p2:
            with pytest.raises(ValueError, match="Unknown asset_id"):
                adapter.get_sz_decimals(99999)

    @pytest.mark.asyncio
    async def test_get_full_user_state(self, adapter, mock_info, _patch_adapter):
        p1, p2 = _patch_adapter()
        with p1, p2:
            ok, state = await adapter.get_full_user_state(account="0x1234")
            assert ok is True
            assert state["protocol"] == "hyperliquid"
            assert state["account"] == "0x1234"
            assert state["perp"] is not None
            assert state["spot"] is not None
            assert state["openOrders"] == [{"oid": 1}]

    @pytest.mark.asyncio
    async def test_wait_for_deposit_confirms_via_ledger_if_already_credited(
        self, adapter, mock_info, _patch_adapter
    ):
        address = "0x" + "11" * 20

        def _post_side_effect(_path, payload):
            req_type = payload.get("type", "")
            if req_type == "clearinghouseState":
                return {
                    "marginSummary": {"accountValue": "100.0"},
                    "assetPositions": [],
                    "crossMarginSummary": {},
                }
            return []

        mock_info.post.side_effect = _post_side_effect
        mock_info.user_non_funding_ledger_updates.return_value = [
            {
                "time": int(time.time() * 1000),
                "delta": {"type": "deposit", "usdc": "100.0"},
            }
        ]

        p1, p2 = _patch_adapter()
        with p1, p2:
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
    async def test_wait_for_deposit_confirms_on_margin_increase(
        self, adapter, mock_info, _patch_adapter
    ):
        address = "0x" + "22" * 20
        mock_info.user_non_funding_ledger_updates.return_value = []

        # Track call count to return different values on each post call
        call_count = {"n": 0}

        def _post_side_effect(_path, payload):
            req_type = payload.get("type", "")
            if req_type == "clearinghouseState":
                call_count["n"] += 1
                if call_count["n"] <= 2:
                    return {
                        "marginSummary": {"accountValue": "0.0"},
                        "assetPositions": [],
                        "crossMarginSummary": {},
                    }
                return {
                    "marginSummary": {"accountValue": "100.0"},
                    "assetPositions": [],
                    "crossMarginSummary": {},
                }
            return []

        mock_info.post.side_effect = _post_side_effect

        p1, p2 = _patch_adapter()
        with p1, p2:
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
