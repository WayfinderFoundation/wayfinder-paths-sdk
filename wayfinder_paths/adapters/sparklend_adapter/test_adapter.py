from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_utils import to_checksum_address

from wayfinder_paths.adapters.sparklend_adapter.adapter import (
    STABLE_RATE_MODE,
    VARIABLE_RATE_MODE,
    SparkLendAdapter,
)
from wayfinder_paths.core.constants.contracts import ZERO_ADDRESS
from wayfinder_paths.testing import fake_signing

FAKE_ADDR = "0x1234567890123456789012345678901234567890"
FAKE_ASSET = "0x0000000000000000000000000000000000000001"


class TestSparkLendAdapter:
    @pytest.fixture
    def adapter(self):
        return SparkLendAdapter(
            config={},
            signing=fake_signing(address=FAKE_ADDR),
            wallet_address=FAKE_ADDR,
        )

    @pytest.fixture
    def adapter_no_wallet(self):
        return SparkLendAdapter(config={})

    # ---- Construction ----

    def test_adapter_type(self, adapter):
        assert adapter.adapter_type == "SPARKLEND"

    def test_wallet_optional(self):
        a = SparkLendAdapter(config={})
        assert a.wallet_address is None

    def test_wallet_checksummed(self):
        raw = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
        a = SparkLendAdapter(config={}, wallet_address=raw)
        assert a.wallet_address == to_checksum_address(raw)

    # ---- _entry ----

    def test_entry_valid_chain(self, adapter):
        entry = adapter._entry(1)
        assert "pool" in entry

    def test_entry_unsupported_chain(self, adapter):
        with pytest.raises(ValueError, match="Unsupported"):
            adapter._entry(999999)

    # ---- require_wallet decorator ----

    @pytest.mark.asyncio
    async def test_require_wallet_blocks_lend(self, adapter_no_wallet):
        ok, msg = await adapter_no_wallet.lend(
            chain_id=1, underlying_token=FAKE_ASSET, qty=100
        )
        assert ok is False
        assert "wallet" in msg.lower()

    @pytest.mark.asyncio
    async def test_require_wallet_blocks_borrow(self, adapter_no_wallet):
        ok, msg = await adapter_no_wallet.borrow(
            chain_id=1, asset=FAKE_ASSET, amount=100
        )
        assert ok is False
        assert "wallet" in msg.lower()

    @pytest.mark.asyncio
    async def test_require_wallet_blocks_set_collateral(self, adapter_no_wallet):
        ok, msg = await adapter_no_wallet.set_collateral(
            chain_id=1, underlying_token=FAKE_ASSET, use_as_collateral=True
        )
        assert ok is False
        assert "wallet" in msg.lower()

    @pytest.mark.asyncio
    async def test_require_wallet_blocks_lend_native(self, adapter_no_wallet):
        ok, msg = await adapter_no_wallet.lend(
            chain_id=1, underlying_token=ZERO_ADDRESS, qty=100
        )
        assert ok is False
        assert "wallet" in msg.lower()

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
            chain_id=1, underlying_token=ZERO_ADDRESS, qty=100
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
            chain_id=1, underlying_token=ZERO_ADDRESS, qty=100
        )
        assert ok is True
        assert result["withdraw_tx"] == "0xabc"

    # ---- Amount validation ----

    @pytest.mark.asyncio
    async def test_lend_rejects_zero_amount(self, adapter):
        ok, msg = await adapter.lend(chain_id=1, underlying_token=FAKE_ASSET, qty=0)
        assert ok is False
        assert "positive" in msg

    @pytest.mark.asyncio
    async def test_lend_rejects_negative_amount(self, adapter):
        ok, msg = await adapter.lend(chain_id=1, underlying_token=FAKE_ASSET, qty=-1)
        assert ok is False
        assert "positive" in msg

    @pytest.mark.asyncio
    async def test_borrow_rejects_zero_amount(self, adapter):
        ok, msg = await adapter.borrow(chain_id=1, asset=FAKE_ASSET, amount=0)
        assert ok is False
        assert "positive" in msg

    @pytest.mark.asyncio
    async def test_lend_native_rejects_zero_amount(self, adapter):
        ok, msg = await adapter.lend(chain_id=1, underlying_token=ZERO_ADDRESS, qty=0)
        assert ok is False
        assert "positive" in msg

    @pytest.mark.asyncio
    async def test_borrow_native_rejects_zero_amount(self, adapter):
        ok, msg = await adapter.borrow_native(chain_id=1, amount=0)
        assert ok is False
        assert "positive" in msg

    # ---- unlend/withdraw_full bypass ----

    @pytest.mark.asyncio
    async def test_unlend_rejects_zero_without_withdraw_full(self, adapter):
        ok, msg = await adapter.unlend(
            chain_id=1, underlying_token=FAKE_ASSET, qty=0, withdraw_full=False
        )
        assert ok is False
        assert "positive" in msg

    @pytest.mark.asyncio
    async def test_repay_rejects_zero_without_repay_full(self, adapter):
        ok, msg = await adapter.repay(
            chain_id=1, asset=FAKE_ASSET, amount=0, repay_full=False
        )
        assert ok is False
        assert "positive" in msg

    @pytest.mark.asyncio
    async def test_unlend_native_rejects_zero_without_withdraw_full(self, adapter):
        ok, msg = await adapter.unlend(
            chain_id=1, underlying_token=ZERO_ADDRESS, qty=0, withdraw_full=False
        )
        assert ok is False
        assert "positive" in msg

    @pytest.mark.asyncio
    async def test_repay_native_rejects_zero_without_repay_full(self, adapter):
        ok, msg = await adapter.repay_native(chain_id=1, amount=0, repay_full=False)
        assert ok is False
        assert "positive" in msg

    # ---- Rate mode validation ----

    @pytest.mark.asyncio
    async def test_borrow_rate_mode_validation(self, adapter):
        ok, msg = await adapter.borrow(
            chain_id=1, asset=FAKE_ASSET, amount=1, rate_mode=3
        )
        assert ok is False
        assert "rate_mode" in msg.lower()

    @pytest.mark.asyncio
    async def test_repay_rate_mode_validation(self, adapter):
        ok, msg = await adapter.repay(
            chain_id=1, asset=FAKE_ASSET, amount=1, rate_mode=0
        )
        assert ok is False
        assert "rate_mode" in msg.lower()

    @pytest.mark.asyncio
    async def test_borrow_native_rate_mode_validation(self, adapter):
        ok, msg = await adapter.borrow_native(chain_id=1, amount=1, rate_mode=99)
        assert ok is False
        assert "rate_mode" in msg.lower()

    @pytest.mark.asyncio
    async def test_repay_native_rate_mode_validation(self, adapter):
        ok, msg = await adapter.repay_native(chain_id=1, amount=1, rate_mode=-1)
        assert ok is False
        assert "rate_mode" in msg.lower()

    @pytest.mark.asyncio
    async def test_borrow_accepts_stable_rate_past_validation(self, adapter):
        """rate_mode=1 should pass rate_mode validation (fail later on RPC)."""
        ok, msg = await adapter.borrow(
            chain_id=1, asset=FAKE_ASSET, amount=1, rate_mode=STABLE_RATE_MODE
        )
        # Fails on RPC/config, not on "rate_mode must be 1 or 2"
        assert ok is False
        assert "rate_mode must be" not in msg

    @pytest.mark.asyncio
    async def test_borrow_accepts_variable_rate_past_validation(self, adapter):
        ok, msg = await adapter.borrow(
            chain_id=1, asset=FAKE_ASSET, amount=1, rate_mode=VARIABLE_RATE_MODE
        )
        assert ok is False
        assert "rate_mode must be" not in msg

    @pytest.mark.asyncio
    @patch(
        "wayfinder_paths.adapters.sparklend_adapter.adapter.send_transaction",
        new_callable=AsyncMock,
        return_value="0xabc",
    )
    @patch(
        "wayfinder_paths.adapters.sparklend_adapter.adapter.encode_call",
        new_callable=AsyncMock,
        return_value={"to": FAKE_ADDR},
    )
    @patch(
        "wayfinder_paths.adapters.sparklend_adapter.adapter.read_only_calls_multicall_or_gather",
        new_callable=AsyncMock,
    )
    async def test_claim_rewards_encodes_asset_list(
        self, mock_multicall, mock_encode, mock_send, adapter
    ):
        mock_multicall.side_effect = [
            [
                (FAKE_ASSET, ZERO_ADDRESS, ZERO_ADDRESS),
                (FAKE_ADDR, ZERO_ADDRESS, ZERO_ADDRESS),
            ],
            [["0xreward"], []],
        ]

        mock_dp = MagicMock()
        mock_dp.functions.getAllReservesTokens = MagicMock(
            return_value=MagicMock(
                call=AsyncMock(
                    return_value=[("ASSET1", FAKE_ASSET), ("ASSET2", FAKE_ADDR)]
                )
            )
        )
        mock_rewards = MagicMock()
        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(side_effect=[mock_dp, mock_rewards])

        @asynccontextmanager
        async def fake_web3_from_chain_id(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.sparklend_adapter.adapter.web3_utils.web3_from_chain_id",
            fake_web3_from_chain_id,
        ):
            ok, tx = await adapter.claim_rewards(chain_id=1)

        assert ok is True
        assert tx == "0xabc"
        assert mock_encode.await_args.kwargs["args"] == [
            [to_checksum_address(FAKE_ASSET)]
        ]

    # ---- get_all_markets (mocked) ----

    @pytest.mark.asyncio
    async def test_get_all_markets_basic(self, adapter):
        mock_dp = MagicMock()

        # getAllReservesTokens returns [(symbol, address), ...]
        mock_dp.functions.getAllReservesTokens = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=[("USDC", FAKE_ASSET)]))
        )

        # getReserveConfigurationData returns 10-element tuple
        mock_dp.functions.getReserveConfigurationData = MagicMock(
            return_value=MagicMock(
                call=AsyncMock(
                    return_value=(
                        6,  # decimals
                        8000,  # ltv
                        8500,  # liq_threshold
                        10500,  # liq_bonus
                        1000,  # reserve_factor
                        True,  # usage_as_collateral_enabled
                        True,  # borrowing_enabled
                        False,  # stable_borrow_rate_enabled
                        True,  # is_active
                        False,  # is_frozen
                    )
                )
            )
        )

        # getReserveData returns 12-element tuple
        mock_dp.functions.getReserveData = MagicMock(
            return_value=MagicMock(
                call=AsyncMock(
                    return_value=(
                        0,  # unbacked
                        0,  # accruedToTreasuryScaled
                        100_000_000,  # totalAToken (100 USDC)
                        0,  # totalStableDebt
                        50_000_000,  # totalVariableDebt (50 USDC)
                        int(0.05 * 10**27),  # liquidityRate (5% APR in ray)
                        int(0.10 * 10**27),  # variableBorrowRate (10% APR in ray)
                        0,  # stableBorrowRate
                        0,  # averageStableBorrowRate
                        10**27,  # liquidityIndex
                        10**27,  # variableBorrowIndex
                        0,  # lastUpdateTimestamp
                    )
                )
            )
        )

        a_token = "0x00000000000000000000000000000000000000A1"
        stable_debt = "0x00000000000000000000000000000000000000B1"
        variable_debt = "0x00000000000000000000000000000000000000C1"
        mock_dp.functions.getReserveTokensAddresses = MagicMock(
            return_value=MagicMock(
                call=AsyncMock(return_value=(a_token, stable_debt, variable_debt))
            )
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_dp)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.sparklend_adapter.adapter.web3_utils.web3_from_chain_id",
            mock_web3_ctx,
        ):
            ok, markets = await adapter.get_all_markets(chain_id=1, include_caps=False)

        assert ok is True
        assert isinstance(markets, list)
        assert len(markets) == 1

        m = markets[0]
        assert m["symbol"] == "USDC"
        assert m["decimals"] == 6
        assert m["ltv_bps"] == 8000
        assert m["liquidation_threshold_bps"] == 8500
        assert m["usage_as_collateral_enabled"] is True
        assert m["borrowing_enabled"] is True
        assert m["is_active"] is True
        assert m["is_frozen"] is False
        assert m["total_supply_raw"] == 100_000_000
        assert m["total_variable_debt_raw"] == 50_000_000
        assert m["supply_apy"] > 0
        assert m["variable_borrow_apy"] > 0

    # ---- get_pos (mocked) ----

    @pytest.mark.asyncio
    async def test_get_pos_returns_position(self, adapter):
        mock_dp = MagicMock()

        # getReserveConfigurationData
        mock_dp.functions.getReserveConfigurationData = MagicMock(
            return_value=MagicMock(
                call=AsyncMock(
                    return_value=(
                        6,
                        8000,
                        8500,
                        10500,
                        1000,
                        True,
                        True,
                        False,
                        True,
                        False,
                    )
                )
            )
        )

        # getUserReserveData returns 9-element tuple
        mock_dp.functions.getUserReserveData = MagicMock(
            return_value=MagicMock(
                call=AsyncMock(
                    return_value=(
                        5_000_000,  # currentATokenBalance
                        0,  # currentStableDebt
                        1_000_000,  # currentVariableDebt
                        0,  # principalStableDebt
                        500_000,  # scaledVariableDebt
                        0,  # stableBorrowRate
                        int(0.05 * 10**27),  # liquidityRate
                        0,  # stableRateLastUpdated
                        True,  # usageAsCollateralEnabledOnUser
                    )
                )
            )
        )

        a_token = "0x00000000000000000000000000000000000000A1"
        stable_debt = "0x00000000000000000000000000000000000000B1"
        variable_debt = "0x00000000000000000000000000000000000000C1"
        mock_dp.functions.getReserveTokensAddresses = MagicMock(
            return_value=MagicMock(
                call=AsyncMock(return_value=(a_token, stable_debt, variable_debt))
            )
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_dp)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.sparklend_adapter.adapter.web3_utils.web3_from_chain_id",
            mock_web3_ctx,
        ):
            ok, pos = await adapter.get_pos(
                chain_id=1, asset=FAKE_ASSET, account=FAKE_ADDR
            )

        assert ok is True
        assert pos["protocol"] == "sparklend"
        assert pos["supply_raw"] == 5_000_000
        assert pos["variable_borrow_raw"] == 1_000_000
        assert pos["usage_as_collateral_enabled_on_user"] is True
        assert pos["decimals"] == 6

    # ---- get_full_user_state (mocked) ----

    @pytest.mark.asyncio
    async def test_get_full_user_state_basic(self, adapter):
        mock_dp = MagicMock()

        mock_dp.functions.getAllReservesTokens = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=[("USDC", FAKE_ASSET)]))
        )

        mock_dp.functions.getReserveConfigurationData = MagicMock(
            return_value=MagicMock(
                call=AsyncMock(
                    return_value=(
                        6,
                        8000,
                        8500,
                        10500,
                        1000,
                        True,
                        True,
                        False,
                        True,
                        False,
                    )
                )
            )
        )

        # getUserReserveData — has supply and borrow
        mock_dp.functions.getUserReserveData = MagicMock(
            return_value=MagicMock(
                call=AsyncMock(
                    return_value=(2_000_000, 0, 500_000, 0, 0, 0, 0, 0, True)
                )
            )
        )

        a_token = "0x00000000000000000000000000000000000000A1"
        stable_debt = "0x00000000000000000000000000000000000000B1"
        variable_debt = "0x00000000000000000000000000000000000000C1"
        mock_dp.functions.getReserveTokensAddresses = MagicMock(
            return_value=MagicMock(
                call=AsyncMock(return_value=(a_token, stable_debt, variable_debt))
            )
        )

        # getUserAccountData returns 6-element tuple
        mock_pool = MagicMock()
        mock_pool.functions.getUserAccountData = MagicMock(
            return_value=MagicMock(
                call=AsyncMock(
                    return_value=(
                        2_000_000_00,  # totalCollateralBase
                        500_000_00,  # totalDebtBase
                        1_500_000_00,  # availableBorrowsBase
                        8500,  # currentLiquidationThreshold
                        8000,  # ltv
                        2 * 10**18,  # healthFactor (2.0)
                    )
                )
            )
        )

        mock_web3 = MagicMock()

        def contract_side_effect(*, address, abi):
            if any(
                x.get("name") == "getUserAccountData"
                for x in abi
                if isinstance(x, dict)
            ):
                return mock_pool
            return mock_dp

        mock_web3.eth.contract = MagicMock(side_effect=contract_side_effect)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.sparklend_adapter.adapter.web3_utils.web3_from_chain_id",
            mock_web3_ctx,
        ):
            ok, state = await adapter.get_full_user_state(chain_id=1, account=FAKE_ADDR)

        assert ok is True
        assert state["protocol"] == "sparklend"
        assert state["chain_id"] == 1
        assert len(state["positions"]) == 1

        pos = state["positions"][0]
        assert pos["supply_raw"] == 2_000_000
        assert pos["variable_borrow_raw"] == 500_000
        assert pos["usage_as_collateral_enabled_on_user"] is True

        assert state["account_data"]["health_factor"] == 2 * 10**18
        assert state["account_data"]["ltv"] == 8000

    # ---- Caching ----

    def test_reserve_config_cache_populated(self, adapter):
        """Caches start empty."""
        assert adapter._wrapped_native_by_chain == {}
        assert adapter._reserve_config_by_chain_underlying == {}
