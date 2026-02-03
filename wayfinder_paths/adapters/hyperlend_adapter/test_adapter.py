from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.adapters.hyperlend_adapter.adapter import HyperlendAdapter


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
