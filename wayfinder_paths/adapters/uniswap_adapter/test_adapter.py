from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.adapters.uniswap_adapter.adapter import (
    MAX_UINT128,
    SUPPORTED_CHAIN_IDS,
    UniswapAdapter,
)
from wayfinder_paths.core.constants.contracts import (
    UNISWAP_V3_NPM,
    ZERO_ADDRESS,
)

MOCK_WALLET = "0x1234567890123456789012345678901234567890"
MOCK_TOKEN0 = "0x0000000000000000000000000000000000000AAA"
MOCK_TOKEN1 = "0x0000000000000000000000000000000000000BBB"
MOCK_TX_HASH = "0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
CHAIN_ID = 8453  # Base


class TestUniswapAdapter:
    @pytest.fixture
    def adapter(self):
        config = {"strategy_wallet": {"address": MOCK_WALLET}}
        return UniswapAdapter(
            config=config, strategy_wallet_signing_callback=AsyncMock()
        )


    def test_adapter_type(self, adapter):
        assert adapter.adapter_type == "UNISWAP"

    def test_strategy_address_missing(self):
        with pytest.raises(KeyError):
            UniswapAdapter(config={})

    def test_supported_chains(self):
        assert SUPPORTED_CHAIN_IDS == frozenset({1, 42161, 137, 8453, 56, 43114})


    @pytest.mark.asyncio
    async def test_add_liquidity_success(self, adapter):
        with (
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.ensure_allowance",
                new_callable=AsyncMock,
            ) as mock_allowance,
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_allowance.return_value = (True, {})
            mock_encode.return_value = {
                "data": "0x1234",
                "to": UNISWAP_V3_NPM[CHAIN_ID],
            }
            mock_send.return_value = MOCK_TX_HASH

            ok, result = await adapter.add_liquidity(
                token0=MOCK_TOKEN0,
                token1=MOCK_TOKEN1,
                fee=3000,
                tick_lower=-60,
                tick_upper=60,
                amount0_desired=10**18,
                amount1_desired=3000 * 10**6,
                chain_id=CHAIN_ID,
            )

            assert ok is True
            assert result == MOCK_TX_HASH
            assert mock_allowance.call_count == 2
            mock_encode.assert_called_once()
            mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_liquidity_approval_failure(self, adapter):
        with patch(
            "wayfinder_paths.adapters.uniswap_adapter.adapter.ensure_allowance",
            new_callable=AsyncMock,
        ) as mock_allowance:
            mock_allowance.return_value = (False, "approval denied")

            ok, result = await adapter.add_liquidity(
                token0=MOCK_TOKEN0,
                token1=MOCK_TOKEN1,
                fee=3000,
                tick_lower=-60,
                tick_upper=60,
                amount0_desired=10**18,
                amount1_desired=3000 * 10**6,
                chain_id=CHAIN_ID,
            )

            assert ok is False
            assert "approval denied" in result

    @pytest.mark.asyncio
    async def test_add_liquidity_auto_orders_tokens(self, adapter):
        higher = "0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
        lower = "0x0000000000000000000000000000000000000001"

        with (
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.ensure_allowance",
                new_callable=AsyncMock,
            ) as mock_allowance,
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_allowance.return_value = (True, {})
            mock_encode.return_value = {
                "data": "0x1234",
                "to": UNISWAP_V3_NPM[CHAIN_ID],
            }
            mock_send.return_value = MOCK_TX_HASH

            ok, _ = await adapter.add_liquidity(
                token0=higher,
                token1=lower,
                fee=500,
                tick_lower=-10,
                tick_upper=10,
                amount0_desired=100,
                amount1_desired=200,
                chain_id=CHAIN_ID,
            )

            assert ok is True
            call_args = mock_encode.call_args
            mint_params = call_args.kwargs["args"][0]
            addr0 = mint_params[0]
            addr1 = mint_params[1]
            assert int(addr0, 16) < int(addr1, 16)


    @pytest.mark.asyncio
    async def test_increase_liquidity_success(self, adapter):
        with (
            patch.object(
                adapter, "get_position", new_callable=AsyncMock
            ) as mock_get_pos,
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.ensure_allowance",
                new_callable=AsyncMock,
            ) as mock_allowance,
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_get_pos.return_value = (
                True,
                {"token0": MOCK_TOKEN0, "token1": MOCK_TOKEN1, "fee": 3000},
            )
            mock_allowance.return_value = (True, {})
            mock_encode.return_value = {
                "data": "0x1234",
                "to": UNISWAP_V3_NPM[CHAIN_ID],
            }
            mock_send.return_value = MOCK_TX_HASH

            ok, result = await adapter.increase_liquidity(
                token_id=123,
                amount0_desired=10**18,
                amount1_desired=10**6,
                chain_id=CHAIN_ID,
            )

            assert ok is True
            assert result == MOCK_TX_HASH


    @pytest.mark.asyncio
    async def test_remove_liquidity_with_collect(self, adapter):
        mock_contract = MagicMock()
        mock_contract.encode_abi = MagicMock(
            side_effect=[b"decrease_data", b"collect_data"]
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with (
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.web3_from_chain_id",
                mock_web3_ctx,
            ),
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_encode.return_value = {
                "data": "0x1234",
                "to": UNISWAP_V3_NPM[CHAIN_ID],
            }
            mock_send.return_value = MOCK_TX_HASH

            ok, result = await adapter.remove_liquidity(
                token_id=123,
                liquidity=10**12,
                chain_id=CHAIN_ID,
                collect=True,
                burn=False,
            )

            assert ok is True
            assert result == MOCK_TX_HASH
            assert mock_contract.encode_abi.call_count == 2

    @pytest.mark.asyncio
    async def test_remove_liquidity_with_burn(self, adapter):
        mock_contract = MagicMock()
        mock_contract.encode_abi = MagicMock(
            side_effect=[b"decrease_data", b"collect_data", b"burn_data"]
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with (
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.web3_from_chain_id",
                mock_web3_ctx,
            ),
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_encode.return_value = {
                "data": "0x1234",
                "to": UNISWAP_V3_NPM[CHAIN_ID],
            }
            mock_send.return_value = MOCK_TX_HASH

            ok, result = await adapter.remove_liquidity(
                token_id=123,
                liquidity=10**12,
                chain_id=CHAIN_ID,
                collect=True,
                burn=True,
            )

            assert ok is True
            assert mock_contract.encode_abi.call_count == 3

    @pytest.mark.asyncio
    async def test_remove_liquidity_multicall_encoding(self, adapter):
        mock_contract = MagicMock()
        mock_contract.encode_abi = MagicMock(return_value=b"encoded_data")

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with (
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.web3_from_chain_id",
                mock_web3_ctx,
            ),
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_encode.return_value = {
                "data": "0x1234",
                "to": UNISWAP_V3_NPM[CHAIN_ID],
            }
            mock_send.return_value = MOCK_TX_HASH

            ok, _ = await adapter.remove_liquidity(
                token_id=456,
                liquidity=5000,
                chain_id=CHAIN_ID,
            )

            assert ok is True
            encode_args = mock_encode.call_args
            assert encode_args.kwargs["fn_name"] == "multicall"


    @pytest.mark.asyncio
    async def test_collect_fees_success(self, adapter):
        with (
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_encode.return_value = {
                "data": "0x1234",
                "to": UNISWAP_V3_NPM[CHAIN_ID],
            }
            mock_send.return_value = MOCK_TX_HASH

            ok, result = await adapter.collect_fees(
                token_id=789,
                chain_id=CHAIN_ID,
            )

            assert ok is True
            assert result == MOCK_TX_HASH
            call_args = mock_encode.call_args
            collect_params = call_args.kwargs["args"][0]
            assert collect_params[2] == MAX_UINT128
            assert collect_params[3] == MAX_UINT128


    @pytest.mark.asyncio
    async def test_get_uncollected_fees(self, adapter):
        mock_collect_fn = MagicMock()
        mock_collect_fn.call = AsyncMock(return_value=(5000, 3000))

        mock_contract = MagicMock()
        mock_contract.functions.collect = MagicMock(return_value=mock_collect_fn)

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with (
            patch.object(
                adapter, "get_position", new_callable=AsyncMock
            ) as mock_get_pos,
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.web3_from_chain_id",
                mock_web3_ctx,
            ),
        ):
            mock_get_pos.return_value = (
                True,
                {"token0": MOCK_TOKEN0, "token1": MOCK_TOKEN1},
            )

            ok, result = await adapter.get_uncollected_fees(
                token_id=123,
                chain_id=CHAIN_ID,
            )

            assert ok is True
            assert result["fees0"] == 5000
            assert result["fees1"] == 3000
            assert result["token0"] == MOCK_TOKEN0
            assert result["token1"] == MOCK_TOKEN1


    @pytest.mark.asyncio
    async def test_get_position(self, adapter):
        position_data = (
            0,  # nonce
            ZERO_ADDRESS,  # operator
            MOCK_TOKEN0,  # token0
            MOCK_TOKEN1,  # token1
            3000,  # fee
            -887220,  # tickLower
            887220,  # tickUpper
            10**15,  # liquidity
            0,  # feeGrowthInside0LastX128
            0,  # feeGrowthInside1LastX128
            100,  # tokensOwed0
            200,  # tokensOwed1
        )

        mock_positions_fn = MagicMock()
        mock_positions_fn.call = AsyncMock(return_value=position_data)

        mock_contract = MagicMock()
        mock_contract.functions.positions = MagicMock(return_value=mock_positions_fn)

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.uniswap_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        ):
            ok, result = await adapter.get_position(token_id=42, chain_id=CHAIN_ID)

        assert ok is True
        assert result["token_id"] == 42
        assert result["token0"] == MOCK_TOKEN0
        assert result["token1"] == MOCK_TOKEN1
        assert result["fee"] == 3000
        assert result["liquidity"] == 10**15
        assert result["tokens_owed0"] == 100
        assert result["tokens_owed1"] == 200

    @pytest.mark.asyncio
    async def test_get_positions(self, adapter):
        mock_balance_fn = MagicMock()
        mock_balance_fn.call = AsyncMock(return_value=2)

        mock_token_of_fn = MagicMock()
        mock_token_of_fn.call = AsyncMock(side_effect=[10, 20])

        mock_contract = MagicMock()
        mock_contract.functions.balanceOf = MagicMock(return_value=mock_balance_fn)
        mock_contract.functions.tokenOfOwnerByIndex = MagicMock(
            return_value=mock_token_of_fn
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with (
            patch(
                "wayfinder_paths.adapters.uniswap_adapter.adapter.web3_from_chain_id",
                mock_web3_ctx,
            ),
            patch.object(
                adapter, "get_position", new_callable=AsyncMock
            ) as mock_get_pos,
        ):
            mock_get_pos.side_effect = [
                (True, {"token_id": 10, "liquidity": 100}),
                (True, {"token_id": 20, "liquidity": 200}),
            ]

            ok, result = await adapter.get_positions(chain_id=CHAIN_ID)

        assert ok is True
        assert len(result) == 2
        assert result[0]["token_id"] == 10
        assert result[1]["token_id"] == 20


    @pytest.mark.asyncio
    async def test_get_pool_exists(self, adapter):
        pool_addr = "0x8888888888888888888888888888888888888888"

        mock_get_pool_fn = MagicMock()
        mock_get_pool_fn.call = AsyncMock(return_value=pool_addr)

        mock_contract = MagicMock()
        mock_contract.functions.getPool = MagicMock(return_value=mock_get_pool_fn)

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.uniswap_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        ):
            ok, result = await adapter.get_pool(
                token0=MOCK_TOKEN0, token1=MOCK_TOKEN1, fee=500, chain_id=CHAIN_ID
            )

        assert ok is True
        assert result == pool_addr

    @pytest.mark.asyncio
    async def test_get_pool_not_found(self, adapter):
        mock_get_pool_fn = MagicMock()
        mock_get_pool_fn.call = AsyncMock(return_value=ZERO_ADDRESS)

        mock_contract = MagicMock()
        mock_contract.functions.getPool = MagicMock(return_value=mock_get_pool_fn)

        mock_web3 = MagicMock()
        mock_web3.eth.contract = MagicMock(return_value=mock_contract)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(
            "wayfinder_paths.adapters.uniswap_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        ):
            ok, result = await adapter.get_pool(
                token0=MOCK_TOKEN0, token1=MOCK_TOKEN1, fee=500, chain_id=CHAIN_ID
            )

        assert ok is False
        assert "No pool found" in result


    def test_price_to_tick_eth_usdc(self):
        tick = UniswapAdapter.price_to_tick(3000.0, 18, 6)
        price = UniswapAdapter.tick_to_price(tick, 18, 6)
        assert abs(price - 3000.0) / 3000.0 < 0.001

    def test_tick_to_price_and_back(self):
        original_tick = 100000
        price = UniswapAdapter.tick_to_price(original_tick, 18, 6)
        tick_back = UniswapAdapter.price_to_tick(price, 18, 6)
        assert abs(tick_back - original_tick) <= 1

    def test_price_to_tick_stablecoin(self):
        tick = UniswapAdapter.price_to_tick(1.0, 6, 6)
        assert tick == 0

    def test_nearest_usable_tick(self):
        assert UniswapAdapter.nearest_usable_tick(15, 10) == 20
        assert UniswapAdapter.nearest_usable_tick(14, 10) == 10
        assert UniswapAdapter.nearest_usable_tick(-33, 60) == -60
        assert UniswapAdapter.nearest_usable_tick(0, 200) == 0

    def test_nearest_usable_tick_invalid_spacing(self):
        with pytest.raises(ValueError):
            UniswapAdapter.nearest_usable_tick(100, 0)


    def test_calculate_il_v3_no_change(self):
        result = UniswapAdapter.calculate_il(
            price_initial=3000.0,
            price_current=3000.0,
            tick_lower=-887220,
            tick_upper=887220,
            token0_decimals=18,
            token1_decimals=6,
        )
        assert abs(result["il_percent"]) < 0.01
        assert abs(result["value_lp"] - result["value_hold"]) < 0.01

    def test_calculate_il_v3_price_increase(self):
        result = UniswapAdapter.calculate_il(
            price_initial=3000.0,
            price_current=6000.0,
            tick_lower=-887220,
            tick_upper=887220,
            token0_decimals=18,
            token1_decimals=6,
        )
        assert result["il_percent"] < 0
        assert result["value_lp"] < result["value_hold"]


    def test_unsupported_chain_npm(self):
        with pytest.raises(ValueError, match="not deployed"):
            UniswapAdapter._get_npm_address(999999)

    def test_unsupported_chain_factory(self):
        with pytest.raises(ValueError, match="not deployed"):
            UniswapAdapter._get_factory_address(999999)


    def test_order_tokens_already_sorted(self):
        lower = "0x0000000000000000000000000000000000000001"
        higher = "0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
        t0, t1, a0, a1 = UniswapAdapter._order_tokens(lower, higher, 100, 200)
        assert int(t0, 16) < int(t1, 16)
        assert a0 == 100
        assert a1 == 200

    def test_order_tokens_needs_swap(self):
        lower = "0x0000000000000000000000000000000000000001"
        higher = "0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
        t0, t1, a0, a1 = UniswapAdapter._order_tokens(higher, lower, 100, 200)
        assert int(t0, 16) < int(t1, 16)
        assert a0 == 200
        assert a1 == 100
