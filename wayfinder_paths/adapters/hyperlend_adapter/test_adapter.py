from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.adapters.hyperlend_adapter.adapter import HyperlendAdapter
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.contracts import (
    HYPEREVM_WHYPE,
    HYPERLEND_POOL,
    HYPERLEND_WRAPPED_TOKEN_GATEWAY,
)
from wayfinder_paths.core.constants.hyperlend_abi import UI_POOL_RESERVE_KEYS


class TestHyperlendAdapter:
    @pytest.fixture
    def mock_hyperlend_client(self):
        return AsyncMock()

    @pytest.fixture
    def adapter(self):
        return HyperlendAdapter(
            config={
                "strategy_wallet": {
                    "address": "0x1234567890123456789012345678901234567890"
                }
            },
        )

    @pytest.mark.asyncio
    async def test_get_stable_markets_success(self, adapter, mock_hyperlend_client):
        mock_response = {
            "markets": {
                "0x1234...": {
                    "symbol": "USDT",
                    "symbol_canonical": "usdt",
                    "display_symbol": "USDT",
                    "reserve": {},
                    "decimals": 6,
                    "headroom": 1000000000000,
                    "supply_cap": 5000000000000,
                },
                "0x5678...": {
                    "symbol": "USDC",
                    "symbol_canonical": "usdc",
                    "display_symbol": "USDC",
                    "reserve": {},
                    "decimals": 6,
                    "headroom": 2000000000000,
                    "supply_cap": 10000000000000,
                },
            },
            "notes": [],
        }
        mock_hyperlend_client.get_stable_markets = AsyncMock(return_value=mock_response)

        with patch(
            "wayfinder_paths.adapters.hyperlend_adapter.adapter.HYPERLEND_CLIENT",
            mock_hyperlend_client,
        ):
            success, data = await adapter.get_stable_markets(
                required_underlying_tokens=1000.0,
                buffer_bps=100,
                min_buffer_tokens=100.0,
            )

            assert success
            assert data == mock_response
            mock_hyperlend_client.get_stable_markets.assert_called_once_with(
                required_underlying_tokens=1000.0,
                buffer_bps=100,
                min_buffer_tokens=100.0,
            )

    @pytest.mark.asyncio
    async def test_get_stable_markets_minimal_params(
        self, adapter, mock_hyperlend_client
    ):
        mock_response = {
            "markets": {
                "0x1234...": {
                    "symbol": "USDT",
                    "symbol_canonical": "usdt",
                    "display_symbol": "USDT",
                    "reserve": {},
                    "decimals": 6,
                    "headroom": 1000000000000,
                    "supply_cap": 5000000000000,
                }
            },
            "notes": [],
        }
        mock_hyperlend_client.get_stable_markets = AsyncMock(return_value=mock_response)

        with patch(
            "wayfinder_paths.adapters.hyperlend_adapter.adapter.HYPERLEND_CLIENT",
            mock_hyperlend_client,
        ):
            success, data = await adapter.get_stable_markets()

            assert success
            assert data == mock_response
            mock_hyperlend_client.get_stable_markets.assert_called_once_with(
                required_underlying_tokens=None,
                buffer_bps=None,
                min_buffer_tokens=None,
            )

    @pytest.mark.asyncio
    async def test_get_stable_markets_partial_params(
        self, adapter, mock_hyperlend_client
    ):
        mock_response = {"markets": {}, "notes": []}
        mock_hyperlend_client.get_stable_markets = AsyncMock(return_value=mock_response)

        with patch(
            "wayfinder_paths.adapters.hyperlend_adapter.adapter.HYPERLEND_CLIENT",
            mock_hyperlend_client,
        ):
            success, data = await adapter.get_stable_markets(
                required_underlying_tokens=500.0
            )

            assert success
            assert data == mock_response
            mock_hyperlend_client.get_stable_markets.assert_called_once_with(
                required_underlying_tokens=500.0,
                buffer_bps=None,
                min_buffer_tokens=None,
            )

    @pytest.mark.asyncio
    async def test_get_stable_markets_failure(self, adapter, mock_hyperlend_client):
        mock_hyperlend_client.get_stable_markets = AsyncMock(
            side_effect=Exception("API Error: Connection timeout")
        )

        with patch(
            "wayfinder_paths.adapters.hyperlend_adapter.adapter.HYPERLEND_CLIENT",
            mock_hyperlend_client,
        ):
            success, data = await adapter.get_stable_markets()

            assert success is False
            assert "API Error: Connection timeout" in data

    @pytest.mark.asyncio
    async def test_get_stable_markets_http_error(self, adapter, mock_hyperlend_client):
        mock_hyperlend_client.get_stable_markets = AsyncMock(
            side_effect=Exception("HTTP 404 Not Found")
        )

        with patch(
            "wayfinder_paths.adapters.hyperlend_adapter.adapter.HYPERLEND_CLIENT",
            mock_hyperlend_client,
        ):
            success, data = await adapter.get_stable_markets()

            assert success is False
            assert "404" in data or "Not Found" in data

    @pytest.mark.asyncio
    async def test_get_stable_markets_empty_response(
        self, adapter, mock_hyperlend_client
    ):
        mock_response = {"markets": {}, "notes": []}
        mock_hyperlend_client.get_stable_markets = AsyncMock(return_value=mock_response)

        with patch(
            "wayfinder_paths.adapters.hyperlend_adapter.adapter.HYPERLEND_CLIENT",
            mock_hyperlend_client,
        ):
            success, data = await adapter.get_stable_markets()

            assert success
            assert data == mock_response
            assert len(data.get("markets", {})) == 0

    def test_adapter_type(self, adapter):
        assert adapter.adapter_type == "HYPERLEND"

    def test_strategy_address_optional(self):
        adapter = HyperlendAdapter(config={})
        assert adapter.strategy_wallet_address is None

    @pytest.mark.asyncio
    async def test_get_all_markets_success(self, adapter):
        reserve_keys = UI_POOL_RESERVE_KEYS

        def build_reserve(**overrides):
            base = dict.fromkeys(reserve_keys, 0)
            base.update(
                {
                    "underlyingAsset": "0x0000000000000000000000000000000000000001",
                    "name": "",
                    "symbol": "",
                    "decimals": 18,
                    "usageAsCollateralEnabled": True,
                    "borrowingEnabled": True,
                    "isActive": True,
                    "isFrozen": False,
                    "isPaused": False,
                    "isSiloedBorrowing": False,
                    "aTokenAddress": "0x0000000000000000000000000000000000000002",
                    "variableDebtTokenAddress": "0x0000000000000000000000000000000000000003",
                    "interestRateStrategyAddress": "0x0000000000000000000000000000000000000004",
                    "priceOracle": "0x0000000000000000000000000000000000000005",
                    "flashLoanEnabled": True,
                    "borrowableInIsolation": False,
                    "virtualAccActive": False,
                }
            )
            base.update(overrides)
            return tuple(base[k] for k in reserve_keys)

        reserves = [
            build_reserve(
                underlyingAsset="0x0000000000000000000000000000000000000011",
                symbol="USDC",
                decimals=6,
                liquidityRate=int(0.05 * 10**27),
                variableBorrowRate=int(0.10 * 10**27),
                priceInMarketReferenceCurrency=100000000,
                availableLiquidity=5000000,
                totalScaledVariableDebt=0,
                variableBorrowIndex=10**27,
                supplyCap=0,
            ),
            build_reserve(
                underlyingAsset="0x0000000000000000000000000000000000000022",
                symbol="uSOL",
                decimals=6,
                liquidityRate=int(0.02 * 10**27),
                variableBorrowRate=int(0.08 * 10**27),
                priceInMarketReferenceCurrency=2000000000,
                availableLiquidity=10000000,
                totalScaledVariableDebt=5000000,
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
            "wayfinder_paths.adapters.hyperlend_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        ):
            ok, markets = await adapter.get_all_markets()

        assert ok is True
        assert isinstance(markets, list)
        assert len(markets) == 2

        usol = next(m for m in markets if m["symbol"].lower() == "usol")
        assert usol["is_stablecoin"] is False
        assert usol["price_usd"] == 20.0
        assert usol["supply_cap"] == 100
        assert usol["supply_cap_headroom"] == 85000000

    @pytest.mark.asyncio
    async def test_get_stable_markets_with_is_stable_symbol(
        self, adapter, mock_hyperlend_client
    ):
        mock_response = {
            "markets": {
                "0x1234...": {
                    "symbol": "USDT",
                    "symbol_canonical": "usdt",
                    "display_symbol": "USDT",
                    "reserve": {},
                    "decimals": 6,
                    "headroom": 1000000000000,
                    "supply_cap": 5000000000000,
                }
            },
            "notes": [],
        }
        mock_hyperlend_client.get_stable_markets = AsyncMock(return_value=mock_response)

        with patch(
            "wayfinder_paths.adapters.hyperlend_adapter.adapter.HYPERLEND_CLIENT",
            mock_hyperlend_client,
        ):
            success, data = await adapter.get_stable_markets()

            assert success
            assert data == mock_response
            mock_hyperlend_client.get_stable_markets.assert_called_once_with(
                required_underlying_tokens=None,
                buffer_bps=None,
                min_buffer_tokens=None,
            )

    @pytest.mark.asyncio
    async def test_get_assets_view_success(self, adapter, mock_hyperlend_client):
        mock_response = {
            "block_number": 12345,
            "user": "0x0c737cB5934afCb5B01965141F865F795B324080",
            "native_balance_wei": 0,
            "native_balance": 0.0,
            "assets": [
                {
                    "underlying": "0x1234...",
                    "symbol": "USDT",
                    "symbol_canonical": "usdt",
                    "symbol_display": "USDT",
                    "decimals": 6,
                    "a_token": "0x...",
                    "variable_debt_token": "0x...",
                    "usage_as_collateral_enabled": True,
                    "borrowing_enabled": True,
                    "is_active": True,
                    "is_frozen": False,
                    "is_paused": False,
                    "is_siloed_borrowing": False,
                    "is_stablecoin": True,
                    "underlying_wallet_balance": 1000.0,
                    "underlying_wallet_balance_wei": 1000000000,
                    "price_usd": 1.0,
                    "supply": 500.0,
                    "variable_borrow": 0.0,
                    "supply_usd": 500.0,
                    "variable_borrow_usd": 0.0,
                    "supply_apr": 0.05,
                    "supply_apy": 0.05,
                    "variable_borrow_apr": 0.07,
                    "variable_borrow_apy": 0.07,
                }
            ],
            "account_data": {
                "total_collateral_base": 500,
                "total_debt_base": 0,
                "available_borrows_base": 400,
                "current_liquidation_threshold": 8000,
                "ltv": 7500,
                "health_factor_wad": 115792089237316195423570985008687907853269984665640564039457584007913129639935,
                "health_factor": 1.157920892373162e59,
            },
            "base_currency_info": {
                "marketReferenceCurrencyUnit": 100000000,
                "marketReferenceCurrencyPriceInUsd": 100000000,
                "networkBaseTokenPriceInUsd": 0,
                "networkBaseTokenPriceDecimals": 8,
            },
        }
        mock_hyperlend_client.get_assets_view = AsyncMock(return_value=mock_response)

        with patch(
            "wayfinder_paths.adapters.hyperlend_adapter.adapter.HYPERLEND_CLIENT",
            mock_hyperlend_client,
        ):
            success, data = await adapter.get_assets_view(
                user_address="0x0c737cB5934afCb5B01965141F865F795B324080",
            )

            assert success
            assert data == mock_response
            mock_hyperlend_client.get_assets_view.assert_called_once_with(
                user_address="0x0c737cB5934afCb5B01965141F865F795B324080",
            )

    @pytest.mark.asyncio
    async def test_get_assets_view_failure(self, adapter, mock_hyperlend_client):
        mock_hyperlend_client.get_assets_view = AsyncMock(
            side_effect=Exception("API Error: Invalid address")
        )

        with patch(
            "wayfinder_paths.adapters.hyperlend_adapter.adapter.HYPERLEND_CLIENT",
            mock_hyperlend_client,
        ):
            success, data = await adapter.get_assets_view(
                user_address="0x0c737cB5934afCb5B01965141F865F795B324080",
            )

            assert success is False
            assert "API Error: Invalid address" in data

    @pytest.mark.asyncio
    async def test_get_full_user_state_filters_zero_positions(
        self, adapter, mock_hyperlend_client
    ):
        mock_response = {
            "block_number": 12345,
            "user": "0xabc",
            "native_balance_wei": 0,
            "native_balance": 0.0,
            "assets": [
                {
                    "underlying": "0x1",
                    "symbol": "USDC",
                    "symbol_canonical": "usdc",
                    "symbol_display": "USDC",
                    "decimals": 6,
                    "a_token": "0xa",
                    "variable_debt_token": "0xd",
                    "usage_as_collateral_enabled": True,
                    "borrowing_enabled": True,
                    "is_active": True,
                    "is_frozen": False,
                    "is_paused": False,
                    "is_siloed_borrowing": False,
                    "is_stablecoin": True,
                    "underlying_wallet_balance": 0.0,
                    "underlying_wallet_balance_wei": 0,
                    "price_usd": 1.0,
                    "supply": 0.0,
                    "variable_borrow": 0.0,
                    "supply_usd": 0.0,
                    "variable_borrow_usd": 0.0,
                    "supply_apr": 0.0,
                    "supply_apy": 0.0,
                    "variable_borrow_apr": 0.0,
                    "variable_borrow_apy": 0.0,
                },
                {
                    "underlying": "0x2",
                    "symbol": "USDT",
                    "symbol_canonical": "usdt",
                    "symbol_display": "USDT",
                    "decimals": 6,
                    "a_token": "0xa2",
                    "variable_debt_token": "0xd2",
                    "usage_as_collateral_enabled": True,
                    "borrowing_enabled": True,
                    "is_active": True,
                    "is_frozen": False,
                    "is_paused": False,
                    "is_siloed_borrowing": False,
                    "is_stablecoin": True,
                    "underlying_wallet_balance": 0.0,
                    "underlying_wallet_balance_wei": 0,
                    "price_usd": 1.0,
                    "supply": 123.0,
                    "variable_borrow": 0.0,
                    "supply_usd": 123.0,
                    "variable_borrow_usd": 0.0,
                    "supply_apr": 0.05,
                    "supply_apy": 0.05,
                    "variable_borrow_apr": 0.07,
                    "variable_borrow_apy": 0.07,
                },
            ],
            "account_data": {"health_factor": 2.0},
            "base_currency_info": {},
        }
        mock_hyperlend_client.get_assets_view = AsyncMock(return_value=mock_response)

        with patch(
            "wayfinder_paths.adapters.hyperlend_adapter.adapter.HYPERLEND_CLIENT",
            mock_hyperlend_client,
        ):
            ok, state = await adapter.get_full_user_state(account="0xabc")
            assert ok is True
            assert state["protocol"] == "hyperlend"
            assert state["account"] == "0xabc"
            assert len(state["positions"]) == 1
            assert state["positions"][0]["symbol"] == "USDT"

    @pytest.mark.asyncio
    async def test_get_assets_view_empty_response(self, adapter, mock_hyperlend_client):
        mock_response = {
            "block_number": 12345,
            "user": "0x0c737cB5934afCb5B01965141F865F795B324080",
            "native_balance_wei": 0,
            "native_balance": 0.0,
            "assets": [],
            "account_data": {
                "total_collateral_base": 0,
                "total_debt_base": 0,
                "available_borrows_base": 0,
                "current_liquidation_threshold": 0,
                "ltv": 0,
                "health_factor_wad": 0,
                "health_factor": 0.0,
            },
            "base_currency_info": {
                "marketReferenceCurrencyUnit": 100000000,
                "marketReferenceCurrencyPriceInUsd": 100000000,
                "networkBaseTokenPriceInUsd": 0,
                "networkBaseTokenPriceDecimals": 8,
            },
        }
        mock_hyperlend_client.get_assets_view = AsyncMock(return_value=mock_response)

        with patch(
            "wayfinder_paths.adapters.hyperlend_adapter.adapter.HYPERLEND_CLIENT",
            mock_hyperlend_client,
        ):
            success, data = await adapter.get_assets_view(
                user_address="0x0c737cB5934afCb5B01965141F865F795B324080",
            )

            assert success
            assert data == mock_response
            assert len(data.get("assets", [])) == 0
            # New API uses account_data; total_value may not be present
            assert data.get("account_data", {}).get("total_collateral_base") == 0

    @pytest.mark.asyncio
    async def test_lend_native_uses_whype(self, adapter):
        with (
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
            patch.object(
                adapter, "_record_pool_op", new_callable=AsyncMock
            ) as mock_record,
        ):
            mock_encode.return_value = {"tx": "data"}
            mock_send.return_value = "0xabc"
            mock_record.return_value = None
            adapter.strategy_wallet_signing_callback = AsyncMock()

            ok, txn = await adapter.lend(
                underlying_token="0x0000000000000000000000000000000000000000",
                qty=123,
                chain_id=999,
                native=True,
            )

        assert ok is True
        assert txn == "0xabc"
        mock_encode.assert_awaited_once()
        _, kwargs = mock_encode.await_args
        assert kwargs["target"] == HYPERLEND_WRAPPED_TOKEN_GATEWAY
        assert kwargs["fn_name"] == "depositETH"
        assert kwargs["args"][0] == HYPEREVM_WHYPE
        assert kwargs["value"] == 123

    @pytest.mark.asyncio
    async def test_unlend_native_uses_whype(self, adapter):
        with (
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
            patch.object(
                adapter, "_record_pool_op", new_callable=AsyncMock
            ) as mock_record,
        ):
            mock_encode.return_value = {"tx": "data"}
            mock_send.return_value = "0xabc"
            mock_record.return_value = None
            adapter.strategy_wallet_signing_callback = AsyncMock()

            ok, txn = await adapter.unlend(
                underlying_token="0x0000000000000000000000000000000000000000",
                qty=123,
                chain_id=999,
                native=True,
            )

        assert ok is True
        assert txn == "0xabc"
        mock_encode.assert_awaited_once()
        _, kwargs = mock_encode.await_args
        assert kwargs["target"] == HYPERLEND_WRAPPED_TOKEN_GATEWAY
        assert kwargs["fn_name"] == "withdrawETH"
        assert kwargs["args"][0] == HYPEREVM_WHYPE

    @pytest.mark.asyncio
    async def test_borrow_erc20_calls_pool_borrow(self, adapter):
        token = "0x0000000000000000000000000000000000000011"
        with (
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_encode.return_value = {"tx": "data"}
            mock_send.return_value = "0xabc"
            adapter.strategy_wallet_signing_callback = AsyncMock()

            ok, txn = await adapter.borrow(
                underlying_token=token,
                qty=123,
                chain_id=999,
                native=False,
            )

        assert ok is True
        assert txn == "0xabc"
        _, kwargs = mock_encode.await_args
        assert kwargs["target"] == HYPERLEND_POOL
        assert kwargs["fn_name"] == "borrow"
        assert kwargs["args"][0].lower() == token.lower()
        assert kwargs["args"][1] == 123
        assert kwargs["args"][2] == 2  # variable rate mode
        assert kwargs["args"][3] == 0  # referral code

    @pytest.mark.asyncio
    async def test_borrow_native_borrows_and_unwraps(self, adapter):
        with (
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_encode.side_effect = [{"tx": "borrow"}, {"tx": "unwrap"}]
            mock_send.side_effect = ["0xborrow", "0xunwrap"]
            adapter.strategy_wallet_signing_callback = AsyncMock()

            ok, res = await adapter.borrow(
                underlying_token="0x0000000000000000000000000000000000000000",
                qty=123,
                chain_id=999,
                native=True,
            )

        assert ok is True
        assert res == {"borrow_tx": "0xborrow", "unwrap_tx": "0xunwrap"}

        assert mock_encode.await_count == 2
        first_kwargs = mock_encode.await_args_list[0].kwargs
        assert first_kwargs["target"] == HYPERLEND_POOL
        assert first_kwargs["fn_name"] == "borrow"
        assert first_kwargs["args"][0] == HYPEREVM_WHYPE
        assert first_kwargs["args"][1] == 123
        assert first_kwargs["args"][2] == 2  # variable rate mode
        assert first_kwargs["args"][3] == 0  # referral code
        assert first_kwargs["args"][4] == adapter.strategy_wallet_address

        second_kwargs = mock_encode.await_args_list[1].kwargs
        assert second_kwargs["target"] == HYPEREVM_WHYPE
        assert second_kwargs["fn_name"] == "withdraw"
        assert second_kwargs["args"][0] == 123

    @pytest.mark.asyncio
    async def test_repay_erc20_approves_and_calls_pool_repay(self, adapter):
        token = "0x0000000000000000000000000000000000000011"
        with (
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.ensure_allowance",
                new_callable=AsyncMock,
            ) as mock_allow,
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_allow.return_value = (True, "ok")
            mock_encode.return_value = {"tx": "data"}
            mock_send.return_value = "0xabc"
            adapter.strategy_wallet_signing_callback = AsyncMock()

            ok, txn = await adapter.repay(
                underlying_token=token,
                qty=123,
                chain_id=999,
                native=False,
                repay_full=True,
            )

        assert ok is True
        assert txn == "0xabc"
        mock_allow.assert_awaited_once()
        _, allow_kwargs = mock_allow.await_args
        assert allow_kwargs["spender"] == HYPERLEND_POOL
        assert allow_kwargs["approval_amount"] == MAX_UINT256

        _, kwargs = mock_encode.await_args
        assert kwargs["target"] == HYPERLEND_POOL
        assert kwargs["fn_name"] == "repay"
        assert kwargs["args"][0].lower() == token.lower()
        assert kwargs["args"][1] == MAX_UINT256
        assert kwargs["args"][2] == 2  # variable rate mode

    @pytest.mark.asyncio
    async def test_repay_native_calls_gateway_repayeth_with_value(self, adapter):
        with (
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_encode.return_value = {"tx": "data"}
            mock_send.return_value = "0xabc"
            adapter.strategy_wallet_signing_callback = AsyncMock()

            ok, txn = await adapter.repay(
                underlying_token="0x0000000000000000000000000000000000000000",
                qty=123,
                chain_id=999,
                native=True,
                repay_full=False,
            )

        assert ok is True
        assert txn == "0xabc"
        _, kwargs = mock_encode.await_args
        assert kwargs["target"] == HYPERLEND_WRAPPED_TOKEN_GATEWAY
        assert kwargs["fn_name"] == "repayETH"
        assert kwargs["args"][0] == HYPEREVM_WHYPE
        assert kwargs["args"][1] == 123
        assert kwargs["args"][2] == adapter.strategy_wallet_address
        assert kwargs["value"] == 123

    @pytest.mark.asyncio
    async def test_repay_native_full_repays_max_uint_with_buffer(self, adapter):
        mock_ui_pool = MagicMock()
        reserves = [
            {
                "underlyingAsset": HYPEREVM_WHYPE,
                "variableDebtTokenAddress": "0x00000000000000000000000000000000000000dE",
            }
        ]
        base_currency = (0, 0, 0, 0)
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

        with (
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.web3_from_chain_id",
                mock_web3_ctx,
            ),
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.get_token_balance",
                new_callable=AsyncMock,
            ) as mock_bal,
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_bal.side_effect = [1000, 10000]  # debt, native balance
            mock_encode.return_value = {"tx": "data"}
            mock_send.return_value = "0xabc"
            adapter.strategy_wallet_signing_callback = AsyncMock()

            ok, txn = await adapter.repay(
                underlying_token="0x0000000000000000000000000000000000000000",
                qty=0,
                chain_id=999,
                native=True,
                repay_full=True,
            )

        assert ok is True
        assert txn == "0xabc"
        assert mock_bal.await_count == 2
        _, kwargs = mock_encode.await_args
        assert kwargs["target"] == HYPERLEND_WRAPPED_TOKEN_GATEWAY
        assert kwargs["fn_name"] == "repayETH"
        assert kwargs["args"][0] == HYPEREVM_WHYPE
        assert kwargs["args"][1] == MAX_UINT256
        assert kwargs["args"][2] == adapter.strategy_wallet_address
        assert kwargs["value"] == 1001  # 1000 + 1 wei buffer

    @pytest.mark.asyncio
    async def test_repay_native_full_no_debt_is_noop(self, adapter):
        mock_ui_pool = MagicMock()
        reserves = [
            {
                "underlyingAsset": HYPEREVM_WHYPE,
                "variableDebtTokenAddress": "0x00000000000000000000000000000000000000dE",
            }
        ]
        base_currency = (0, 0, 0, 0)
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

        with (
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.web3_from_chain_id",
                mock_web3_ctx,
            ),
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.get_token_balance",
                new_callable=AsyncMock,
            ) as mock_bal,
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_bal.return_value = 0

            ok, txn = await adapter.repay(
                underlying_token="0x0000000000000000000000000000000000000000",
                qty=0,
                chain_id=999,
                native=True,
                repay_full=True,
            )

        assert ok is True
        assert txn is None
        mock_encode.assert_not_awaited()
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_repay_native_full_requires_sufficient_balance(self, adapter):
        mock_ui_pool = MagicMock()
        reserves = [
            {
                "underlyingAsset": HYPEREVM_WHYPE,
                "variableDebtTokenAddress": "0x00000000000000000000000000000000000000dE",
            }
        ]
        base_currency = (0, 0, 0, 0)
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

        with (
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.web3_from_chain_id",
                mock_web3_ctx,
            ),
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.get_token_balance",
                new_callable=AsyncMock,
            ) as mock_bal,
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.hyperlend_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_bal.side_effect = [1000, 999]  # debt, native balance

            ok, err = await adapter.repay(
                underlying_token="0x0000000000000000000000000000000000000000",
                qty=0,
                chain_id=999,
                native=True,
                repay_full=True,
            )

        assert ok is False
        assert "insufficient HYPE balance for repay_full" in str(err)
        mock_encode.assert_not_awaited()
        mock_send.assert_not_awaited()
