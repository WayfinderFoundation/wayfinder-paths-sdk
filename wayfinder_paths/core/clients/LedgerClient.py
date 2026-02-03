from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NotRequired, Required, TypedDict


class StrategyTransaction(TypedDict):
    id: Required[str]
    operation: Required[str]
    timestamp: Required[str]
    amount: Required[str]
    token_address: Required[str]
    usd_value: Required[str]
    strategy_name: NotRequired[str | None]
    chain_id: NotRequired[int | None]
    created: NotRequired[str]
    op_data: NotRequired[dict[str, Any]]


class StrategyTransactionList(TypedDict):
    transactions: Required[list[StrategyTransaction]]
    total: Required[int]
    limit: Required[int]
    offset: Required[int]


class TransactionRecord(TypedDict):
    transaction_id: Required[str]
    status: Required[str]
    timestamp: Required[str]


class OperationData(TypedDict, total=False):
    type: str
    transaction_hash: str
    transaction_chain_id: int


class StrategyOperationTransactionData(TypedDict):
    op_data: OperationData


class LedgerClient:
    def __init__(self, ledger_dir: Path | str | None = None) -> None:
        if ledger_dir is None:
            ledger_dir = Path(__file__).resolve().parents[3] / ".ledger"
        self.ledger_dir = Path(ledger_dir)
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        self.transactions_file = self.ledger_dir / "transactions.json"
        self.snapshots_file = self.ledger_dir / "snapshots.json"
        self._transactions_lock = asyncio.Lock()
        self._snapshots_lock = asyncio.Lock()
        for path, default in [
            (self.transactions_file, {"transactions": []}),
            (self.snapshots_file, {"snapshots": []}),
        ]:
            if not path.exists():
                path.write_text(json.dumps(default, indent=2))

    @staticmethod
    def _load_json_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return default

    @staticmethod
    def _save_json_file(path: Path, data: dict[str, Any]) -> None:
        path.write_text(json.dumps(data, indent=2))

    async def _read_json(
        self, path: Path, lock: asyncio.Lock, default: dict[str, Any]
    ) -> dict[str, Any]:
        async with lock:
            return self._load_json_file(path, default)

    async def _write_json(
        self, path: Path, lock: asyncio.Lock, data: dict[str, Any]
    ) -> None:
        async with lock:
            self._save_json_file(path, data)

    async def _read_transactions(self) -> dict[str, Any]:
        return await self._read_json(
            self.transactions_file, self._transactions_lock, {"transactions": []}
        )

    async def _write_transactions(self, data: dict[str, Any]) -> None:
        await self._write_json(self.transactions_file, self._transactions_lock, data)

    async def _read_snapshots(self) -> dict[str, Any]:
        return await self._read_json(
            self.snapshots_file, self._snapshots_lock, {"snapshots": []}
        )

    async def _write_snapshots(self, data: dict[str, Any]) -> None:
        await self._write_json(self.snapshots_file, self._snapshots_lock, data)

    async def _transactions_for_wallet(
        self,
        wallet_address: str,
        *,
        operation: str | None = None,
    ) -> list[dict[str, Any]]:
        data = await self._read_transactions()
        txs = [
            tx
            for tx in data.get("transactions", [])
            if tx.get("wallet_address", "").lower() == wallet_address.lower()
        ]
        if operation is not None:
            txs = [tx for tx in txs if tx.get("operation") == operation]
        txs.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return txs

    def _to_strategy_transaction(self, tx: dict[str, Any]) -> StrategyTransaction:
        operation = tx["operation"]
        op_data: dict[str, Any] = {}
        if operation == "STRAT_OP":
            op_data = (tx.get("data") or {}).get("op_data") or {}
        if op_data:
            operation = op_data.get("type") or operation

        amount = tx["amount"]
        token_address = tx["token_address"]
        if op_data:
            op_type = op_data.get("type", "")
            if op_type == "SWAP":
                token_address = op_data.get("to_token_id", "")
                amount = op_data.get("to_amount", "0")
            elif op_type in ("LEND", "UNLEND"):
                token_address = op_data.get("contract", "")
                amount = str(op_data.get("amount", 0))
            else:
                amount = amount or "0"
                token_address = token_address or ""

        out: StrategyTransaction = {
            "id": tx["id"],
            "operation": str(operation),
            "timestamp": tx["timestamp"],
            "created": tx["timestamp"],
            "amount": amount,
            "token_address": token_address,
            "usd_value": tx["usd_value"],
        }
        if "chain_id" in tx:
            out["chain_id"] = tx["chain_id"]
        if "strategy_name" in tx:
            out["strategy_name"] = tx["strategy_name"]
        if op_data:
            out["op_data"] = op_data
        return out

    @staticmethod
    def _record(transaction_id: str, timestamp: str) -> TransactionRecord:
        return {
            "transaction_id": transaction_id,
            "status": "success",
            "timestamp": timestamp,
        }

    async def _append_transaction(self, transaction: dict[str, Any]) -> None:
        async with self._transactions_lock:
            data = self._load_json_file(self.transactions_file, {"transactions": []})
            data.setdefault("transactions", []).append(transaction)
            self._save_json_file(self.transactions_file, data)

    async def get_strategy_transactions(
        self,
        *,
        wallet_address: str,
        limit: int = 50,
        offset: int = 0,
        operation: str | None = None,
    ) -> StrategyTransactionList:
        filtered = await self._transactions_for_wallet(
            wallet_address, operation=operation
        )
        total = len(filtered)
        paginated = filtered[offset : offset + limit]
        transactions = [self._to_strategy_transaction(tx) for tx in paginated]
        return {
            "transactions": transactions,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def get_strategy_net_deposit(self, *, wallet_address: str) -> float:
        filtered = await self._transactions_for_wallet(wallet_address)
        total_deposits = 0.0
        total_withdrawals = 0.0
        for tx in filtered:
            op = tx.get("operation", "").upper()
            usd = float(tx.get("usd_value", 0))
            if op == "DEPOSIT":
                total_deposits += usd
            elif op == "WITHDRAW":
                total_withdrawals += usd
        return total_deposits - total_withdrawals

    async def get_strategy_latest_transactions(
        self, *, wallet_address: str
    ) -> StrategyTransactionList:
        return await self.get_strategy_transactions(
            wallet_address=wallet_address,
            limit=80,
            offset=0,
            operation="STRAT_OP",
        )

    async def _add_deposit_or_withdraw(
        self,
        operation: Literal["DEPOSIT", "WITHDRAW"],
        *,
        wallet_address: str,
        chain_id: int,
        token_address: str,
        token_amount: str | float,
        usd_value: str | float,
        data: dict[str, Any] | None = None,
        strategy_name: str | None = None,
    ) -> TransactionRecord:
        transaction_id = str(uuid.uuid4())
        timestamp = datetime.now(UTC).isoformat()
        transaction: dict[str, Any] = {
            "id": transaction_id,
            "wallet_address": wallet_address,
            "operation": operation,
            "timestamp": timestamp,
            "amount": str(token_amount),
            "token_address": token_address,
            "usd_value": str(usd_value),
            "chain_id": chain_id,
        }
        if data is not None:
            transaction["data"] = data
        if strategy_name is not None:
            transaction["strategy_name"] = strategy_name
        await self._append_transaction(transaction)
        return self._record(transaction_id, timestamp)

    async def add_strategy_deposit(
        self,
        *,
        wallet_address: str,
        chain_id: int,
        token_address: str,
        token_amount: str | float,
        usd_value: str | float,
        data: dict[str, Any] | None = None,
        strategy_name: str | None = None,
    ) -> TransactionRecord:
        return await self._add_deposit_or_withdraw(
            "DEPOSIT",
            wallet_address=wallet_address,
            chain_id=chain_id,
            token_address=token_address,
            token_amount=token_amount,
            usd_value=usd_value,
            data=data,
            strategy_name=strategy_name,
        )

    async def add_strategy_withdraw(
        self,
        *,
        wallet_address: str,
        chain_id: int,
        token_address: str,
        token_amount: str | float,
        usd_value: str | float,
        data: dict[str, Any] | None = None,
        strategy_name: str | None = None,
    ) -> TransactionRecord:
        return await self._add_deposit_or_withdraw(
            "WITHDRAW",
            wallet_address=wallet_address,
            chain_id=chain_id,
            token_address=token_address,
            token_amount=token_amount,
            usd_value=usd_value,
            data=data,
            strategy_name=strategy_name,
        )

    async def add_strategy_operation(
        self,
        *,
        wallet_address: str,
        operation_data: dict[str, Any],
        usd_value: str | float,
        strategy_name: str | None = None,
    ) -> TransactionRecord:
        transaction_id = str(uuid.uuid4())
        timestamp = datetime.now(UTC).isoformat()
        transaction: dict[str, Any] = {
            "id": transaction_id,
            "wallet_address": wallet_address,
            "operation": "STRAT_OP",
            "timestamp": timestamp,
            "amount": "0",
            "token_address": "",
            "usd_value": str(usd_value),
            "data": {"op_data": operation_data},
        }
        if strategy_name is not None:
            transaction["strategy_name"] = strategy_name
        await self._append_transaction(transaction)
        return self._record(transaction_id, timestamp)

    async def _append_snapshot(self, snapshot: dict[str, Any]) -> None:
        async with self._snapshots_lock:
            data = self._load_json_file(self.snapshots_file, {"snapshots": []})
            data.setdefault("snapshots", []).append(snapshot)
            self._save_json_file(self.snapshots_file, data)

    async def strategy_snapshot(
        self,
        wallet_address: str,
        strat_portfolio_value: float,
        net_deposit: float,
        strategy_status: dict,
        gas_available: float,
        gassed_up: bool,
    ) -> None:
        snapshot = {
            "id": str(uuid.uuid4()),
            "wallet_address": wallet_address,
            "timestamp": datetime.now(UTC).isoformat(),
            "portfolio_value": strat_portfolio_value,
            "net_deposit": net_deposit,
            "gas_available": gas_available,
            "gassed_up": gassed_up,
            "strategy_status": strategy_status,
        }
        await self._append_snapshot(snapshot)
