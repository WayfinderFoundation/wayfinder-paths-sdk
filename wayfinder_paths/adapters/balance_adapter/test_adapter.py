from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.adapters.balance_adapter.adapter import BalanceAdapter


class TestBalanceAdapter:
    @pytest.fixture
    def mock_token_client(self):
        return AsyncMock()

    @pytest.fixture
    def adapter(self):
        return BalanceAdapter(config={})

    def test_adapter_type(self, adapter):
        assert adapter.adapter_type == "BALANCE"

    @pytest.mark.asyncio
    async def test_get_balance_with_token_id(self, adapter, mock_token_client):
        mock_token_client.get_token_details = AsyncMock(
            return_value={
                "token_id": "usd-coin-base",
                "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "chain": {"id": 8453, "code": "base"},
            }
        )

        with (
            patch(
                "wayfinder_paths.adapters.balance_adapter.adapter.TOKEN_CLIENT",
                mock_token_client,
            ),
            patch(
                "wayfinder_paths.adapters.balance_adapter.adapter.get_token_balance",
                new_callable=AsyncMock,
                return_value=1000000,
            ) as mock_get_balance,
        ):
            success, balance = await adapter.get_balance(
                token_id="usd-coin-base",
                wallet_address="0xWallet",
            )

            assert success
            assert balance == 1000000
            mock_token_client.get_token_details.assert_called_once_with("usd-coin-base")
            mock_get_balance.assert_called_once_with(
                "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 8453, "0xWallet"
            )

    @pytest.mark.asyncio
    async def test_get_balance_with_token_address(self, adapter):
        with patch(
            "wayfinder_paths.adapters.balance_adapter.adapter.get_token_balance",
            new_callable=AsyncMock,
            return_value=5000000,
        ) as mock_get_balance:
            success, balance = await adapter.get_balance(
                token_address="0xTokenAddress",
                wallet_address="0xWallet",
                chain_id=8453,
            )

            assert success
            assert balance == 5000000
            mock_get_balance.assert_called_once_with("0xTokenAddress", 8453, "0xWallet")

    @pytest.mark.asyncio
    async def test_get_balance_token_not_found(self, adapter, mock_token_client):
        mock_token_client.get_token_details = AsyncMock(return_value=None)

        with patch(
            "wayfinder_paths.adapters.balance_adapter.adapter.TOKEN_CLIENT",
            mock_token_client,
        ):
            success, error = await adapter.get_balance(
                token_id="invalid-token",
                wallet_address="0xWallet",
            )

            assert success is False
            assert "NoneType" in error or "subscriptable" in error
