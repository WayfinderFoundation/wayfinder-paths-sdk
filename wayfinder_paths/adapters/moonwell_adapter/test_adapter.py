from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from web3 import Web3

from wayfinder_paths.adapters.moonwell_adapter.adapter import (
    CHAIN_NAME,
    MANTISSA,
    MoonwellAdapter,
)
from wayfinder_paths.core.constants.contracts import (
    BASE_USDC,
    BASE_WETH,
    MOONWELL_M_USDC,
    MOONWELL_M_WETH,
    MOONWELL_M_WSTETH,
    MOONWELL_REWARD_DISTRIBUTOR,
    MOONWELL_WELL_TOKEN,
)


class TestMoonwellAdapter:
    @pytest.fixture
    def adapter(self):
        config = {
            "strategy_wallet": {"address": "0x1234567890123456789012345678901234567890"}
        }
        return MoonwellAdapter(config=config)

    def test_adapter_type(self, adapter):
        assert adapter.adapter_type == "MOONWELL"

    def test_chain_name(self):
        assert CHAIN_NAME == "base"

    @pytest.mark.asyncio
    async def test_get_full_user_state_basic(self, adapter):
        w3 = Web3()

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield w3

        m1 = MOONWELL_M_USDC
        m2 = MOONWELL_M_WETH

        rewards = w3.codec.encode(
            ["(address,(address,uint256,uint256,uint256)[])[]"],
            [[(m1, [(MOONWELL_WELL_TOKEN, 123, 0, 0)])]],
        )

        stage1 = [
            w3.codec.encode(["address[]"], [[m1, m2]]),
            w3.codec.encode(["address[]"], [[m1]]),
            w3.codec.encode(["uint256", "uint256", "uint256"], [0, 123, 0]),
            rewards,
        ]

        stage2 = [
            # m1
            w3.codec.encode(["uint256"], [100]),
            w3.codec.encode(["uint256"], [2 * MANTISSA]),
            w3.codec.encode(["uint256"], [50]),
            w3.codec.encode(["address"], [BASE_USDC]),
            w3.codec.encode(["uint8"], [8]),
            w3.codec.encode(["bool", "uint256"], [True, int(0.5 * MANTISSA)]),
            # m2 (all zeros, should be filtered out)
            w3.codec.encode(["uint256"], [0]),
            w3.codec.encode(["uint256"], [0]),
            w3.codec.encode(["uint256"], [0]),
            w3.codec.encode(["address"], [BASE_WETH]),
            w3.codec.encode(["uint8"], [8]),
            w3.codec.encode(["bool", "uint256"], [True, int(0.5 * MANTISSA)]),
        ]

        with (
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.web3_from_chain_id",
                mock_web3_ctx,
            ),
            patch.object(
                adapter, "_multicall_chunked", new_callable=AsyncMock
            ) as mock_multicall,
        ):
            mock_multicall.side_effect = [stage1, stage2]
            ok, state = await adapter.get_full_user_state(
                include_rewards=True,
                include_usd=False,
                include_apy=False,
            )

        assert ok is True
        assert state["protocol"] == "moonwell"
        assert state["chainId"] == adapter.chain_id
        assert state["accountLiquidity"]["liquidity"] == 123
        assert len(state["positions"]) == 1
        assert state["positions"][0]["enteredAsCollateral"] is True
        assert state["positions"][0]["suppliedUnderlying"] == 200
        assert state["rewards"][f"base_{MOONWELL_WELL_TOKEN.lower()}"] == 123

    @pytest.mark.asyncio
    async def test_lend(self, adapter):
        mock_tx_hash = {"tx_hash": "0xabc123", "status": "success"}
        with (
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.ensure_allowance",
                new_callable=AsyncMock,
            ) as mock_allowance,
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_allowance.return_value = (True, {})
            mock_encode.return_value = {"data": "0x1234", "to": MOONWELL_M_USDC}
            mock_send.return_value = mock_tx_hash

            success, result = await adapter.lend(
                mtoken=MOONWELL_M_USDC,
                underlying_token=BASE_USDC,
                amount=10**6,
            )

            assert success
            assert result == mock_tx_hash

    @pytest.mark.asyncio
    async def test_lend_invalid_amount(self, adapter):
        success, result = await adapter.lend(
            mtoken=MOONWELL_M_USDC,
            underlying_token=BASE_USDC,
            amount=0,
        )

        assert success is False
        assert "positive" in result.lower()

    @pytest.mark.asyncio
    async def test_unlend(self, adapter):
        mock_tx_hash = {"tx_hash": "0xabc123", "status": "success"}

        with (
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_encode.return_value = {"data": "0x1234", "to": MOONWELL_M_USDC}
            mock_send.return_value = mock_tx_hash
            success, result = await adapter.unlend(
                mtoken=MOONWELL_M_USDC,
                amount=10**8,
            )

        assert success
        assert result == mock_tx_hash

    @pytest.mark.asyncio
    async def test_unlend_invalid_amount(self, adapter):
        success, result = await adapter.unlend(
            mtoken=MOONWELL_M_USDC,
            amount=-1,
        )

        assert success is False
        assert "positive" in result.lower()

    @pytest.mark.asyncio
    async def test_borrow(self, adapter):
        mock_tx_hash = {"tx_hash": "0xabc123", "status": "success"}

        with (
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_encode.return_value = {"data": "0x1234", "to": MOONWELL_M_USDC}
            mock_send.return_value = mock_tx_hash
            success, result = await adapter.borrow(
                mtoken=MOONWELL_M_USDC,
                amount=10**6,
            )

        assert success
        assert result == mock_tx_hash

    @pytest.mark.asyncio
    async def test_repay(self, adapter):
        mock_tx_hash = {"tx_hash": "0xabc123", "status": "success"}
        with (
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.ensure_allowance",
                new_callable=AsyncMock,
            ) as mock_allowance,
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_allowance.return_value = (True, {})
            mock_encode.return_value = {"data": "0x1234", "to": MOONWELL_M_USDC}
            mock_send.return_value = mock_tx_hash

            success, result = await adapter.repay(
                mtoken=MOONWELL_M_USDC,
                underlying_token=BASE_USDC,
                amount=10**6,
            )

            assert success
            assert result == mock_tx_hash

    @pytest.mark.asyncio
    async def test_set_collateral(self, adapter):
        mock_comptroller = MagicMock()
        mock_comptroller.functions.checkMembership = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=True))
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_comptroller)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        mock_tx_hash = {"tx_hash": "0xabc123", "status": "success"}

        with (
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.web3_from_chain_id",
                mock_web3_ctx,
            ),
        ):
            mock_encode.return_value = {"data": "0x1234", "to": MOONWELL_M_WSTETH}
            mock_send.return_value = mock_tx_hash
            success, result = await adapter.set_collateral(
                mtoken=MOONWELL_M_WSTETH,
            )

            assert success is True
            assert result == mock_tx_hash

    @pytest.mark.asyncio
    async def test_claim_rewards(self, adapter):
        # Mock contract for getting outstanding rewards
        mock_reward_contract = MagicMock()
        mock_reward_contract.functions.getOutstandingRewardsForUser = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=[]))
        )

        # Mock contract for claiming (on comptroller)
        mock_comptroller = MagicMock()
        mock_comptroller.functions.claimReward = MagicMock(
            return_value=MagicMock(
                build_transaction=AsyncMock(return_value={"data": "0x1234"})
            )
        )

        def mock_contract(address, abi):
            if address.lower() == MOONWELL_REWARD_DISTRIBUTOR.lower():
                return mock_reward_contract
            return mock_comptroller

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(side_effect=mock_contract)

        success, result = await adapter.claim_rewards()

        assert success
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_get_pos_success(self, adapter):
        underlying_addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

        # Mock mtoken contract calls
        mock_mtoken = MagicMock()
        mock_mtoken.functions.balanceOf = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=10**8))
        )
        mock_mtoken.functions.exchangeRateStored = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=2 * MANTISSA))
        )
        mock_mtoken.functions.borrowBalanceStored = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=10**6))
        )
        mock_mtoken.functions.underlying = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=underlying_addr))
        )

        # Mock reward distributor contract
        mock_reward = MagicMock()
        mock_reward.functions.getOutstandingRewardsForUser = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=[]))
        )

        def mock_contract(address, abi):
            if address.lower() == MOONWELL_REWARD_DISTRIBUTOR.lower():
                return mock_reward
            return mock_mtoken

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(side_effect=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.moonwell_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        ):
            success, result = await adapter.get_pos(mtoken=MOONWELL_M_USDC)

        assert success
        assert "mtoken_balance" in result
        assert "underlying_balance" in result
        assert "borrow_balance" in result
        assert "balances" in result
        assert result["mtoken_balance"] == 10**8
        assert result["borrow_balance"] == 10**6

    @pytest.mark.asyncio
    async def test_get_collateral_factor_success(self, adapter):
        # Clear cache to ensure fresh test
        await adapter._cache.clear()

        # Mock contract calls - returns (isListed, collateralFactorMantissa)
        mock_contract = MagicMock()
        mock_contract.functions.markets = MagicMock(
            return_value=MagicMock(
                call=AsyncMock(return_value=(True, int(0.75 * MANTISSA)))
            )
        )
        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.moonwell_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        ):
            success, result = await adapter.get_collateral_factor(
                mtoken=MOONWELL_M_WSTETH
            )

        assert success
        assert result == 0.75

    @pytest.mark.asyncio
    async def test_get_collateral_factor_not_listed(self, adapter):
        mock_contract = MagicMock()
        mock_contract.functions.markets = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=(False, 0)))
        )
        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.moonwell_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        ):
            success, result = await adapter.get_collateral_factor(
                mtoken="0x0000000000000000000000000000000000000001"
            )

        assert success is False
        assert "not listed" in result.lower()

    @pytest.mark.asyncio
    async def test_get_collateral_factor_caching(self, adapter):
        # Clear cache to ensure fresh test
        await adapter._cache.clear()

        call_count = 0

        async def mock_markets_call(**kwargs):
            nonlocal call_count
            call_count += 1
            return (True, int(0.80 * MANTISSA))

        mock_contract = MagicMock()
        mock_contract.functions.markets = MagicMock(
            return_value=MagicMock(call=mock_markets_call)
        )
        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        mtoken = MOONWELL_M_WSTETH

        with patch(
            "wayfinder_paths.adapters.moonwell_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        ):
            # First call should hit RPC
            success1, result1 = await adapter.get_collateral_factor(mtoken=mtoken)
            assert success1 is True
            assert result1 == 0.80
            assert call_count == 1

            # Second call should use cache (no additional RPC call)
            success2, result2 = await adapter.get_collateral_factor(mtoken=mtoken)
            assert success2 is True
            assert result2 == 0.80
            assert call_count == 1

            # Third call for same mtoken should still use cache
            success3, result3 = await adapter.get_collateral_factor(mtoken=mtoken)
            assert success3 is True
            assert result3 == 0.80
            assert call_count == 1

            success4, result4 = await adapter.get_collateral_factor(
                mtoken=MOONWELL_M_USDC
            )
            assert success4 is True
            assert call_count == 2

    @pytest.mark.asyncio
    async def test_get_apy_supply(self, adapter):
        rate_per_second = int(1.5e9)

        # Mock mtoken contract
        mock_mtoken = MagicMock()
        mock_mtoken.functions.supplyRatePerTimestamp = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=rate_per_second))
        )
        mock_mtoken.functions.totalSupply = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=10**18))
        )

        # Mock reward distributor
        mock_reward = MagicMock()
        mock_reward.functions.getAllMarketConfigs = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=[]))
        )

        def mock_contract(address, abi):
            if address.lower() == MOONWELL_REWARD_DISTRIBUTOR.lower():
                return mock_reward
            return mock_mtoken

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(side_effect=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.moonwell_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        ):
            success, result = await adapter.get_apy(
                mtoken=MOONWELL_M_USDC,
                apy_type="supply",
                include_rewards=False,
            )

        assert success
        assert isinstance(result, float)
        assert result >= 0

    @pytest.mark.asyncio
    async def test_get_apy_borrow(self, adapter):
        rate_per_second = int(2e9)

        # Mock mtoken contract
        mock_mtoken = MagicMock()
        mock_mtoken.functions.borrowRatePerTimestamp = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=rate_per_second))
        )
        mock_mtoken.functions.totalBorrows = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=10**18))
        )

        # Mock reward distributor
        mock_reward = MagicMock()
        mock_reward.functions.getAllMarketConfigs = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=[]))
        )

        def mock_contract(address, abi):
            if address.lower() == MOONWELL_REWARD_DISTRIBUTOR.lower():
                return mock_reward
            return mock_mtoken

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(side_effect=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.moonwell_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        ):
            success, result = await adapter.get_apy(
                mtoken=MOONWELL_M_USDC,
                apy_type="borrow",
                include_rewards=False,
            )

        assert success
        assert isinstance(result, float)
        assert result >= 0

    @pytest.mark.asyncio
    async def test_get_borrowable_amount_success(self, adapter):
        mock_contract = MagicMock()
        mock_contract.functions.getAccountLiquidity = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=(0, 10**18, 0)))
        )
        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.moonwell_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        ):
            success, result = await adapter.get_borrowable_amount()

        assert success
        assert result == 10**18

    @pytest.mark.asyncio
    async def test_get_borrowable_amount_shortfall(self, adapter):
        mock_contract = MagicMock()
        mock_contract.functions.getAccountLiquidity = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=(0, 0, 10**16)))
        )
        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.moonwell_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        ):
            success, result = await adapter.get_borrowable_amount()

        assert success is False
        assert "shortfall" in result.lower()

    @pytest.mark.asyncio
    async def test_wrap_eth(self, adapter):
        mock_tx_hash = {"tx_hash": "0xabc123", "status": "success"}

        with (
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.moonwell_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_encode.return_value = {"data": "0x1234", "to": BASE_WETH}
            mock_send.return_value = mock_tx_hash
            success, result = await adapter.wrap_eth(amount=10**18)

        assert success
        assert result == mock_tx_hash

    def test_strategy_address_missing(self):
        with pytest.raises(KeyError):
            MoonwellAdapter(config={})

    @pytest.mark.asyncio
    async def test_max_withdrawable_mtoken_zero_balance(self, adapter):
        # Mock contracts
        mock_mtoken = MagicMock()
        mock_mtoken.functions.balanceOf = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=0))
        )
        mock_mtoken.functions.exchangeRateStored = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=MANTISSA))
        )
        mock_mtoken.functions.getCash = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=10**18))
        )
        mock_mtoken.functions.decimals = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=8))
        )
        mock_mtoken.functions.underlying = MagicMock(
            return_value=MagicMock(call=AsyncMock(return_value=BASE_USDC))
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_mtoken)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.moonwell_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        ):
            success, result = await adapter.max_withdrawable_mtoken(
                mtoken=MOONWELL_M_USDC
            )

        assert success
        assert result["cTokens_raw"] == 0
        assert result["underlying_raw"] == 0
