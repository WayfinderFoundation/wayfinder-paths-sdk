import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from wayfinder_paths.core.adapters.models import LEND, SWAP, UNLEND
from wayfinder_paths.core.clients.LedgerClient import LedgerClient


@pytest.fixture
def temp_ledger_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def ledger_client(temp_ledger_dir):
    return LedgerClient(ledger_dir=temp_ledger_dir)


@pytest.fixture
def test_wallet_address():
    return "0x1234567890abcdef1234567890abcdef12345678"


class TestLedgerClientInitialization:
    def test_creates_ledger_directory(self, temp_ledger_dir):
        assert not (temp_ledger_dir / "transactions.json").exists()

        LedgerClient(ledger_dir=temp_ledger_dir)

        assert (temp_ledger_dir / "transactions.json").exists()
        assert (temp_ledger_dir / "snapshots.json").exists()

    def test_initializes_empty_json_files(self, ledger_client, temp_ledger_dir):
        transactions_data = json.loads(
            (temp_ledger_dir / "transactions.json").read_text()
        )
        snapshots_data = json.loads((temp_ledger_dir / "snapshots.json").read_text())

        assert transactions_data == {"transactions": []}
        assert snapshots_data == {"snapshots": []}


class TestDepositOperations:
    @pytest.mark.asyncio
    async def test_add_deposit(self, ledger_client, test_wallet_address):
        result = await ledger_client.add_strategy_deposit(
            wallet_address=test_wallet_address,
            chain_id=1,
            token_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            token_amount="1000.0",
            usd_value="1000.0",
            strategy_name="Test Strategy",
            data={"note": "Test deposit"},
        )

        assert result["status"] == "success"
        assert "transaction_id" in result
        assert "timestamp" in result

    @pytest.mark.asyncio
    async def test_deposit_creates_transaction_record(
        self, ledger_client, test_wallet_address, temp_ledger_dir
    ):
        await ledger_client.add_strategy_deposit(
            wallet_address=test_wallet_address,
            chain_id=1,
            token_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            token_amount="1000.0",
            usd_value="1000.0",
            strategy_name="Test Strategy",
        )

        data = json.loads((temp_ledger_dir / "transactions.json").read_text())
        transactions = data["transactions"]

        assert len(transactions) == 1
        assert transactions[0]["operation"] == "DEPOSIT"
        assert transactions[0]["wallet_address"] == test_wallet_address
        assert transactions[0]["usd_value"] == "1000.0"
        assert transactions[0]["strategy_name"] == "Test Strategy"


class TestWithdrawalOperations:
    @pytest.mark.asyncio
    async def test_add_withdrawal(self, ledger_client, test_wallet_address):
        result = await ledger_client.add_strategy_withdraw(
            wallet_address=test_wallet_address,
            chain_id=1,
            token_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            token_amount="500.0",
            usd_value="500.0",
            strategy_name="Test Strategy",
        )

        assert result["status"] == "success"
        assert "transaction_id" in result


class TestOperationRecording:
    @pytest.mark.asyncio
    async def test_add_swap_operation(self, ledger_client, test_wallet_address):
        swap_op = SWAP(
            adapter="TestAdapter",
            from_token_id="usd-coin-base",
            to_token_id="aerodrome-usdc-base",
            from_amount="1000000000",
            to_amount="1000000000",
            from_amount_usd=1000.0,
            to_amount_usd=1000.0,
            transaction_hash="0xabc123",
            transaction_chain_id=8453,
        )

        result = await ledger_client.add_strategy_operation(
            wallet_address=test_wallet_address,
            operation_data=swap_op.model_dump(mode="json"),
            usd_value="1000.0",
            strategy_name="Test Strategy",
        )

        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_add_lend_operation(self, ledger_client, test_wallet_address):
        lend_op = LEND(
            adapter="TestAdapter",
            token_address="0xTokenAddress",
            pool_address="0xPoolContract",
            amount="1000000000",
            amount_usd=1000.0,
            transaction_hash="0xdef456",
            transaction_chain_id=8453,
        )

        result = await ledger_client.add_strategy_operation(
            wallet_address=test_wallet_address,
            operation_data=lend_op.model_dump(mode="json"),
            usd_value="1000.0",
        )

        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_add_unlend_operation(self, ledger_client, test_wallet_address):
        unlend_op = UNLEND(
            adapter="TestAdapter",
            token_address="0xTokenAddress",
            pool_address="0xPoolContract",
            amount="1000000000",
            amount_usd=1000.0,
            transaction_hash="0xghi789",
            transaction_chain_id=8453,
        )

        result = await ledger_client.add_strategy_operation(
            wallet_address=test_wallet_address,
            operation_data=unlend_op.model_dump(mode="json"),
            usd_value="1000.0",
        )

        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_operation_stores_op_data(
        self, ledger_client, test_wallet_address, temp_ledger_dir
    ):
        swap_op = SWAP(
            adapter="TestAdapter",
            from_token_id="token-a",
            to_token_id="token-b",
            from_amount="100",
            to_amount="95",
            from_amount_usd=100.0,
            to_amount_usd=95.0,
            transaction_hash="0xjkl012",
            transaction_chain_id=8453,
        )

        await ledger_client.add_strategy_operation(
            wallet_address=test_wallet_address,
            operation_data=swap_op.model_dump(mode="json"),
            usd_value="100.0",
        )

        data = json.loads((temp_ledger_dir / "transactions.json").read_text())
        transaction = data["transactions"][0]

        assert transaction["operation"] == "STRAT_OP"
        assert "data" in transaction
        assert "op_data" in transaction["data"]
        assert transaction["data"]["op_data"]["type"] == "SWAP"
        assert transaction["usd_value"] == "100.0"
        assert "id" in transaction
        assert "timestamp" in transaction
        assert "wallet_address" in transaction
        # Stored minimal; amount/token_address derived when formatting
        assert transaction["amount"] == "0"
        assert transaction["token_address"] == ""

        # Formatted output derives amount/token_address from op_data
        list_result = await ledger_client.get_strategy_transactions(
            wallet_address=test_wallet_address
        )
        txn = list_result["transactions"][0]
        assert txn["amount"] == "95"
        assert txn["token_address"] == "token-b"


class TestTransactionRetrieval:
    @pytest.mark.asyncio
    async def test_get_empty_transactions(self, ledger_client, test_wallet_address):
        result = await ledger_client.get_strategy_transactions(
            wallet_address=test_wallet_address
        )

        assert result["transactions"] == []
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_get_transactions_filters_by_wallet(
        self, ledger_client, test_wallet_address
    ):
        await ledger_client.add_strategy_deposit(
            wallet_address=test_wallet_address,
            chain_id=1,
            token_address="0xTest",
            token_amount="100",
            usd_value="100",
        )

        await ledger_client.add_strategy_deposit(
            wallet_address="0xDifferentWallet",
            chain_id=1,
            token_address="0xTest",
            token_amount="200",
            usd_value="200",
        )

        result = await ledger_client.get_strategy_transactions(
            wallet_address=test_wallet_address
        )

        assert result["total"] == 1
        # Return shape is StrategyTransaction (no wallet_address in list items)
        tx = result["transactions"][0]
        assert tx["operation"] == "DEPOSIT"
        assert tx["amount"] == "100"
        assert tx["token_address"] == "0xTest"
        assert tx["usd_value"] == "100"

    @pytest.mark.asyncio
    async def test_get_transactions_pagination(
        self, ledger_client, test_wallet_address
    ):
        for i in range(5):
            await ledger_client.add_strategy_deposit(
                wallet_address=test_wallet_address,
                chain_id=1,
                token_address="0xTest",
                token_amount=str(100 * (i + 1)),
                usd_value=str(100 * (i + 1)),
            )

        result = await ledger_client.get_strategy_transactions(
            wallet_address=test_wallet_address, limit=2, offset=0
        )

        assert result["total"] == 5
        assert len(result["transactions"]) == 2
        assert result["limit"] == 2
        assert result["offset"] == 0

        result = await ledger_client.get_strategy_transactions(
            wallet_address=test_wallet_address, limit=2, offset=2
        )

        assert len(result["transactions"]) == 2
        assert result["offset"] == 2

    @pytest.mark.asyncio
    async def test_get_latest_transactions(self, ledger_client, test_wallet_address):
        # get_strategy_latest_transactions returns only STRAT_OP (limit 80), matching vault
        for i in range(3):
            await ledger_client.add_strategy_operation(
                wallet_address=test_wallet_address,
                operation_data={"type": "SWAP", "to_token_id": f"token-{i}"},
                usd_value=str(i),
            )

        result = await ledger_client.get_strategy_latest_transactions(
            wallet_address=test_wallet_address
        )

        assert len(result["transactions"]) == 3
        assert result["limit"] == 80
        assert result["offset"] == 0
        assert result["total"] == 3
        # Should be sorted by timestamp descending (most recent first)


class TestNetDepositCalculation:
    @pytest.mark.asyncio
    async def test_net_deposit_empty(self, ledger_client, test_wallet_address):
        result = await ledger_client.get_strategy_net_deposit(
            wallet_address=test_wallet_address
        )

        assert result == 0.0

    @pytest.mark.asyncio
    async def test_net_deposit_only_deposits(self, ledger_client, test_wallet_address):
        await ledger_client.add_strategy_deposit(
            wallet_address=test_wallet_address,
            chain_id=1,
            token_address="0xTest",
            token_amount="1000",
            usd_value="1000",
        )

        await ledger_client.add_strategy_deposit(
            wallet_address=test_wallet_address,
            chain_id=1,
            token_address="0xTest",
            token_amount="500",
            usd_value="500",
        )

        result = await ledger_client.get_strategy_net_deposit(
            wallet_address=test_wallet_address
        )

        assert result == 1500.0

    @pytest.mark.asyncio
    async def test_net_deposit_with_withdrawals(
        self, ledger_client, test_wallet_address
    ):
        await ledger_client.add_strategy_deposit(
            wallet_address=test_wallet_address,
            chain_id=1,
            token_address="0xTest",
            token_amount="1000",
            usd_value="1000",
        )

        await ledger_client.add_strategy_withdraw(
            wallet_address=test_wallet_address,
            chain_id=1,
            token_address="0xTest",
            token_amount="300",
            usd_value="300",
        )

        result = await ledger_client.get_strategy_net_deposit(
            wallet_address=test_wallet_address
        )

        assert result == 700.0


class TestSnapshotRecording:
    @pytest.mark.asyncio
    async def test_record_snapshot(self, ledger_client, test_wallet_address):
        await ledger_client.strategy_snapshot(
            wallet_address=test_wallet_address,
            strat_portfolio_value=1050.0,
            net_deposit=1000.0,
            strategy_status={"current_pool": "test-pool", "apy": "5.2%"},
            gas_available=0.01,
            gassed_up=True,
        )

        # Verify snapshot was saved (no return value)
        # We verify by reading the file
        # This is done indirectly through the client

    @pytest.mark.asyncio
    async def test_snapshot_creates_record(
        self, ledger_client, test_wallet_address, temp_ledger_dir
    ):
        await ledger_client.strategy_snapshot(
            wallet_address=test_wallet_address,
            strat_portfolio_value=1050.0,
            net_deposit=1000.0,
            strategy_status={"pool": "test"},
            gas_available=0.01,
            gassed_up=True,
        )

        data = json.loads((temp_ledger_dir / "snapshots.json").read_text())
        snapshots = data["snapshots"]

        assert len(snapshots) == 1
        assert snapshots[0]["wallet_address"] == test_wallet_address
        assert snapshots[0]["portfolio_value"] == 1050.0
        assert snapshots[0]["net_deposit"] == 1000.0
        assert snapshots[0]["gas_available"] == 0.01
        assert snapshots[0]["gassed_up"] is True
        assert snapshots[0]["strategy_status"] == {"pool": "test"}

    @pytest.mark.asyncio
    async def test_multiple_snapshots(
        self, ledger_client, test_wallet_address, temp_ledger_dir
    ):
        for i in range(3):
            await ledger_client.strategy_snapshot(
                wallet_address=test_wallet_address,
                strat_portfolio_value=1000.0 + (i * 10),
                net_deposit=1000.0,
                strategy_status={"iteration": i},
                gas_available=0.01,
                gassed_up=True,
            )

        data = json.loads((temp_ledger_dir / "snapshots.json").read_text())
        snapshots = data["snapshots"]

        assert len(snapshots) == 3
        assert snapshots[0]["portfolio_value"] == 1000.0
        assert snapshots[1]["portfolio_value"] == 1010.0
        assert snapshots[2]["portfolio_value"] == 1020.0


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_writes(self, ledger_client, test_wallet_address):
        async def add_deposit(amount):
            await ledger_client.add_strategy_deposit(
                wallet_address=test_wallet_address,
                chain_id=1,
                token_address="0xTest",
                token_amount=str(amount),
                usd_value=str(amount),
            )

        # Execute multiple deposits concurrently
        await asyncio.gather(*[add_deposit(i * 100) for i in range(5)])

        result = await ledger_client.get_strategy_transactions(
            wallet_address=test_wallet_address
        )

        # All 5 transactions should be recorded
        assert result["total"] == 5
