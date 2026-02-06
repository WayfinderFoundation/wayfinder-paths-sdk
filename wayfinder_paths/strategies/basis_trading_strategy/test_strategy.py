import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.adapters.ledger_adapter.adapter import LedgerAdapter
from wayfinder_paths.core.clients.LedgerClient import LedgerClient
from wayfinder_paths.strategies.basis_trading_strategy.strategy import (
    BasisPosition,
    BasisTradingStrategy,
)
from wayfinder_paths.tests.test_utils import (
    assert_quote_result,
    assert_status_dict,
    assert_status_tuple,
    load_strategy_examples,
)


def load_examples():
    return load_strategy_examples(Path(__file__))


class TestBasisTradingStrategy:
    @pytest.fixture
    def mock_hyperliquid_adapter(self):
        mock = MagicMock()
        # Provide enough points to satisfy the strategy's lookback checks without making tests too slow.
        mock.get_meta_and_asset_ctxs = AsyncMock(
            return_value=(
                True,
                [
                    {
                        "universe": [
                            {"name": "ETH", "maxLeverage": 50, "marginTableId": 1},
                            {"name": "BTC", "maxLeverage": 50, "marginTableId": 2},
                        ]
                    },
                    [
                        {
                            "openInterest": "1000",
                            "markPx": "2000",
                            "dayNtlVlm": "10000000",
                        },
                        {
                            "openInterest": "500",
                            "markPx": "50000",
                            "dayNtlVlm": "50000000",
                        },
                    ],
                ],
            )
        )
        mock.get_spot_meta = AsyncMock(
            return_value=(
                True,
                {
                    "tokens": [
                        {"index": 0, "name": "ETH"},
                        {"index": 1, "name": "USDC"},
                    ],
                    "universe": [{"tokens": [0, 1], "index": 0}],
                },
            )
        )
        mock.get_spot_l2_book = AsyncMock(
            return_value=(
                True,
                {
                    "levels": [
                        [{"px": "1999", "sz": "100", "n": 10}],
                        [{"px": "2001", "sz": "100", "n": 10}],
                    ],
                    "midPx": "2000",
                },
            )
        )
        mock.get_margin_table = AsyncMock(
            return_value=(
                True,
                {
                    "marginTiers": [
                        {"lowerBound": 0, "maxLeverage": 50},
                    ]
                },
            )
        )
        mock.coin_to_asset = {"ETH": 1, "BTC": 0}
        mock.asset_to_sz_decimals = {0: 4, 1: 3, 10000: 6}
        mock.get_all_mid_prices = AsyncMock(
            return_value=(True, {"ETH": 2000.0, "BTC": 50000.0})
        )
        mock.get_user_state = AsyncMock(
            return_value=(
                True,
                {
                    "marginSummary": {"accountValue": "0"},
                    "withdrawable": "100.0",
                    "assetPositions": [],
                },
            )
        )
        mock.get_spot_user_state = AsyncMock(return_value=(True, {"balances": []}))
        mock.get_max_builder_fee = AsyncMock(return_value=(True, 0))
        mock.approve_builder_fee = AsyncMock(return_value=(True, {"status": "ok"}))
        mock.ensure_builder_fee_approved = AsyncMock(
            return_value=(True, "Builder fee approved: 0.030%")
        )
        mock.update_leverage = AsyncMock(return_value=(True, {"status": "ok"}))
        mock.transfer_perp_to_spot = AsyncMock(return_value=(True, {"status": "ok"}))
        mock.transfer_spot_to_perp = AsyncMock(return_value=(True, {"status": "ok"}))
        mock.place_market_order = AsyncMock(return_value=(True, {"status": "ok"}))
        mock.place_limit_order = AsyncMock(return_value=(True, {"status": "ok"}))
        mock.place_stop_loss = AsyncMock(return_value=(True, {"status": "ok"}))
        mock.cancel_order = AsyncMock(return_value=(True, {"status": "ok"}))
        mock.withdraw = AsyncMock(return_value=(True, {"status": "ok"}))
        mock.get_open_orders = AsyncMock(return_value=(True, []))
        mock.get_frontend_open_orders = AsyncMock(return_value=(True, []))
        mock.get_valid_order_size = MagicMock(side_effect=lambda _asset, size: size)
        mock.wait_for_deposit = AsyncMock(return_value=(True, 100.0))
        mock.wait_for_withdrawal = AsyncMock(
            # tx_hash -> amount (float)
            return_value=(True, {"0x123456": 100.0})
        )
        return mock

    @pytest.fixture
    def mock_hyperliquid_data_client(self):
        mock = MagicMock()
        n_points = 1200
        start_ms = 1700000000000
        step_ms = 3600 * 1000
        funding_data = [
            {"fundingRate": "0.0001", "time": start_ms + i * step_ms}
            for i in range(n_points)
        ]
        candle_data = [
            {
                "t": start_ms + i * step_ms,
                "o": "2000",
                "h": "2050",
                "l": "1980",
                "c": "2020",
            }
            for i in range(n_points)
        ]
        mock.get_funding_history = AsyncMock(return_value=funding_data)
        mock.get_candles = AsyncMock(return_value=candle_data)
        return mock

    @pytest.fixture
    def ledger_adapter(self, tmp_path):
        ledger_client = LedgerClient(ledger_dir=tmp_path)
        return LedgerAdapter(ledger_client=ledger_client)

    @pytest.fixture
    def strategy(
        self, mock_hyperliquid_adapter, mock_hyperliquid_data_client, ledger_adapter
    ):
        with patch(
            "wayfinder_paths.strategies.basis_trading_strategy.strategy.HyperliquidAdapter",
            return_value=mock_hyperliquid_adapter,
        ):
            with patch(
                "wayfinder_paths.strategies.basis_trading_strategy.strategy.BalanceAdapter"
            ):
                with patch(
                    "wayfinder_paths.strategies.basis_trading_strategy.strategy.TokenAdapter"
                ):
                    with patch(
                        "wayfinder_paths.core.strategies.Strategy.LedgerAdapter",
                        return_value=ledger_adapter,
                    ):
                        s = BasisTradingStrategy(
                            config={
                                "main_wallet": {"address": "0x1234"},
                                "strategy_wallet": {"address": "0x5678"},
                            },
                        )
                        s.hyperliquid_adapter = mock_hyperliquid_adapter
                        s._hyperliquid_data_client = mock_hyperliquid_data_client
                        s.ledger_adapter = ledger_adapter
                        s.balance_adapter = MagicMock()
                        s.balance_adapter.get_balance = AsyncMock(
                            return_value=(True, 0)
                        )
                        s.balance_adapter.move_from_main_wallet_to_strategy_wallet = (
                            AsyncMock(return_value=(True, {}))
                        )
                        s.balance_adapter.move_from_strategy_wallet_to_main_wallet = (
                            AsyncMock(return_value=(True, {}))
                        )
                        s.balance_adapter.send_to_address = AsyncMock(
                            return_value=(True, {"tx_hash": "0x123"})
                        )
                        # Mock internal dependencies to prevent MagicMock await errors
                        # These are needed if the real method somehow gets called
                        s.balance_adapter.token_client = AsyncMock()
                        s.balance_adapter.token_client.get_token_details = AsyncMock(
                            return_value={
                                "id": "usdc",
                                "address": "0x1234",
                                "decimals": 6,
                            }
                        )
                        s.balance_adapter.token_adapter = AsyncMock()
                        s.balance_adapter.token_adapter.get_token_price = AsyncMock(
                            return_value=(True, {"current_price": 1.0})
                        )
                        # ledger_adapter is real, but ensure its methods are async-mockable
                        s.balance_adapter.ledger_adapter = ledger_adapter
                        # Also ensure the balance_adapter's _move_between_wallets won't call real methods
                        # by making sure all its dependencies return AsyncMock
                        s.balance_adapter._move_between_wallets = AsyncMock(
                            return_value=(True, {"transaction_hash": "0x123"})
                        )
                        return s

    @pytest.mark.asyncio
    @pytest.mark.smoke
    async def test_smoke(self, strategy):
        examples = load_examples()
        smoke = examples["smoke"]

        # Mock PairedFiller for update() and withdraw() to work
        with patch(
            "wayfinder_paths.strategies.basis_trading_strategy.strategy.PairedFiller"
        ) as mock_filler_class:
            mock_filler = MagicMock()
            mock_filler.fill_pair_units = AsyncMock(
                return_value=(0.5, 0.5, 1000.0, 1000.0, [], [])
            )
            mock_filler_class.return_value = mock_filler

            # Deposit
            deposit_params = smoke.get("deposit", {})
            success, msg = assert_status_tuple(await strategy.deposit(**deposit_params))
            assert success, f"Deposit failed: {msg}"

            success, msg = assert_status_tuple(await strategy.update())
            assert success, f"Update failed: {msg}"

            # Status
            status = assert_status_dict(await strategy.status())
            assert "portfolio_value" in status

            # Withdraw (needs PairedFiller mock for _close_position)
            success, msg = assert_status_tuple(await strategy.withdraw())
            assert success, f"Withdraw failed: {msg}"

    @pytest.mark.asyncio
    async def test_quote_returns_quote_result(self, strategy):
        with patch.object(
            strategy,
            "find_best_trade_with_backtest",
            new_callable=AsyncMock,
        ) as mock_best:
            mock_best.return_value = None
            assert_quote_result(await strategy.quote(deposit_amount=1000.0))

    @pytest.mark.asyncio
    async def test_exit_returns_status_tuple(self, strategy):
        assert_status_tuple(await strategy.exit())

    @pytest.mark.asyncio
    async def test_deposit_minimum(self, strategy):
        examples = load_examples()
        min_fail = examples.get("min_deposit_fail", {})

        if min_fail:
            deposit_params = min_fail.get("deposit", {})
            success, msg = assert_status_tuple(await strategy.deposit(**deposit_params))

            expect = min_fail.get("expect", {})
            if expect.get("success") is False:
                assert success is False, "Expected deposit to fail"

    @pytest.mark.asyncio
    async def test_update_without_deposit(self, strategy, mock_hyperliquid_adapter):
        strategy.deposit_amount = 0.0

        # No USDC in perp withdrawable or spot.
        mock_hyperliquid_adapter.get_user_state = AsyncMock(
            return_value=(
                True,
                {
                    "marginSummary": {"accountValue": "0"},
                    "withdrawable": "0",
                    "assetPositions": [],
                },
            )
        )
        mock_hyperliquid_adapter.get_spot_user_state = AsyncMock(
            return_value=(True, {"balances": []})
        )

        success, msg = assert_status_tuple(await strategy.update())
        assert success is False
        assert "No funds to manage" in msg

    @pytest.mark.asyncio
    async def test_withdraw_without_deposit(self, strategy):
        success, msg = assert_status_tuple(await strategy.withdraw())
        assert success is False

    @pytest.mark.asyncio
    async def test_status(self, strategy):
        status = assert_status_dict(await strategy.status())
        assert "portfolio_value" in status
        assert "net_deposit" in status
        assert "strategy_status" in status

    @pytest.mark.asyncio
    async def test_ledger_records_snapshot(self, strategy, tmp_path):
        status = await strategy.status()
        assert status is not None

        # Verify snapshot was written to temp ledger
        snapshots_file = tmp_path / "snapshots.json"
        assert snapshots_file.exists()

        with open(snapshots_file) as f:
            data = json.load(f)

        assert len(data["snapshots"]) == 1
        snapshot = data["snapshots"][0]
        assert snapshot["wallet_address"] == "0x5678"
        assert snapshot["portfolio_value"] == status["portfolio_value"]

    def test_maintenance_rate(self, strategy):
        rate = strategy.maintenance_rate_from_max_leverage(50)
        assert rate == 0.01

        rate = strategy.maintenance_rate_from_max_leverage(10)
        assert rate == 0.05

    def test_rolling_min_sum(self, strategy):
        arr = [1, -2, 3, -4, 5]
        result = strategy._rolling_min_sum(arr, 2)
        assert result == -1

    def test_z_from_conf(self, strategy):
        z = strategy._z_from_conf(0.95)
        assert 1.9 < z < 2.0

        z = strategy._z_from_conf(0.99)
        assert 2.5 < z < 2.6

    @pytest.mark.asyncio
    async def test_build_batch_snapshot_and_filter(self, strategy):
        snap = await strategy.build_batch_snapshot(
            score_deposit_usdc=1000.0, bootstrap_sims=0
        )
        assert snap["kind"] == "basis_trading_batch_snapshot"
        assert "hour_bucket_utc" in snap
        assert isinstance(snap.get("candidates"), list)
        assert snap["candidates"], "Expected at least one candidate in snapshot"

        candidate = snap["candidates"][0]
        assert "liquidity" in candidate
        assert candidate["liquidity"]["max_order_usd"] > 0
        assert isinstance(candidate.get("options"), list) and candidate["options"]

        opps = strategy.opportunities_from_snapshot(snapshot=snap, deposit_usdc=1000.0)
        assert opps, "Expected opportunities from snapshot"
        assert opps[0]["selection"]["net_apy"] is not None

    @pytest.mark.asyncio
    async def test_get_undeployed_capital_empty(
        self, strategy, mock_hyperliquid_adapter
    ):
        mock_hyperliquid_adapter.get_user_state = AsyncMock(
            return_value=(
                True,
                {
                    "marginSummary": {"accountValue": "0", "withdrawable": "0"},
                    "withdrawable": "0",
                    "assetPositions": [],
                },
            )
        )
        mock_hyperliquid_adapter.get_spot_user_state = AsyncMock(
            return_value=(True, {"balances": []})
        )
        perp_margin, spot_usdc = await strategy._get_undeployed_capital()
        assert perp_margin == 0.0
        assert spot_usdc == 0.0

    @pytest.mark.asyncio
    async def test_get_undeployed_capital_with_margin(
        self, strategy, mock_hyperliquid_adapter
    ):
        mock_hyperliquid_adapter.get_user_state = AsyncMock(
            return_value=(
                True,
                {
                    "marginSummary": {"accountValue": "100", "withdrawable": "50"},
                    "assetPositions": [],
                },
            )
        )
        mock_hyperliquid_adapter.get_spot_user_state = AsyncMock(
            return_value=(
                True,
                {"balances": [{"coin": "USDC", "total": "25.5"}]},
            )
        )

        perp_margin, spot_usdc = await strategy._get_undeployed_capital()
        assert perp_margin == 50.0
        assert spot_usdc == 25.5

    @pytest.mark.asyncio
    async def test_scale_up_position_no_position(self, strategy):
        success, msg = await strategy._scale_up_position(100.0)
        assert success is False
        assert "No position to scale up" in msg

    @pytest.mark.asyncio
    async def test_scale_up_position_below_minimum(
        self, strategy, mock_hyperliquid_adapter
    ):
        strategy.current_position = BasisPosition(
            coin="ETH",
            spot_asset_id=10000,
            perp_asset_id=1,
            spot_amount=1.0,
            perp_amount=1.0,
            entry_price=2000.0,
            leverage=2,
            entry_timestamp=1700000000000,
            funding_collected=0.0,
        )

        # Try to scale with $5 (below $10 minimum notional)
        # With 2x leverage, order_usd = 5 * (2/3) = 3.33, below $10
        success, msg = await strategy._scale_up_position(5.0)
        assert success
        assert "below minimum notional" in msg

    @pytest.mark.asyncio
    async def test_update_with_idle_capital_scales_up(
        self, strategy, mock_hyperliquid_adapter
    ):
        strategy.deposit_amount = 100.0
        strategy.current_position = BasisPosition(
            coin="ETH",
            spot_asset_id=10000,
            perp_asset_id=1,
            spot_amount=0.03,
            perp_amount=0.03,
            entry_price=2000.0,
            leverage=2,
            entry_timestamp=1700000000000,
            funding_collected=0.0,
        )

        # Make sure there's deployable idle USDC without relying on marginSummary.withdrawable.
        # With 2x leverage and ~0.03 ETH:
        # - spot value ≈ 0.03 * 2000 = 60
        # - perp contrib ≈ (2000*(1+1/2) - 2000) * 0.03 = 30
        # - bankroll ≈ 30 + 60 + 20 = 110
        # - allocated ≈ 30 + 60 = 90
        # - unused ≈ 20 (deployable USDC) -> scale up
        mock_hyperliquid_adapter.get_user_state = AsyncMock(
            return_value=(
                True,
                {
                    "marginSummary": {
                        "accountValue": "30",
                        "withdrawable": "12",
                    },
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "ETH",
                                "szi": "-0.03",
                                "leverage": {"value": "2"},
                                "liquidationPx": "2500",
                                "entryPx": "2000",
                            }
                        }
                    ],
                },
            )
        )
        # Include ETH spot balance for leg balance check, plus USDC to deploy.
        mock_hyperliquid_adapter.get_spot_user_state = AsyncMock(
            return_value=(
                True,
                {
                    "balances": [
                        {"coin": "ETH", "total": "0.03"},
                        {"coin": "USDC", "total": "20"},
                    ]
                },
            )
        )
        mock_hyperliquid_adapter.get_valid_order_size = MagicMock(
            side_effect=lambda _aid, sz: sz
        )
        mock_hyperliquid_adapter.transfer_perp_to_spot = AsyncMock(
            return_value=(True, "ok")
        )
        mock_hyperliquid_adapter.get_open_orders = AsyncMock(
            return_value=(
                True,
                [
                    {
                        "coin": "ETH",
                        "orderType": "trigger",
                        "triggerPx": "2400",
                        "sz": "1.0",
                        "oid": 123,
                    }
                ],
            )
        )

        # Mock the paired filler to avoid actual execution
        with patch(
            "wayfinder_paths.strategies.basis_trading_strategy.strategy.PairedFiller"
        ) as mock_filler_class:
            mock_filler = MagicMock()
            mock_filler.fill_pair_units = AsyncMock(
                return_value=(0.5, 0.5, 1000.0, 1000.0, [], [])
            )
            mock_filler_class.return_value = mock_filler

            success, msg = assert_status_tuple(await strategy.update())

            # Should have called fill_pair_units to scale up
            assert mock_filler.fill_pair_units.called
            assert success

    @pytest.mark.asyncio
    async def test_update_does_not_scale_on_perp_pnl_margin_release(
        self, strategy, mock_hyperliquid_adapter
    ):
        """A favorable perp move can increase withdrawable margin; it should not trigger scale-up."""
        strategy.deposit_amount = 100.0
        strategy.current_position = BasisPosition(
            coin="ETH",
            spot_asset_id=10000,
            perp_asset_id=1,
            spot_amount=0.03,
            perp_amount=0.03,
            entry_price=2000.0,
            leverage=2,
            entry_timestamp=1700000000000,
            funding_collected=0.0,
        )

        # Price down benefits the short perp; withdrawable may rise, but unused cash is ~0.
        mock_hyperliquid_adapter.get_all_mid_prices = AsyncMock(
            return_value=(True, {"ETH": 1800.0, "BTC": 50000.0})
        )
        mock_hyperliquid_adapter.get_user_state = AsyncMock(
            return_value=(
                True,
                {
                    "marginSummary": {
                        "accountValue": "36",
                        "withdrawable": "12",
                    },
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "ETH",
                                "szi": "-0.03",
                                "leverage": {"value": "2"},
                                "liquidationPx": "2500",
                                "entryPx": "2000",
                            }
                        }
                    ],
                },
            )
        )
        mock_hyperliquid_adapter.get_spot_user_state = AsyncMock(
            return_value=(True, {"balances": [{"coin": "ETH", "total": "0.03"}]})
        )

        strategy._scale_up_position = AsyncMock(return_value=(True, "scaled"))

        success, _ = assert_status_tuple(await strategy.update())
        assert success
        strategy._scale_up_position.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_includes_rotation_cooldown_hint(
        self, strategy, mock_hyperliquid_adapter
    ):
        strategy.deposit_amount = 100.0
        strategy.current_position = BasisPosition(
            coin="ETH",
            spot_asset_id=10000,
            perp_asset_id=1,
            spot_amount=0.03,
            perp_amount=0.03,
            entry_price=2000.0,
            leverage=2,
            entry_timestamp=1700000000000,
            funding_collected=0.0,
        )

        mock_hyperliquid_adapter.get_user_state = AsyncMock(
            return_value=(
                True,
                {
                    "marginSummary": {
                        "accountValue": "0",
                        "withdrawable": "0",
                    },
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "ETH",
                                "szi": "-0.03",
                                "leverage": {"value": "2"},
                                "liquidationPx": "2500",
                                "entryPx": "2000",
                            }
                        }
                    ],
                },
            )
        )
        mock_hyperliquid_adapter.get_spot_user_state = AsyncMock(
            return_value=(True, {"balances": [{"coin": "ETH", "total": "0.03"}]})
        )

        strategy._needs_new_position = AsyncMock(
            return_value=(False, "Position healthy")
        )
        strategy._verify_leg_balance = AsyncMock(return_value=(True, "ok"))
        strategy._unused_usd_now = AsyncMock(return_value=(0.0, 100.0))
        strategy._ensure_stop_loss_valid = AsyncMock(
            return_value=(True, "Stop-loss ok")
        )
        strategy._rotation_cooldown_hint = AsyncMock(return_value="3d 4h remaining")

        success, msg = assert_status_tuple(await strategy.update())
        assert success
        assert "Rotation: 3d 4h remaining" in msg

    @pytest.mark.asyncio
    async def test_ensure_builder_fee_approved_already_approved(
        self, mock_hyperliquid_adapter, ledger_adapter
    ):
        with patch(
            "wayfinder_paths.strategies.basis_trading_strategy.strategy.HyperliquidAdapter",
            return_value=mock_hyperliquid_adapter,
        ):
            with patch(
                "wayfinder_paths.strategies.basis_trading_strategy.strategy.BalanceAdapter"
            ):
                with patch(
                    "wayfinder_paths.strategies.basis_trading_strategy.strategy.TokenAdapter"
                ):
                    with patch(
                        "wayfinder_paths.core.strategies.Strategy.LedgerAdapter",
                        return_value=ledger_adapter,
                    ):
                        s = BasisTradingStrategy(
                            config={
                                "main_wallet": {"address": "0x1234"},
                                "strategy_wallet": {"address": "0x5678"},
                            },
                        )
                        s.hyperliquid_adapter = mock_hyperliquid_adapter
                        s.ledger_adapter = ledger_adapter

                        # Mock ensure_builder_fee_approved returning already approved
                        mock_hyperliquid_adapter.ensure_builder_fee_approved = (
                            AsyncMock(
                                return_value=(
                                    True,
                                    "Builder fee already approved (30 >= 30)",
                                )
                            )
                        )

                        success, msg = await s.ensure_builder_fee_approved()
                        assert success
                        assert "already approved" in msg.lower()
                        mock_hyperliquid_adapter.ensure_builder_fee_approved.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_builder_fee_approved_needs_approval(
        self, mock_hyperliquid_adapter, ledger_adapter
    ):
        with patch(
            "wayfinder_paths.strategies.basis_trading_strategy.strategy.HyperliquidAdapter",
            return_value=mock_hyperliquid_adapter,
        ):
            with patch(
                "wayfinder_paths.strategies.basis_trading_strategy.strategy.BalanceAdapter"
            ):
                with patch(
                    "wayfinder_paths.strategies.basis_trading_strategy.strategy.TokenAdapter"
                ):
                    with patch(
                        "wayfinder_paths.core.strategies.Strategy.LedgerAdapter",
                        return_value=ledger_adapter,
                    ):
                        s = BasisTradingStrategy(
                            config={
                                "main_wallet": {"address": "0x1234"},
                                "strategy_wallet": {"address": "0x5678"},
                            },
                        )
                        s.hyperliquid_adapter = mock_hyperliquid_adapter
                        s.ledger_adapter = ledger_adapter

                        # Mock ensure_builder_fee_approved returning newly approved
                        mock_hyperliquid_adapter.ensure_builder_fee_approved = (
                            AsyncMock(
                                return_value=(True, "Builder fee approved: 0.030%")
                            )
                        )

                        success, msg = await s.ensure_builder_fee_approved()
                        assert success
                        assert "approved" in msg.lower()
                        mock_hyperliquid_adapter.ensure_builder_fee_approved.assert_called_once()

    @pytest.mark.asyncio
    async def test_portfolio_value_includes_spot_holdings(
        self, strategy, mock_hyperliquid_adapter
    ):
        # Perp account has $100
        mock_hyperliquid_adapter.get_user_state = AsyncMock(
            return_value=(
                True,
                {"marginSummary": {"accountValue": "100"}, "assetPositions": []},
            )
        )
        # Spot has 50 USDC + 0.5 ETH
        mock_hyperliquid_adapter.get_spot_user_state = AsyncMock(
            return_value=(
                True,
                {
                    "balances": [
                        {"coin": "USDC", "total": "50"},
                        {"coin": "ETH", "total": "0.5"},
                    ]
                },
            )
        )
        # ETH price is $2000
        mock_hyperliquid_adapter.get_all_mid_prices = AsyncMock(
            return_value=(True, {"ETH": 2000.0, "BTC": 50000.0})
        )

        total, hl_value, vault_value = await strategy._get_total_portfolio_value()
        # 100 (perp) + 50 (USDC) + 0.5*2000 (ETH) = 1150
        assert hl_value == 1150.0
        assert total == 1150.0

    @pytest.mark.asyncio
    async def test_portfolio_value_usdc_only(self, strategy, mock_hyperliquid_adapter):
        mock_hyperliquid_adapter.get_user_state = AsyncMock(
            return_value=(
                True,
                {"marginSummary": {"accountValue": "0"}, "assetPositions": []},
            )
        )
        mock_hyperliquid_adapter.get_spot_user_state = AsyncMock(
            return_value=(
                True,
                {"balances": [{"coin": "USDC", "total": "100"}]},
            )
        )
        # Should not need mid prices when only USDC
        mock_hyperliquid_adapter.get_all_mid_prices = AsyncMock(return_value=(True, {}))

        total, hl_value, vault_value = await strategy._get_total_portfolio_value()
        assert hl_value == 100.0
        assert total == 100.0

    @pytest.mark.asyncio
    async def test_withdraw_detects_spot_usdc(self, strategy, mock_hyperliquid_adapter):
        # Perp is empty
        mock_hyperliquid_adapter.get_user_state = AsyncMock(
            return_value=(
                True,
                {
                    "marginSummary": {"accountValue": "0"},
                    "withdrawable": "0",
                    "assetPositions": [],
                },
            )
        )
        # Spot has 100 USDC
        mock_hyperliquid_adapter.get_spot_user_state = AsyncMock(
            return_value=(
                True,
                {"balances": [{"coin": "USDC", "total": "100"}]},
            )
        )

        success, msg = assert_status_tuple(await strategy.withdraw())
        # Should NOT return "Nothing to withdraw" since there's USDC in spot
        assert "Nothing to withdraw" not in msg

    @pytest.mark.asyncio
    async def test_update_detects_hl_balance_when_deposit_zero(
        self, strategy, mock_hyperliquid_adapter
    ):
        strategy.deposit_amount = 0

        # Hyperliquid has $50 in perp account
        mock_hyperliquid_adapter.get_user_state = AsyncMock(
            return_value=(
                True,
                {
                    "marginSummary": {"accountValue": "50", "withdrawable": "50"},
                    "assetPositions": [],
                },
            )
        )

        # Run update - it should detect the balance
        assert_status_tuple(await strategy.update())

        # deposit_amount should now be set from detected balance
        assert strategy.deposit_amount == 50.0

    @pytest.mark.asyncio
    async def test_update_spot_usdc_only_rebalances_before_open(
        self, strategy, mock_hyperliquid_adapter
    ):
        strategy.deposit_amount = 0.0
        strategy.current_position = None

        # Perp account has $0 withdrawable, spot has $100 USDC
        mock_hyperliquid_adapter.get_user_state = AsyncMock(
            return_value=(
                True,
                {
                    "marginSummary": {"accountValue": "0"},
                    "withdrawable": "0",
                    "assetPositions": [],
                },
            )
        )
        mock_hyperliquid_adapter.get_spot_user_state = AsyncMock(
            return_value=(
                True,
                {"balances": [{"coin": "USDC", "total": "100"}]},
            )
        )

        # Avoid running the full solver; return a deterministic best trade.
        strategy.find_best_trade_with_backtest = AsyncMock(
            return_value={
                "coin": "ETH",
                "spot_asset_id": 10000,
                "perp_asset_id": 1,
                "net_apy": 0.1,
                "best_L": 2,
                "safe": {
                    "7": {
                        "safe_leverage": 2,
                        "spot_usdc": 66.67,
                        "spot_amount": 0.033335,
                        "perp_amount": 0.033335,
                    }
                },
            }
        )

        # Mock the paired filler to avoid actual execution
        with patch(
            "wayfinder_paths.strategies.basis_trading_strategy.strategy.PairedFiller"
        ) as mock_filler_class:
            mock_filler = MagicMock()
            mock_filler.fill_pair_units = AsyncMock(
                return_value=(0.5, 0.5, 1000.0, 1000.0, [], [])
            )
            mock_filler_class.return_value = mock_filler

            success, _ = assert_status_tuple(await strategy.update())
            assert success

        # Target spot was $66.67, so we should transfer $33.33 spot->perp.
        mock_hyperliquid_adapter.transfer_spot_to_perp.assert_called_once()
        _, kwargs = mock_hyperliquid_adapter.transfer_spot_to_perp.call_args
        assert kwargs["address"] == "0x5678"
        assert abs(kwargs["amount"] - 33.33) < 1e-6

        # Should not attempt perp->spot when spot already has sufficient USDC.
        mock_hyperliquid_adapter.transfer_perp_to_spot.assert_not_called()

        assert strategy.deposit_amount == 100.0

    @pytest.mark.asyncio
    async def test_update_near_liquidation_closes_and_redeploys(
        self, strategy, mock_hyperliquid_adapter
    ):
        strategy.deposit_amount = 100.0
        strategy.current_position = BasisPosition(
            coin="HYPE",
            spot_asset_id=10107,
            perp_asset_id=7,
            spot_amount=1.0,
            perp_amount=1.0,
            entry_price=100.0,
            leverage=2,
            entry_timestamp=1700000000000,
            funding_collected=0.0,
        )

        # Price is exactly 75% of the way from entry -> liquidation:
        # (175 - 100) / (200 - 100) = 0.75
        mock_hyperliquid_adapter.get_all_mid_prices = AsyncMock(
            return_value=(True, {"HYPE": 175.0})
        )
        mock_hyperliquid_adapter.get_user_state = AsyncMock(
            return_value=(
                True,
                {
                    "marginSummary": {
                        "accountValue": "100",
                        "withdrawable": "0",
                        "totalNtlPos": "100",
                    },
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "HYPE",
                                "szi": "-1.0",
                                "entryPx": "100",
                                "liquidationPx": "200",
                            }
                        }
                    ],
                },
            )
        )
        mock_hyperliquid_adapter.get_spot_user_state = AsyncMock(
            return_value=(
                True,
                {"balances": [{"coin": "HYPE", "total": "1.0"}]},
            )
        )

        # Ensure cooldown would block a normal rebalance, but emergency should bypass it.
        strategy._is_rotation_allowed = AsyncMock(return_value=(False, "cooldown"))
        strategy._close_position = AsyncMock(return_value=(True, "closed"))
        strategy._find_and_open_position = AsyncMock(return_value=(True, "redeployed"))

        success, msg = assert_status_tuple(await strategy.update())
        assert success
        assert msg == "redeployed"
        strategy._close_position.assert_awaited_once()
        strategy._find_and_open_position.assert_awaited_once()
        strategy._is_rotation_allowed.assert_not_called()

    @pytest.mark.asyncio
    async def test_net_deposit_handles_float_return(self, strategy):
        # Mock ledger adapter to return a float (not a dict)
        strategy.ledger_adapter.get_strategy_net_deposit = AsyncMock(
            return_value=(True, 1500.0)
        )

        status = await strategy.status()

        # Verify net_deposit is correctly set from the float
        assert status["net_deposit"] == 1500.0

    @pytest.mark.asyncio
    async def test_setup_handles_float_net_deposit(
        self, mock_hyperliquid_adapter, ledger_adapter
    ):
        with patch(
            "wayfinder_paths.strategies.basis_trading_strategy.strategy.HyperliquidAdapter",
            return_value=mock_hyperliquid_adapter,
        ):
            with patch(
                "wayfinder_paths.strategies.basis_trading_strategy.strategy.BalanceAdapter"
            ):
                with patch(
                    "wayfinder_paths.strategies.basis_trading_strategy.strategy.TokenAdapter"
                ):
                    with patch(
                        "wayfinder_paths.core.strategies.Strategy.LedgerAdapter",
                        return_value=ledger_adapter,
                    ):
                        s = BasisTradingStrategy(
                            config={
                                "main_wallet": {"address": "0x1234"},
                                "strategy_wallet": {"address": "0x5678"},
                            },
                        )
                        s.hyperliquid_adapter = mock_hyperliquid_adapter
                        s.ledger_adapter = ledger_adapter

                        # Mock get_strategy_net_deposit to return float (not dict)
                        s.ledger_adapter.get_strategy_net_deposit = AsyncMock(
                            return_value=(True, 2500.0)
                        )

                        # Run setup - should not raise AttributeError
                        await s.setup()

                        # Verify deposit_amount was set from the float
                        assert s.deposit_amount == 2500.0
