from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.adapters.token_adapter.adapter import TokenAdapter


class TestTokenAdapter:
    @pytest.fixture
    def mock_token_client(self):
        return AsyncMock()

    @pytest.fixture
    def adapter(self):
        return TokenAdapter()

    def test_init_with_default_config(self):
        adapter = TokenAdapter()
        assert adapter.adapter_type == "TOKEN"

    @pytest.mark.asyncio
    async def test_get_token_success(self, adapter, mock_token_client):
        mock_token_data = {
            "address": "0x1234...",
            "symbol": "TEST",
            "name": "Test Token",
            "decimals": 18,
        }
        mock_token_client.get_token_details = AsyncMock(return_value=mock_token_data)

        with patch(
            "wayfinder_paths.adapters.token_adapter.adapter.TOKEN_CLIENT",
            mock_token_client,
        ):
            success, data = await adapter.get_token("0x1234...")

            assert success
            assert data == mock_token_data

    @pytest.mark.asyncio
    async def test_get_token_not_found(self, adapter, mock_token_client):
        mock_token_client.get_token_details = AsyncMock(return_value=None)

        with patch(
            "wayfinder_paths.adapters.token_adapter.adapter.TOKEN_CLIENT",
            mock_token_client,
        ):
            success, data = await adapter.get_token("0x1234...")

            assert success is False
            assert "No token found for" in data

    @pytest.mark.asyncio
    async def test_get_token_by_token_id(self, adapter, mock_token_client):
        mock_token_data = {"address": "0x1234...", "symbol": "TEST"}
        mock_token_client.get_token_details = AsyncMock(return_value=mock_token_data)

        with patch(
            "wayfinder_paths.adapters.token_adapter.adapter.TOKEN_CLIENT",
            mock_token_client,
        ):
            success, data = await adapter.get_token("token-123")

            assert success
            assert data == mock_token_data

    def test_adapter_type(self, adapter):
        assert adapter.adapter_type == "TOKEN"

    @pytest.mark.asyncio
    async def test_get_token_price_success(self, adapter, mock_token_client):
        mock_token_data = {
            "current_price": 1.50,
            "price_change_24h": 0.05,
            "market_cap": 1000000,
            "total_volume_usd_24h": 50000,
            "symbol": "TEST",
            "name": "Test Token",
            "address": "0x1234...",
        }
        mock_token_client.get_token_details = AsyncMock(return_value=mock_token_data)

        with patch(
            "wayfinder_paths.adapters.token_adapter.adapter.TOKEN_CLIENT",
            mock_token_client,
        ):
            success, data = await adapter.get_token_price("test-token")

            assert success
            assert data["current_price"] == 1.50
            assert data["symbol"] == "TEST"
            assert data["name"] == "Test Token"
            assert data["total_volume"] == 50000
            assert data["price_change_percentage_24h"] == 5.0

    @pytest.mark.asyncio
    async def test_get_token_price_not_found(self, adapter, mock_token_client):
        mock_token_client.get_token_details = AsyncMock(return_value=None)

        with patch(
            "wayfinder_paths.adapters.token_adapter.adapter.TOKEN_CLIENT",
            mock_token_client,
        ):
            success, data = await adapter.get_token_price("invalid-token")

            assert success is False
            assert "No token found for" in data

    @pytest.mark.asyncio
    async def test_get_gas_token_success(self, adapter, mock_token_client):
        mock_gas_token_data = {
            "id": "ethereum_0x0000000000000000000000000000000000000000",
            "token_id": "ethereum_0x0000000000000000000000000000000000000000",
            "symbol": "ETH",
            "name": "Ethereum",
            "address": "0x0000000000000000000000000000000000000000",
            "decimals": 18,
        }
        mock_token_client.get_gas_token = AsyncMock(return_value=mock_gas_token_data)

        with patch(
            "wayfinder_paths.adapters.token_adapter.adapter.TOKEN_CLIENT",
            mock_token_client,
        ):
            success, data = await adapter.get_gas_token("base")

            assert success
            assert data["symbol"] == "ETH"
            assert data["name"] == "Ethereum"

    @pytest.mark.asyncio
    async def test_get_gas_token_not_found(self, adapter, mock_token_client):
        mock_token_client.get_gas_token = AsyncMock(return_value=None)

        with patch(
            "wayfinder_paths.adapters.token_adapter.adapter.TOKEN_CLIENT",
            mock_token_client,
        ):
            success, data = await adapter.get_gas_token("invalid-chain")

            assert success is False
            assert "No gas token found for chain" in data
