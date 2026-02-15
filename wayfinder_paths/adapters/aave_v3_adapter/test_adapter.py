from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.adapters.aave_v3_adapter.adapter import AaveV3Adapter
from wayfinder_paths.core.constants.aave_v3_abi import UI_POOL_RESERVE_KEYS


class TestAaveV3Adapter:
    @pytest.fixture
    def adapter(self):
        return AaveV3Adapter(
            config={
                "strategy_wallet": {
                    "address": "0x1234567890123456789012345678901234567890"
                }
            }
        )

    def test_adapter_type(self, adapter):
        assert adapter.adapter_type == "AAVE_V3"

    def test_strategy_address_optional(self):
        adapter = AaveV3Adapter(config={})
        assert adapter.strategy_wallet_address is None

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
            return_value=MagicMock(call=AsyncMock(return_value=(reserves, base_currency)))
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
            return_value=MagicMock(call=AsyncMock(return_value=(reserves, base_currency)))
        )

        mock_ui_incentives = MagicMock()
        mock_ui_incentives.functions.getReservesIncentivesData = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=incentives_rows))
        )

        mock_web3 = MagicMock()

        def contract_side_effect(*, address, abi):  # noqa: ARG001
            if abi and isinstance(abi, list) and any(
                x.get("name") == "getReservesIncentivesData" for x in abi if isinstance(x, dict)
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
            ok, markets = await adapter.get_all_markets(chain_id=42161, include_rewards=True)

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
            return_value=MagicMock(call=AsyncMock(return_value=(reserves, base_currency)))
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
            ok, state = await adapter.get_full_user_state(
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
