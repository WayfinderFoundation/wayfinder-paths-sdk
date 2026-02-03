from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter


class TestBRAPAdapter:
    @pytest.fixture
    def mock_brap_client(self):
        return AsyncMock()

    @pytest.fixture
    def adapter(self):
        return BRAPAdapter()

    @pytest.mark.asyncio
    async def test_best_quote_success(self, adapter, mock_brap_client):
        mock_response = {
            "quotes": [],
            "best_quote": {
                "provider": "enso",
                "input_amount": 1000000000000000000,
                "output_amount": 995000000000000000,
                "calldata": {
                    "data": "0x",
                    "to": "0x",
                    "from_address": "0x",
                    "value": "0",
                    "chainId": 8453,
                },
                "fee_estimate": {"fee_total_usd": 0.008, "fee_breakdown": []},
            },
        }
        mock_brap_client.get_quote = AsyncMock(return_value=mock_response)

        with patch(
            "wayfinder_paths.adapters.brap_adapter.adapter.BRAP_CLIENT",
            mock_brap_client,
        ):
            success, data = await adapter.best_quote(
                from_token_address="0x" + "a" * 40,
                to_token_address="0x" + "b" * 40,
                from_chain_id=8453,
                to_chain_id=1,
                from_address="0x1234567890123456789012345678901234567890",
                amount="1000000000000000000",
            )

            assert success
            assert data["input_amount"] == 1000000000000000000
            assert data["output_amount"] == 995000000000000000

    @pytest.mark.asyncio
    async def test_best_quote_no_quotes(self, adapter, mock_brap_client):
        mock_response = {"quotes": [], "best_quote": None}
        mock_brap_client.get_quote = AsyncMock(return_value=mock_response)

        with patch(
            "wayfinder_paths.adapters.brap_adapter.adapter.BRAP_CLIENT",
            mock_brap_client,
        ):
            success, data = await adapter.best_quote(
                from_token_address="0x" + "a" * 40,
                to_token_address="0x" + "b" * 40,
                from_chain_id=8453,
                to_chain_id=1,
                from_address="0x1234567890123456789012345678901234567890",
                amount="1000000000000000000",
            )

            assert success is False
            assert "No quotes available" in data

    @pytest.mark.asyncio
    async def test_best_quote_failure(self, adapter, mock_brap_client):
        mock_brap_client.get_quote = AsyncMock(side_effect=Exception("API Error"))

        with patch(
            "wayfinder_paths.adapters.brap_adapter.adapter.BRAP_CLIENT",
            mock_brap_client,
        ):
            success, data = await adapter.best_quote(
                from_token_address="0x" + "a" * 40,
                to_token_address="0x" + "b" * 40,
                from_chain_id=8453,
                to_chain_id=1,
                from_address="0x1234567890123456789012345678901234567890",
                amount="1000000000000000000",
            )

            assert success is False
            assert "API Error" in data

    def test_adapter_type(self, adapter):
        assert adapter.adapter_type == "BRAP"
