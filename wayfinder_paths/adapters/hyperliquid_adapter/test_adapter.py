from contextlib import ExitStack, contextmanager
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
            req_type = payload["type"]
            if req_type == "clearinghouseState":
                return {"assetPositions": [], "crossMarginSummary": {}}
            if req_type == "metaAndAssetCtxs":
                return [
                    {"universe": [{"name": "BTC"}, {"name": "ETH"}]},
                    [{"funding": "0.0001"}],
                ]
            if req_type == "allMids":
                return {"BTC": "50000.0", "ETH": "3000.0"}
            if req_type == "activeAssetData":
                return {
                    "availableToTrade": ["12.34", "56.78"],
                    "leverage": {"type": "cross", "value": 5},
                    "markPx": "50000.0",
                    "maxTradeSzs": ["0.0012", "0.0056"],
                }
            if req_type == "userAbstraction":
                return "unifiedAccount"
            if req_type == "openOrders":
                return [{"oid": 1}]
            if req_type == "frontendOpenOrders":
                return [{"oid": 1}]
            if req_type == "spotClearinghouseState":
                return {"balances": []}
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
        async def _info_client_post(payload):
            return mock_info.post("/info", payload)

        @contextmanager
        def _ctx():
            with ExitStack() as stack:
                for p in (
                    patch(
                        "wayfinder_paths.adapters.hyperliquid_adapter.adapter.get_info",
                        return_value=mock_info,
                    ),
                    patch(
                        "wayfinder_paths.adapters.hyperliquid_adapter.adapter.get_perp_dexes",
                        return_value=[""],
                    ),
                    patch(
                        "wayfinder_paths.adapters.hyperliquid_adapter.adapter.HYPERLIQUID_INFO_CLIENT.post",
                        new=AsyncMock(side_effect=_info_client_post),
                    ),
                    patch(
                        "wayfinder_paths.adapters.hyperliquid_adapter.adapter.HYPERLIQUID_QUICKNODE_INFO_CLIENT.post",
                        new=AsyncMock(side_effect=_info_client_post),
                    ),
                ):
                    stack.enter_context(p)
                yield

        return _ctx

    @pytest.mark.asyncio
    async def test_get_meta_and_asset_ctxs(self, adapter, mock_info, _patch_adapter):
        with _patch_adapter():
            success, data = await adapter.get_meta_and_asset_ctxs()
            assert success
            assert "universe" in data[0]

    @pytest.mark.asyncio
    async def test_get_spot_meta(self, adapter, mock_info, _patch_adapter):
        with _patch_adapter():
            success, data = await adapter.get_spot_meta()
            assert success

    @pytest.mark.asyncio
    async def test_get_l2_book(self, adapter, mock_info, _patch_adapter):
        with _patch_adapter():
            success, data = await adapter.get_l2_book("ETH")
            assert success
            assert "levels" in data

    @pytest.mark.asyncio
    async def test_get_user_state(self, adapter, mock_info, _patch_adapter):
        with _patch_adapter():
            success, data = await adapter.get_user_state("0x1234")
            assert success
            assert "assetPositions" in data

    @pytest.mark.asyncio
    async def test_get_active_asset_data(self, adapter, mock_info, _patch_adapter):
        with _patch_adapter():
            success, data = await adapter.get_active_asset_data("0x1234", "BTC-USDC")
            assert success
            assert data["availableToTrade"] == ["12.34", "56.78"]
            mock_info.post.assert_any_call(
                "/info",
                {"type": "activeAssetData", "user": "0x1234", "coin": "BTC"},
            )

    @pytest.mark.asyncio
    async def test_get_user_abstraction(self, adapter, mock_info, _patch_adapter):
        with _patch_adapter():
            success, data = await adapter.get_user_abstraction("0x1234")
            assert success
            assert data == "unifiedAccount"
            mock_info.post.assert_any_call(
                "/info",
                {"type": "userAbstraction", "user": "0x1234"},
            )

    def test_active_asset_data_coin(self):
        assert HyperliquidAdapter.active_asset_data_coin("BTC-USDC") == "BTC"
        assert HyperliquidAdapter.active_asset_data_coin("xyz:NVDA") == "xyz:NVDA"
        with pytest.raises(ValueError, match="activeAssetData"):
            HyperliquidAdapter.active_asset_data_coin("BTC/USDC")

    def test_get_sz_decimals(self, adapter, mock_info, _patch_adapter):
        with _patch_adapter():
            decimals = adapter.get_sz_decimals(0)
            assert decimals == 4

    def test_get_sz_decimals_unknown_asset(self, adapter, mock_info, _patch_adapter):
        with _patch_adapter():
            with pytest.raises(ValueError, match="Unknown asset_id"):
                adapter.get_sz_decimals(99999)

    @pytest.mark.asyncio
    async def test_get_full_user_state(self, adapter, mock_info, _patch_adapter):
        with _patch_adapter():
            ok, state = await adapter.get_full_user_state(account="0x1234")
            assert ok is True
            assert state["protocol"] == "hyperliquid"
            assert state["account"] == "0x1234"
            assert state["perp"] is not None
            assert state["spot"] is not None
            assert state["openOrders"] == [{"oid": 1}]

    @pytest.mark.asyncio
    async def test_wait_for_deposit_confirms_on_balance_increase(
        self, adapter, mock_info, _patch_adapter
    ):
        address = "0x" + "22" * 20

        call_count = {"n": 0}

        async def _qn_post(body):
            if body["type"] == "spotClearinghouseState":
                call_count["n"] += 1
                total = "0.0" if call_count["n"] <= 2 else "100.0"
                return {"balances": [{"coin": "USDC", "token": 0, "total": total}]}
            return mock_info.post("/info", body)

        with _patch_adapter():
            with (
                patch(
                    "wayfinder_paths.adapters.hyperliquid_adapter.adapter.HYPERLIQUID_QUICKNODE_INFO_CLIENT.post",
                    new=_qn_post,
                ),
                patch(
                    "wayfinder_paths.adapters.hyperliquid_adapter.adapter.asyncio.sleep",
                    new=AsyncMock(),
                ) as sleep_mock,
            ):
                ok, final_balance = await adapter.wait_for_deposit(
                    address,
                    expected_increase=100.0,
                    timeout_s=60,
                    poll_interval_s=5,
                )

            assert ok is True
            assert final_balance == 100.0
            assert sleep_mock.await_count == 1

    @pytest.mark.asyncio
    async def test_wait_for_deposit_returns_false_on_timeout(
        self, adapter, mock_info, _patch_adapter
    ):
        address = "0x" + "33" * 20

        async def _qn_post(body):
            if body["type"] == "spotClearinghouseState":
                return {"balances": [{"coin": "USDC", "token": 0, "total": "0.0"}]}
            return mock_info.post("/info", body)

        with _patch_adapter():
            with (
                patch(
                    "wayfinder_paths.adapters.hyperliquid_adapter.adapter.HYPERLIQUID_QUICKNODE_INFO_CLIENT.post",
                    new=_qn_post,
                ),
                patch(
                    "wayfinder_paths.adapters.hyperliquid_adapter.adapter.asyncio.sleep",
                    new=AsyncMock(),
                ),
            ):
                ok, final_balance = await adapter.wait_for_deposit(
                    address,
                    expected_increase=100.0,
                    timeout_s=5,
                    poll_interval_s=1,
                )

        assert ok is False
        assert final_balance == 0.0

    @pytest.mark.asyncio
    async def test_wait_for_deposit_confirms_on_perp_credit_split_mode(
        self, adapter, mock_info, _patch_adapter
    ):
        """Bridge2 credits the PERP clearinghouse for accounts still in
        "default" (split) mode — the waiter must see those too."""
        address = "0x" + "44" * 20

        perp_calls = {"n": 0}

        async def _qn_post(body):
            if body["type"] == "spotClearinghouseState":
                return {"balances": [{"coin": "USDC", "token": 0, "total": "0.0"}]}
            if body["type"] == "clearinghouseState":
                perp_calls["n"] += 1
                value = "0.0" if perp_calls["n"] <= 2 else "60.0"
                return {
                    "assetPositions": [],
                    "marginSummary": {"accountValue": value},
                }
            return mock_info.post("/info", body)

        with _patch_adapter():
            with (
                patch(
                    "wayfinder_paths.adapters.hyperliquid_adapter.adapter.HYPERLIQUID_QUICKNODE_INFO_CLIENT.post",
                    new=_qn_post,
                ),
                patch(
                    "wayfinder_paths.adapters.hyperliquid_adapter.adapter.asyncio.sleep",
                    new=AsyncMock(),
                ),
            ):
                ok, final_balance = await adapter.wait_for_deposit(
                    address,
                    expected_increase=60.0,
                    timeout_s=60,
                    poll_interval_s=5,
                )

        assert ok is True
        assert final_balance == 60.0

    @pytest.mark.asyncio
    async def test_ensure_unified_account_short_circuits_when_already_unified(
        self, adapter, mock_info, _patch_adapter
    ):
        address = "0x" + "55" * 20
        mock_info.query_user_abstraction_state.return_value = "unifiedAccount"

        with _patch_adapter():
            with patch.object(
                adapter, "set_account_abstraction", new=AsyncMock()
            ) as set_mock:
                ok, msg = await adapter.ensure_unified_account(address)

        assert ok is True
        assert "already" in msg
        set_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ensure_unified_account_converts_default_accounts(
        self, adapter, mock_info, _patch_adapter
    ):
        address = "0x" + "66" * 20
        mock_info.query_user_abstraction_state.return_value = "default"

        with _patch_adapter():
            with patch.object(
                adapter,
                "set_account_abstraction",
                new=AsyncMock(return_value=(True, {"status": "ok"})),
            ) as set_mock:
                ok, msg = await adapter.ensure_unified_account(address)

        assert ok is True
        assert msg == "Unified account enabled"
        set_mock.assert_awaited_once_with(address, "unifiedAccount")

    @pytest.mark.asyncio
    async def test_ensure_unified_account_reports_failure(
        self, adapter, mock_info, _patch_adapter
    ):
        address = "0x" + "77" * 20
        mock_info.query_user_abstraction_state.return_value = "default"

        with _patch_adapter():
            with patch.object(
                adapter,
                "set_account_abstraction",
                new=AsyncMock(return_value=(False, {"status": "err"})),
            ):
                ok, msg = await adapter.ensure_unified_account(address)

        assert ok is False
        assert msg.startswith("Failed to enable unified account")

    @pytest.mark.asyncio
    async def test_unify_if_split_account_converts_default(
        self, adapter, mock_info, _patch_adapter
    ):
        address = "0x" + "88" * 20
        mock_info.query_user_abstraction_state.return_value = "default"

        with _patch_adapter():
            with patch.object(
                adapter,
                "set_account_abstraction",
                new=AsyncMock(return_value=(True, {"status": "ok"})),
            ) as set_mock:
                ok, msg = await adapter.unify_if_split_account(address)

        assert ok is True
        assert msg == "Unified account enabled"
        set_mock.assert_awaited_once_with(address, "unifiedAccount")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["unifiedAccount", "portfolioMargin", "dexAbstraction"])
    async def test_unify_if_split_account_leaves_non_default_modes_alone(
        self, adapter, mock_info, _patch_adapter, state
    ):
        """portfolioMargin/dexAbstraction are deliberate user choices that
        already share collateral — money movement must not downgrade them."""
        address = "0x" + "99" * 20
        mock_info.query_user_abstraction_state.return_value = state

        with _patch_adapter():
            with patch.object(
                adapter, "set_account_abstraction", new=AsyncMock()
            ) as set_mock:
                ok, msg = await adapter.unify_if_split_account(address)

        assert ok is True
        assert state in msg
        set_mock.assert_not_awaited()
