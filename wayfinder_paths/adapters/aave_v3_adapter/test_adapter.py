from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.adapters.aave_v3_adapter.adapter import AaveV3Adapter
from wayfinder_paths.core.constants.aave_v3_abi import (
    UI_POOL_RESERVE_KEYS,
    UI_POOL_RESERVE_KEYS_LEGACY,
    UI_POOL_RESERVE_KEYS_ORIGIN,
)
from wayfinder_paths.core.constants.contracts import ZERO_ADDRESS

FAKE_ADDR = "0x1234567890123456789012345678901234567890"
FAKE_ASSET = "0x0000000000000000000000000000000000000001"
FAKE_VAULT = "0x0000000000000000000000000000000000000002"
FAKE_ATOKEN = "0x0000000000000000000000000000000000000003"


def _async_call(value):
    return MagicMock(call=AsyncMock(return_value=value))


class TestAaveV3Adapter:
    @pytest.fixture
    def adapter(self):
        return AaveV3Adapter(
            config={},
            wallet_address=FAKE_ADDR,
        )

    def test_adapter_type(self, adapter):
        assert adapter.adapter_type == "AAVE_V3"

    def test_strategy_address_optional(self):
        adapter = AaveV3Adapter(config={})
        assert adapter.wallet_address is None

    def test_ui_pool_reserve_keys_match_current_v3_tail_order(self):
        assert UI_POOL_RESERVE_KEYS[-10:] == [
            "isolationModeTotalDebt",
            "flashLoanEnabled",
            "debtCeiling",
            "debtCeilingDecimals",
            "eModeCategoryId",
            "borrowCap",
            "supplyCap",
            "borrowableInIsolation",
            "virtualAccActive",
            "virtualUnderlyingBalance",
        ]
        assert UI_POOL_RESERVE_KEYS_ORIGIN[-10:] == [
            "accruedToTreasury",
            "isolationModeTotalDebt",
            "flashLoanEnabled",
            "debtCeiling",
            "debtCeilingDecimals",
            "borrowCap",
            "supplyCap",
            "borrowableInIsolation",
            "virtualUnderlyingBalance",
            "deficit",
        ]
        assert UI_POOL_RESERVE_KEYS_LEGACY[-10:] == [
            "debtCeilingDecimals",
            "eModeCategoryId",
            "borrowCap",
            "supplyCap",
            "eModeLtv",
            "eModeLiquidationThreshold",
            "eModeLiquidationBonus",
            "eModePriceSource",
            "eModeLabel",
            "borrowableInIsolation",
        ]

    @pytest.mark.asyncio
    async def test_get_all_markets_basic(self, adapter):
        reserve_keys = UI_POOL_RESERVE_KEYS

        def build_reserve(**overrides):
            base = dict.fromkeys(reserve_keys, 0)
            base.update(
                {
                    "underlyingAsset": "0x0000000000000000000000000000000000000011",
                    "name": "",
                    "symbol": "USDC",
                    "decimals": 6,
                    "usageAsCollateralEnabled": True,
                    "borrowingEnabled": True,
                    "isActive": True,
                    "isFrozen": False,
                    "isPaused": False,
                    "isSiloedBorrowing": False,
                    "aTokenAddress": "0x00000000000000000000000000000000000000a1",
                    "variableDebtTokenAddress": "0x00000000000000000000000000000000000000b1",
                    "priceInMarketReferenceCurrency": 100000000,
                    "availableLiquidity": 5_000_000,
                    "totalScaledVariableDebt": 0,
                    "variableBorrowIndex": 10**27,
                    "liquidityRate": int(0.05 * 10**27),
                    "variableBorrowRate": int(0.10 * 10**27),
                    "supplyCap": 0,
                    "borrowCap": 0,
                    "baseLTVasCollateral": 8000,
                    "reserveLiquidationThreshold": 8500,
                    "reserveLiquidationBonus": 10500,
                    "reserveFactor": 1000,
                    "flashLoanEnabled": True,
                    "debtCeiling": 12345,
                    "debtCeilingDecimals": 2,
                    "eModeCategoryId": 1,
                    "borrowableInIsolation": True,
                    "virtualAccActive": True,
                    "virtualUnderlyingBalance": 1_000_000,
                    "isolationModeTotalDebt": 234,
                    "unbacked": 345,
                    "accruedToTreasury": 456,
                    "lastUpdateTimestamp": 1_700_000_000,
                    "priceOracle": "0x00000000000000000000000000000000000000c1",
                    "interestRateStrategyAddress": "0x00000000000000000000000000000000000000d1",
                    "variableRateSlope1": 111,
                    "variableRateSlope2": 222,
                    "baseVariableBorrowRate": 333,
                    "optimalUsageRatio": 444,
                }
            )
            base.update(overrides)
            return tuple(base[k] for k in reserve_keys)

        reserves = [
            build_reserve(
                underlyingAsset="0x0000000000000000000000000000000000000011",
                symbol="USDC",
                priceInMarketReferenceCurrency=100000000,
                availableLiquidity=5_000_000,
                supplyCap=0,
            ),
            build_reserve(
                underlyingAsset="0x0000000000000000000000000000000000000022",
                symbol="uSOL",
                decimals=6,
                priceInMarketReferenceCurrency=2000000000,
                availableLiquidity=10_000_000,
                totalScaledVariableDebt=5_000_000,
                variableBorrowIndex=10**27,
                supplyCap=100,
            ),
        ]
        base_currency = (100000000, 100000000, 0, 8)  # ref_unit=1e8, ref_usd=1.0

        mock_ui_pool = MagicMock()
        mock_ui_pool.functions.getReservesData = MagicMock(
            return_value=MagicMock(
                call=AsyncMock(return_value=(reserves, base_currency))
            )
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_ui_pool)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.aave_v3_adapter.adapter.web3_utils.web3_from_chain_id",
            mock_web3_ctx,
        ):
            ok, markets = await adapter.get_all_markets(
                chain_id=42161, include_rewards=False
            )

        assert ok is True
        assert isinstance(markets, list)
        assert len(markets) == 2

        usol = next(m for m in markets if m["symbol"].lower() == "usol")
        assert usol["price_usd"] == 20.0
        assert usol["supply_cap"] == 100
        assert usol["supply_cap_headroom"] == 85_000_000
        assert usol["ltv_bps"] == 8000
        assert usol["liquidation_threshold_bps"] == 8500
        assert usol["liquidation_bonus_bps"] == 10500
        assert usol["reserve_factor_bps"] == 1000
        assert usol["flash_loan_enabled"] is True
        assert usol["debt_ceiling"] == 12345
        assert usol["debt_ceiling_decimals"] == 2
        assert usol["emode_category_id"] == 1
        assert usol["borrowable_in_isolation"] is True
        assert usol["virtual_accounting_active"] is True
        assert usol["virtual_underlying_balance"] == 1_000_000
        assert usol["isolation_mode_total_debt"] == 234
        assert usol["unbacked"] == 345
        assert usol["accrued_to_treasury"] == 456
        assert usol["last_update_timestamp"] == 1_700_000_000
        assert usol["variable_rate_slope1"] == 111
        assert usol["variable_rate_slope2"] == 222
        assert usol["base_variable_borrow_rate"] == 333
        assert usol["optimal_usage_ratio"] == 444

    @pytest.mark.asyncio
    async def test_get_all_markets_includes_rewards(self, adapter):
        reserve_keys = UI_POOL_RESERVE_KEYS

        def build_reserve(**overrides):
            base = dict.fromkeys(reserve_keys, 0)
            base.update(
                {
                    "underlyingAsset": "0x0000000000000000000000000000000000000011",
                    "name": "",
                    "symbol": "USDC",
                    "decimals": 6,
                    "usageAsCollateralEnabled": True,
                    "borrowingEnabled": True,
                    "isActive": True,
                    "isFrozen": False,
                    "isPaused": False,
                    "isSiloedBorrowing": False,
                    "aTokenAddress": "0x00000000000000000000000000000000000000a1",
                    "variableDebtTokenAddress": "0x00000000000000000000000000000000000000b1",
                    "priceInMarketReferenceCurrency": 100000000,
                    "availableLiquidity": 5_000_000,
                    "totalScaledVariableDebt": 0,
                    "variableBorrowIndex": 10**27,
                    "liquidityRate": int(0.00 * 10**27),
                    "variableBorrowRate": int(0.00 * 10**27),
                    "supplyCap": 0,
                }
            )
            base.update(overrides)
            return tuple(base[k] for k in reserve_keys)

        reserves = [build_reserve()]
        base_currency = (100000000, 100000000, 0, 8)

        reward_info = (
            "OP",
            "0x00000000000000000000000000000000000000c1",
            "0x00000000000000000000000000000000000000d1",
            10**12,  # emissionPerSecond
            0,
            0,
            0,
            100000000,  # $1.00 with 8 decimals
            18,
            18,
            8,
        )
        a_inc = (
            "0x00000000000000000000000000000000000000a1",
            "0x00000000000000000000000000000000000000e1",
            [reward_info],
        )
        v_inc = (
            "0x00000000000000000000000000000000000000b1",
            "0x00000000000000000000000000000000000000e1",
            [],
        )
        incentives_rows = [
            (
                "0x0000000000000000000000000000000000000011",
                a_inc,
                v_inc,
            )
        ]

        mock_ui_pool = MagicMock()
        mock_ui_pool.functions.getReservesData = MagicMock(
            return_value=MagicMock(
                call=AsyncMock(return_value=(reserves, base_currency))
            )
        )

        mock_ui_incentives = MagicMock()
        mock_ui_incentives.functions.getReservesIncentivesData = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=incentives_rows))
        )

        mock_web3 = MagicMock()

        def contract_side_effect(*, address, abi):  # noqa: ARG001
            if (
                abi
                and isinstance(abi, list)
                and any(
                    x.get("name") == "getReservesIncentivesData"
                    for x in abi
                    if isinstance(x, dict)
                )
            ):
                return mock_ui_incentives
            return mock_ui_pool

        mock_web3.eth.contract = MagicMock(side_effect=contract_side_effect)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.aave_v3_adapter.adapter.web3_utils.web3_from_chain_id",
            mock_web3_ctx,
        ):
            ok, markets = await adapter.get_all_markets(
                chain_id=42161, include_rewards=True
            )

        assert ok is True
        assert isinstance(markets, list)
        assert markets[0]["reward_supply_apr"] > 0
        assert markets[0]["incentives"]

    @pytest.mark.asyncio
    async def test_get_full_user_state_basic(self, adapter):
        reserve_keys = UI_POOL_RESERVE_KEYS

        def build_reserve(**overrides):
            base = dict.fromkeys(reserve_keys, 0)
            base.update(
                {
                    "underlyingAsset": "0x0000000000000000000000000000000000000011",
                    "name": "",
                    "symbol": "USDC",
                    "decimals": 6,
                    "usageAsCollateralEnabled": True,
                    "borrowingEnabled": True,
                    "isActive": True,
                    "isFrozen": False,
                    "isPaused": False,
                    "isSiloedBorrowing": False,
                    "aTokenAddress": "0x00000000000000000000000000000000000000a1",
                    "variableDebtTokenAddress": "0x00000000000000000000000000000000000000b1",
                    "priceInMarketReferenceCurrency": 100000000,
                    "liquidityIndex": 10**27,
                    "variableBorrowIndex": 10**27,
                }
            )
            base.update(overrides)
            return tuple(base[k] for k in reserve_keys)

        reserves = [build_reserve()]
        base_currency = (100000000, 100000000, 0, 8)
        user_reserves = [
            (
                "0x0000000000000000000000000000000000000011",
                2_000_000,  # scaledATokenBalance
                True,  # usageAsCollateralEnabledOnUser
                1_000_000,  # scaledVariableDebt
            )
        ]

        mock_ui_pool = MagicMock()
        mock_ui_pool.functions.getReservesData = MagicMock(
            return_value=MagicMock(
                call=AsyncMock(return_value=(reserves, base_currency))
            )
        )
        mock_ui_pool.functions.getUserReservesData = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=(user_reserves, 0)))
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_ui_pool)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.aave_v3_adapter.adapter.web3_utils.web3_from_chain_id",
            mock_web3_ctx,
        ):
            ok, state = await adapter.get_full_user_state_per_chain(
                chain_id=42161,
                account="0x1234567890123456789012345678901234567890",
                include_rewards=False,
            )

        assert ok is True
        assert isinstance(state, dict)
        assert state["protocol"] == "aave_v3"
        assert state["positions"]
        pos = state["positions"][0]
        assert pos["supply_raw"] == 2_000_000
        assert pos["variable_borrow_raw"] == 1_000_000

    @pytest.mark.asyncio
    async def test_get_full_user_state_includes_account_data_and_emodes(self, adapter):
        reserve_keys = UI_POOL_RESERVE_KEYS

        def build_reserve(**overrides):
            base = dict.fromkeys(reserve_keys, 0)
            base.update(
                {
                    "underlyingAsset": "0x0000000000000000000000000000000000000011",
                    "name": "",
                    "symbol": "USDC",
                    "decimals": 6,
                    "usageAsCollateralEnabled": True,
                    "borrowingEnabled": True,
                    "isActive": True,
                    "isFrozen": False,
                    "isPaused": False,
                    "isSiloedBorrowing": True,
                    "borrowableInIsolation": True,
                    "aTokenAddress": "0x00000000000000000000000000000000000000a1",
                    "variableDebtTokenAddress": "0x00000000000000000000000000000000000000b1",
                    "priceInMarketReferenceCurrency": 100000000,
                    "liquidityIndex": 10**27,
                    "variableBorrowIndex": 10**27,
                    "baseLTVasCollateral": 8000,
                    "reserveLiquidationThreshold": 8500,
                    "reserveLiquidationBonus": 10500,
                    "eModeCategoryId": 1,
                    "debtCeiling": 1000,
                    "debtCeilingDecimals": 2,
                    "isolationModeTotalDebt": 12,
                }
            )
            base.update(overrides)
            return tuple(base[k] for k in reserve_keys)

        mock_ui_pool = MagicMock()
        mock_ui_pool.functions.getReservesData = MagicMock(
            return_value=_async_call(([build_reserve()], (100000000, 100000000, 0, 8)))
        )
        mock_ui_pool.functions.getUserReservesData = MagicMock(
            return_value=_async_call(
                (
                    [
                        (
                            "0x0000000000000000000000000000000000000011",
                            2_000_000,
                            True,
                            1_000_000,
                        )
                    ],
                    1,
                )
            )
        )
        mock_ui_pool.functions.getEModes = MagicMock(
            return_value=_async_call(
                [
                    (
                        1,
                        (
                            9300,
                            9500,
                            10100,
                            7,
                            "Stablecoins",
                            3,
                        ),
                    )
                ]
            )
        )

        mock_pool = MagicMock()
        mock_pool.functions.getUserAccountData = MagicMock(
            return_value=_async_call((500, 100, 300, 8500, 8000, 2 * 10**18))
        )

        mock_web3 = MagicMock()

        def contract_side_effect(*, address, abi):  # noqa: ARG001
            if (
                abi
                and isinstance(abi, list)
                and any(
                    x.get("name") == "getUserAccountData"
                    for x in abi
                    if isinstance(x, dict)
                )
            ):
                return mock_pool
            return mock_ui_pool

        mock_web3.eth.contract = MagicMock(side_effect=contract_side_effect)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.aave_v3_adapter.adapter.web3_utils.web3_from_chain_id",
            mock_web3_ctx,
        ):
            ok, state = await adapter.get_full_user_state_per_chain(
                chain_id=42161,
                account="0x1234567890123456789012345678901234567890",
                include_rewards=False,
            )

        assert ok is True
        assert isinstance(state, dict)
        assert state["user_emode_category_id"] == 1
        assert state["account_data"]["health_factor"] == 2 * 10**18
        assert state["account_data"]["available_borrows_base"] == 300
        assert state["emode_categories"] == [
            {
                "id": 1,
                "ltv_bps": 9300,
                "liquidation_threshold_bps": 9500,
                "liquidation_bonus_bps": 10100,
                "collateral_bitmap": 7,
                "borrowable_bitmap": 3,
                "label": "Stablecoins",
            }
        ]
        pos = state["positions"][0]
        assert pos["emode_category_id"] == 1
        assert pos["is_siloed_borrowing"] is True
        assert pos["borrowable_in_isolation"] is True
        assert pos["debt_ceiling"] == 1000

    @pytest.mark.asyncio
    async def test_claim_all_rewards_encodes_tx(self, adapter):
        incentives_rows = [
            (
                "0x0000000000000000000000000000000000000011",
                (
                    "0x00000000000000000000000000000000000000a1",
                    "0x00000000000000000000000000000000000000e1",
                    [],
                ),
                (
                    "0x00000000000000000000000000000000000000b1",
                    "0x00000000000000000000000000000000000000e1",
                    [],
                ),
            )
        ]

        mock_ui_incentives = MagicMock()
        mock_ui_incentives.functions.getReservesIncentivesData = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=incentives_rows))
        )

        mock_web3 = MagicMock()

        def contract_side_effect(*, address, abi):  # noqa: ARG001
            return mock_ui_incentives

        mock_web3.eth.contract = MagicMock(side_effect=contract_side_effect)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with (
            patch(
                "wayfinder_paths.adapters.aave_v3_adapter.adapter.web3_utils.web3_from_chain_id",
                mock_web3_ctx,
            ),
            patch(
                "wayfinder_paths.adapters.aave_v3_adapter.adapter.encode_call",
                AsyncMock(return_value={"data": "0xdeadbeef"}),
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.aave_v3_adapter.adapter.send_transaction",
                AsyncMock(return_value="0xtx"),
            ),
        ):
            ok, tx = await adapter.claim_all_rewards(chain_id=42161)

        assert ok is True
        assert tx == "0xtx"
        assert mock_encode.await_count == 1

    @pytest.mark.asyncio
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.send_transaction",
        new_callable=AsyncMock,
        return_value="0xabc",
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.ensure_allowance",
        new_callable=AsyncMock,
        return_value=(True, "ok"),
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.get_token_balance",
        new_callable=AsyncMock,
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.encode_call",
        new_callable=AsyncMock,
        return_value={"to": FAKE_ADDR},
    )
    async def test_repay_full_erc20_uses_debt_plus_buffer_amount(
        self,
        mock_encode,
        mock_balance,
        mock_allowance,
        _mock_send,
        adapter,
    ):
        adapter._variable_debt_token = AsyncMock(
            return_value="0x00000000000000000000000000000000000000dE"
        )
        mock_balance.side_effect = [123, 200]

        ok, tx = await adapter.repay(
            chain_id=42161,
            underlying_token=FAKE_ASSET,
            qty=0,
            repay_full=True,
        )

        assert ok is True
        assert tx == "0xabc"
        assert mock_allowance.await_args.kwargs["amount"] == 124
        assert mock_encode.await_args.kwargs["args"][1] == 124

    @pytest.mark.asyncio
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.ensure_allowance",
        new_callable=AsyncMock,
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.get_token_balance",
        new_callable=AsyncMock,
    )
    async def test_repay_full_erc20_requires_enough_wallet_balance(
        self,
        mock_balance,
        mock_allowance,
        adapter,
    ):
        adapter._variable_debt_token = AsyncMock(
            return_value="0x00000000000000000000000000000000000000dE"
        )
        mock_balance.side_effect = [123, 122]

        ok, message = await adapter.repay(
            chain_id=42161,
            underlying_token=FAKE_ASSET,
            qty=0,
            repay_full=True,
        )

        assert ok is False
        assert "insufficient token balance for repay_full" in message
        mock_allowance.assert_not_awaited()

    # ---- native via ZERO_ADDRESS ----

    @pytest.mark.asyncio
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.send_transaction",
        new_callable=AsyncMock,
        return_value="0xabc",
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.ensure_allowance",
        new_callable=AsyncMock,
        return_value=(True, "ok"),
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.encode_call",
        new_callable=AsyncMock,
        return_value={"to": FAKE_ADDR},
    )
    async def test_lend_native_via_zero_address(
        self, _mock_encode, _mock_allow, _mock_send, adapter
    ):
        adapter._wrapped_native = AsyncMock(return_value=FAKE_ASSET)
        ok, result = await adapter.lend(
            chain_id=42161, underlying_token=ZERO_ADDRESS, qty=100
        )
        assert ok is True
        assert result["wrap_tx"] == "0xabc"
        assert result["supply_tx"] == "0xabc"

    @pytest.mark.asyncio
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.send_transaction",
        new_callable=AsyncMock,
        return_value="0xabc",
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.get_token_balance",
        new_callable=AsyncMock,
        return_value=200,
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.encode_call",
        new_callable=AsyncMock,
        return_value={"to": FAKE_ADDR},
    )
    async def test_unlend_native_via_zero_address(
        self, _mock_encode, _mock_balance, _mock_send, adapter
    ):
        adapter._wrapped_native = AsyncMock(return_value=FAKE_ASSET)
        ok, result = await adapter.unlend(
            chain_id=42161, underlying_token=ZERO_ADDRESS, qty=100
        )
        assert ok is True
        assert result["withdraw_tx"] == "0xabc"

    @pytest.mark.asyncio
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.send_transaction",
        new_callable=AsyncMock,
        return_value="0xabc",
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.encode_call",
        new_callable=AsyncMock,
        return_value={"to": FAKE_ADDR},
    )
    async def test_borrow_native_via_zero_address(
        self, _mock_encode, _mock_send, adapter
    ):
        adapter._wrapped_native = AsyncMock(return_value=FAKE_ASSET)
        ok, result = await adapter.borrow(
            chain_id=42161, underlying_token=ZERO_ADDRESS, qty=100
        )
        assert ok is True
        assert result["borrow_tx"] == "0xabc"
        assert result["unwrap_tx"] == "0xabc"

    @pytest.mark.asyncio
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.send_transaction",
        new_callable=AsyncMock,
        return_value="0xabc",
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.ensure_allowance",
        new_callable=AsyncMock,
        return_value=(True, "ok"),
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.encode_call",
        new_callable=AsyncMock,
        return_value={"to": FAKE_ADDR},
    )
    async def test_repay_native_via_zero_address(
        self, _mock_encode, _mock_allow, _mock_send, adapter
    ):
        adapter._wrapped_native = AsyncMock(return_value=FAKE_ASSET)
        ok, result = await adapter.repay(
            chain_id=42161, underlying_token=ZERO_ADDRESS, qty=100
        )
        assert ok is True
        assert result["wrap_tx"] == "0xabc"
        assert result["repay_tx"] == "0xabc"

    @pytest.mark.asyncio
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.send_transaction",
        new_callable=AsyncMock,
        return_value="0xabc",
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.encode_call",
        new_callable=AsyncMock,
        return_value={"to": FAKE_ADDR},
    )
    async def test_set_emode_encodes_tx(self, mock_encode, _mock_send, adapter):
        ok, result = await adapter.set_emode(chain_id=42161, category_id=1)

        assert ok is True
        assert result == "0xabc"
        assert mock_encode.await_args.kwargs["fn_name"] == "setUserEMode"
        assert mock_encode.await_args.kwargs["args"] == [1]

    @pytest.mark.asyncio
    async def test_set_emode_validates_category(self, adapter):
        ok, result = await adapter.set_emode(chain_id=42161, category_id=256)

        assert ok is False
        assert "category_id must be between 0 and 255" in result

    @pytest.mark.asyncio
    async def test_get_earn_vault_state_reads_vault_and_user(self, adapter):
        mock_vault = MagicMock()
        mock_vault.functions.asset = MagicMock(return_value=_async_call(FAKE_ASSET))
        mock_vault.functions.name = MagicMock(
            return_value=_async_call("Aave USDC Vault")
        )
        mock_vault.functions.symbol = MagicMock(return_value=_async_call("avUSDC"))
        mock_vault.functions.decimals = MagicMock(return_value=_async_call(6))
        mock_vault.functions.totalSupply = MagicMock(
            return_value=_async_call(1_000_000)
        )
        mock_vault.functions.totalAssets = MagicMock(
            return_value=_async_call(2_000_000)
        )
        mock_vault.functions.convertToAssets = MagicMock(
            side_effect=[_async_call(2_000_000), _async_call(200_000)]
        )
        mock_vault.functions.convertToShares = MagicMock(
            return_value=_async_call(500_000)
        )
        mock_vault.functions.maxDeposit = MagicMock(return_value=_async_call(3_000_000))
        mock_vault.functions.maxMint = MagicMock(return_value=_async_call(1_500_000))
        mock_vault.functions.getFee = MagicMock(return_value=_async_call(1000))
        mock_vault.functions.getClaimableFees = MagicMock(return_value=_async_call(50))
        mock_vault.functions.getLastVaultBalance = MagicMock(
            return_value=_async_call(1_900_000)
        )
        mock_vault.functions.balanceOf = MagicMock(return_value=_async_call(100_000))
        mock_vault.functions.maxWithdraw = MagicMock(return_value=_async_call(200_000))
        mock_vault.functions.maxRedeem = MagicMock(return_value=_async_call(100_000))

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_vault)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.aave_v3_adapter.adapter.web3_utils.web3_from_chain_id",
            mock_web3_ctx,
        ):
            ok, state = await adapter.get_earn_vault_state(
                chain_id=42161, vault_address=FAKE_VAULT, account=FAKE_ADDR
            )

        assert ok is True
        assert isinstance(state, dict)
        assert state["type"] == "earn_vault"
        assert state["asset"] == "0x0000000000000000000000000000000000000001"
        assert state["symbol"] == "avUSDC"
        assert state["asset_decimals"] == 6
        assert state["total_assets_raw"] == 2_000_000
        assert state["assets_per_share_unit_raw"] == 2_000_000
        assert state["shares_per_asset_unit_raw"] == 500_000
        assert state["max_deposit_raw"] == 3_000_000
        assert state["max_mint_raw"] == 1_500_000
        assert state["fee_raw"] == 1000
        assert state["claimable_fees_raw"] == 50
        assert state["last_vault_balance_raw"] == 1_900_000
        assert state["user"]["shares_raw"] == 100_000
        assert state["user"]["assets_raw"] == 200_000

    @pytest.mark.asyncio
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.send_transaction",
        new_callable=AsyncMock,
        return_value="0xvault",
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.ensure_allowance",
        new_callable=AsyncMock,
        return_value=(True, "ok"),
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.encode_call",
        new_callable=AsyncMock,
        return_value={"to": FAKE_VAULT},
    )
    async def test_earn_vault_deposit_approves_asset_and_encodes(
        self, mock_encode, mock_allowance, _mock_send, adapter
    ):
        adapter._earn_vault_asset = AsyncMock(return_value=FAKE_ASSET)

        ok, tx = await adapter.earn_vault_deposit(
            chain_id=42161, vault_address=FAKE_VAULT, assets=123
        )

        assert ok is True
        assert tx == "0xvault"
        assert mock_allowance.await_args.kwargs["token_address"] == FAKE_ASSET
        assert mock_allowance.await_args.kwargs["spender"] == FAKE_VAULT
        assert mock_encode.await_args.kwargs["fn_name"] == "deposit"
        assert mock_encode.await_args.kwargs["args"] == [123, FAKE_ADDR]

    @pytest.mark.asyncio
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.send_transaction",
        new_callable=AsyncMock,
        return_value="0xvault",
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.ensure_allowance",
        new_callable=AsyncMock,
        return_value=(True, "ok"),
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.encode_call",
        new_callable=AsyncMock,
        return_value={"to": FAKE_VAULT},
    )
    async def test_earn_vault_deposit_atokens_approves_atoken_and_encodes(
        self, mock_encode, mock_allowance, _mock_send, adapter
    ):
        adapter._earn_vault_asset = AsyncMock(return_value=FAKE_ASSET)
        adapter._a_token_for_underlying = AsyncMock(return_value=FAKE_ATOKEN)

        ok, tx = await adapter.earn_vault_deposit_atokens(
            chain_id=42161, vault_address=FAKE_VAULT, assets=123
        )

        assert ok is True
        assert tx == "0xvault"
        assert mock_allowance.await_args.kwargs["token_address"] == FAKE_ATOKEN
        assert mock_allowance.await_args.kwargs["spender"] == FAKE_VAULT
        assert mock_encode.await_args.kwargs["fn_name"] == "depositATokens"
        assert mock_encode.await_args.kwargs["args"] == [123, FAKE_ADDR]

    @pytest.mark.asyncio
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.send_transaction",
        new_callable=AsyncMock,
        return_value="0xvault",
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.ensure_allowance",
        new_callable=AsyncMock,
        return_value=(True, "ok"),
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.encode_call",
        new_callable=AsyncMock,
        return_value={"to": FAKE_VAULT},
    )
    async def test_earn_vault_mint_with_atokens_approves_atoken_and_encodes(
        self, mock_encode, mock_allowance, _mock_send, adapter
    ):
        adapter._earn_vault_asset = AsyncMock(return_value=FAKE_ASSET)
        adapter._a_token_for_underlying = AsyncMock(return_value=FAKE_ATOKEN)

        ok, tx = await adapter.earn_vault_mint_with_atokens(
            chain_id=42161, vault_address=FAKE_VAULT, shares=456
        )

        assert ok is True
        assert tx == "0xvault"
        assert mock_allowance.await_args.kwargs["token_address"] == FAKE_ATOKEN
        assert mock_allowance.await_args.kwargs["spender"] == FAKE_VAULT
        assert mock_encode.await_args.kwargs["fn_name"] == "mintWithATokens"
        assert mock_encode.await_args.kwargs["args"] == [456, FAKE_ADDR]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method_name", "kwargs", "fn_name", "expected_args"),
        [
            (
                "earn_vault_mint",
                {"shares": 456},
                "mint",
                [456, FAKE_ADDR],
            ),
            (
                "earn_vault_withdraw",
                {"assets": 123},
                "withdraw",
                [123, FAKE_ADDR, FAKE_ADDR],
            ),
            (
                "earn_vault_withdraw_atokens",
                {"assets": 123},
                "withdrawATokens",
                [123, FAKE_ADDR, FAKE_ADDR],
            ),
            (
                "earn_vault_redeem",
                {"shares": 456},
                "redeem",
                [456, FAKE_ADDR, FAKE_ADDR],
            ),
            (
                "earn_vault_redeem_as_atokens",
                {"shares": 456},
                "redeemAsATokens",
                [456, FAKE_ADDR, FAKE_ADDR],
            ),
        ],
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.send_transaction",
        new_callable=AsyncMock,
        return_value="0xvault",
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.ensure_allowance",
        new_callable=AsyncMock,
        return_value=(True, "ok"),
    )
    @patch(
        "wayfinder_paths.adapters.aave_v3_adapter.adapter.encode_call",
        new_callable=AsyncMock,
        return_value={"to": FAKE_VAULT},
    )
    async def test_earn_vault_share_and_withdraw_ops_encode(
        self,
        mock_encode,
        mock_allowance,
        _mock_send,
        adapter,
        method_name,
        kwargs,
        fn_name,
        expected_args,
    ):
        adapter._earn_vault_asset = AsyncMock(return_value=FAKE_ASSET)

        ok, tx = await getattr(adapter, method_name)(
            chain_id=42161, vault_address=FAKE_VAULT, **kwargs
        )

        assert ok is True
        assert tx == "0xvault"
        assert mock_encode.await_args.kwargs["fn_name"] == fn_name
        assert mock_encode.await_args.kwargs["args"] == expected_args
        if method_name == "earn_vault_mint":
            assert mock_allowance.await_count == 1
        else:
            mock_allowance.assert_not_awaited()
