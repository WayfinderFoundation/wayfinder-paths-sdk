from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.adapters.balance_adapter.adapter import BalanceAdapter
from wayfinder_paths.core.utils.token_resolver import TokenResolver


@pytest.fixture(autouse=True)
def _clear_token_resolver_cache():
    TokenResolver._token_details_cache.clear()
    TokenResolver._gas_token_cache.clear()


class TestBalanceAdapter:
    @pytest.fixture
    def adapter(self):
        return BalanceAdapter(config={})

    def test_adapter_type(self, adapter):
        assert adapter.adapter_type == "BALANCE"

    @pytest.mark.asyncio
    async def test_get_balance_with_token_id(self, adapter):
        token_address = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

        with (
            patch(
                "wayfinder_paths.adapters.balance_adapter.adapter.TokenResolver.resolve_token",
                new_callable=AsyncMock,
                return_value=(8453, token_address),
            ) as mock_resolve,
            patch(
                "wayfinder_paths.adapters.balance_adapter.adapter.get_token_balance",
                new_callable=AsyncMock,
                return_value=1_000_000,
            ) as mock_get_balance,
        ):
            success, balance = await adapter.get_balance(
                token_id="usd-coin-base",
                wallet_address="0xWallet",
            )

            assert success is True
            assert balance == 1_000_000
            mock_resolve.assert_called_once_with("usd-coin-base", chain_id=None)
            mock_get_balance.assert_called_once_with(token_address, 8453, "0xWallet")

    @pytest.mark.asyncio
    async def test_get_balance_with_token_address(self, adapter):
        with patch(
            "wayfinder_paths.adapters.balance_adapter.adapter.get_token_balance",
            new_callable=AsyncMock,
            return_value=5_000_000,
        ) as mock_get_balance:
            success, balance = await adapter.get_balance(
                token_address="0xTokenAddress",
                wallet_address="0xWallet",
                chain_id=8453,
            )

            assert success is True
            assert balance == 5_000_000
            mock_get_balance.assert_called_once_with("0xTokenAddress", 8453, "0xWallet")

    @pytest.mark.asyncio
    async def test_get_balance_token_not_found(self, adapter):
        with patch(
            "wayfinder_paths.adapters.balance_adapter.adapter.TokenResolver.resolve_token",
            new_callable=AsyncMock,
            side_effect=ValueError("Cannot resolve token: invalid-token"),
        ):
            success, error = await adapter.get_balance(
                token_id="invalid-token",
                wallet_address="0xWallet",
            )

            assert success is False
            assert "Cannot resolve token" in str(error)

    @pytest.mark.asyncio
    async def test_get_balance_parses_address_token_id_locally(self, adapter):
        token_id = "base_0x1111111111111111111111111111111111111111"

        with (
            patch(
                "wayfinder_paths.core.utils.token_resolver.TOKEN_CLIENT.get_token_details",
                new=AsyncMock(),
            ) as mock_token_details,
            patch(
                "wayfinder_paths.adapters.balance_adapter.adapter.get_token_balance",
                new_callable=AsyncMock,
                return_value=123,
            ) as mock_get_balance,
        ):
            success, balance = await adapter.get_balance(
                token_id=token_id,
                wallet_address="0xWallet",
            )

            assert success is True
            assert balance == 123
            mock_token_details.assert_not_called()
            mock_get_balance.assert_called_once_with(
                "0x1111111111111111111111111111111111111111", 8453, "0xWallet"
            )

    @pytest.mark.asyncio
    async def test_get_balance_details_fetches_balance_and_decimals(self, adapter):
        token_address = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

        with (
            patch(
                "wayfinder_paths.adapters.balance_adapter.adapter.TokenResolver.resolve_token",
                new_callable=AsyncMock,
                return_value=(8453, token_address),
            ) as mock_resolve,
            patch(
                "wayfinder_paths.adapters.balance_adapter.adapter.get_token_balance_with_decimals",
                new_callable=AsyncMock,
                return_value=(1_000_000, 6),
            ) as mock_get_balance,
        ):
            ok, out = await adapter.get_balance_details(
                token_id="usd-coin-base",
                wallet_address="0xWallet",
            )

            assert ok is True
            assert isinstance(out, dict)
            assert out["balance_raw"] == 1_000_000
            assert out["decimals"] == 6
            assert out["balance_decimal"] == 1.0

            mock_resolve.assert_called_once_with("usd-coin-base", chain_id=None)
            mock_get_balance.assert_called_once_with(
                token_address,
                8453,
                "0xWallet",
                balance_block_identifier="pending",
                default_native_decimals=18,
            )

    @pytest.mark.asyncio
    async def test_get_balance_details_falls_back_to_onchain_decimals(self, adapter):
        token_id = "base_0x1111111111111111111111111111111111111111"

        with (
            patch(
                "wayfinder_paths.core.utils.token_resolver.TOKEN_CLIENT.get_token_details",
                new=AsyncMock(),
            ) as mock_token_details,
            patch(
                "wayfinder_paths.adapters.balance_adapter.adapter.get_token_balance_with_decimals",
                new_callable=AsyncMock,
                return_value=(1_000_000, 6),
            ) as mock_get_balance,
        ):
            ok, out = await adapter.get_balance_details(
                token_id=token_id,
                wallet_address="0xWallet",
            )

            assert ok is True
            assert isinstance(out, dict)
            assert out["balance_raw"] == 1_000_000
            assert out["decimals"] == 6
            assert out["balance_decimal"] == 1.0

            mock_token_details.assert_not_called()
            mock_get_balance.assert_called_once_with(
                "0x1111111111111111111111111111111111111111",
                8453,
                "0xWallet",
                balance_block_identifier="pending",
                default_native_decimals=18,
            )
