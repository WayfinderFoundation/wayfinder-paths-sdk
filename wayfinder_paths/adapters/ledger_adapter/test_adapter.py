from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.adapters.ledger_adapter.adapter import LedgerAdapter
from wayfinder_paths.core.adapters.models import SWAP


class TestLedgerAdapter:
    @pytest.fixture
    def mock_ledger_client(self):
        mock_client = AsyncMock()
        return mock_client

    @pytest.fixture
    def adapter(self, mock_ledger_client):
        adapter = LedgerAdapter()
        adapter.ledger_client = mock_ledger_client
        return adapter

    @pytest.mark.asyncio
    async def test_get_strategy_transactions_success(self, adapter, mock_ledger_client):
        mock_response = {
            "transactions": [
                {
                    "id": "tx_123",
                    "operation": "DEPOSIT",
                    "amount": "1000000000000000000",
                    "usd_value": "1000.00",
                }
            ],
            "total": 1,
        }
        mock_ledger_client.get_strategy_transactions = AsyncMock(
            return_value=mock_response
        )

        success, data = await adapter.get_strategy_transactions(
            wallet_address="0x1234567890123456789012345678901234567890",
            limit=10,
            offset=0,
        )

        assert success
        assert data == mock_response
        mock_ledger_client.get_strategy_transactions.assert_called_once_with(
            wallet_address="0x1234567890123456789012345678901234567890",
            limit=10,
            offset=0,
        )

    @pytest.mark.asyncio
    async def test_get_strategy_transactions_failure(self, adapter, mock_ledger_client):
        mock_ledger_client.get_strategy_transactions = AsyncMock(
            side_effect=Exception("API Error")
        )

        success, data = await adapter.get_strategy_transactions(
            wallet_address="0x1234567890123456789012345678901234567890"
        )

        assert success is False
        assert "API Error" in data

    @pytest.mark.asyncio
    async def test_get_strategy_net_deposit_success(self, adapter, mock_ledger_client):
        mock_response = {
            "net_deposit": "1000.00",
            "total_deposits": "1500.00",
            "total_withdrawals": "500.00",
        }
        mock_ledger_client.get_strategy_net_deposit = AsyncMock(
            return_value=mock_response
        )

        # Test
        success, data = await adapter.get_strategy_net_deposit(
            wallet_address="0x1234567890123456789012345678901234567890"
        )

        assert success
        assert data == mock_response
        mock_ledger_client.get_strategy_net_deposit.assert_called_once_with(
            wallet_address="0x1234567890123456789012345678901234567890"
        )

    @pytest.mark.asyncio
    async def test_record_deposit_success(self, adapter, mock_ledger_client):
        mock_response = {
            "transaction_id": "tx_456",
            "status": "recorded",
            "timestamp": "2024-01-15T10:30:00Z",
        }
        mock_ledger_client.add_strategy_deposit.return_value = mock_response

        # Test
        success, data = await adapter.record_deposit(
            wallet_address="0x1234567890123456789012345678901234567890",
            chain_id=8453,
            token_address="0xA0b86a33E6441c8C06DdD4D4c4c4c4c4c4c4c4c4c",
            token_amount="1000000000000000000",
            usd_value="1000.00",
            strategy_name="TestStrategy",
        )

        assert success
        assert data == mock_response
        mock_ledger_client.add_strategy_deposit.assert_called_once_with(
            wallet_address="0x1234567890123456789012345678901234567890",
            chain_id=8453,
            token_address="0xA0b86a33E6441c8C06DdD4D4c4c4c4c4c4c4c4c4c",
            token_amount="1000000000000000000",
            usd_value="1000.00",
            data=None,
            strategy_name="TestStrategy",
        )

    @pytest.mark.asyncio
    async def test_record_withdrawal_success(self, adapter, mock_ledger_client):
        mock_response = {
            "transaction_id": "tx_789",
            "status": "recorded",
            "timestamp": "2024-01-15T11:00:00Z",
        }
        mock_ledger_client.add_strategy_withdraw.return_value = mock_response

        # Test
        success, data = await adapter.record_withdrawal(
            wallet_address="0x1234567890123456789012345678901234567890",
            chain_id=8453,
            token_address="0xA0b86a33E6441c8C06DdD4D4c4c4c4c4c4c4c4c4c",
            token_amount="500000000000000000",
            usd_value="500.00",
            strategy_name="TestStrategy",
        )

        assert success
        assert data == mock_response

    @pytest.mark.asyncio
    async def test_record_operation_success(self, adapter, mock_ledger_client):
        mock_response = {
            "operation_id": "op_123",
            "status": "recorded",
            "timestamp": "2024-01-15T10:45:00Z",
        }
        mock_ledger_client.add_strategy_operation.return_value = mock_response

        # Test
        operation_data = SWAP(
            adapter="TestAdapter",
            from_token_id="0xA0b86a33E6441c8C06DdD4D4c4c4c4c4c4c4c4c4c",
            to_token_id="0xB1c97a44F7552d9Dd5e5e5e5e5e5e5e5e5e5e5e5e5e",
            from_amount="1000000000000000000",
            to_amount="995000000000000000",
            from_amount_usd=1000.0,
            to_amount_usd=995.0,
            transaction_hash="0x123abc",
            transaction_chain_id=8453,
        )

        success, data = await adapter.record_operation(
            wallet_address="0x1234567890123456789012345678901234567890",
            operation_data=operation_data,
            usd_value="1000.00",
            strategy_name="TestStrategy",
        )

        assert success
        assert data == mock_response

    @pytest.mark.asyncio
    async def test_get_transaction_summary_success(self, adapter, mock_ledger_client):
        mock_transactions = {
            "transactions": [
                {"operation": "DEPOSIT", "amount": "1000000000000000000"},
                {"operation": "WITHDRAW", "amount": "500000000000000000"},
                {"operation": "SWAP", "amount": "200000000000000000"},
            ]
        }
        mock_ledger_client.get_strategy_transactions.return_value = mock_transactions

        # Test
        success, data = await adapter.get_transaction_summary(
            wallet_address="0x1234567890123456789012345678901234567890", limit=10
        )

        assert success
        assert data["total_transactions"] == 3
        assert data["operations"]["deposits"] == 1
        assert data["operations"]["withdrawals"] == 1
        assert data["operations"]["operations"] == 1

    def test_adapter_type(self, adapter):
        assert adapter.adapter_type == "LEDGER"
