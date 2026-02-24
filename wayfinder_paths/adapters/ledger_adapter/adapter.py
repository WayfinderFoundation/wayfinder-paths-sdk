from __future__ import annotations

from typing import Any

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.adapters.decorators import status_tuple
from wayfinder_paths.core.adapters.models import Operation
from wayfinder_paths.core.clients.LedgerClient import (
    LedgerClient,
    StrategyTransactionList,
    TransactionRecord,
)


class LedgerAdapter(BaseAdapter):
    adapter_type: str = "LEDGER"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        ledger_client: LedgerClient | None = None,
    ):
        super().__init__("ledger_adapter", config)
        self.ledger_client = ledger_client or LedgerClient()

    @status_tuple
    async def get_strategy_transactions(
        self, wallet_address: str, limit: int = 50, offset: int = 0
    ) -> StrategyTransactionList:
        return await self.ledger_client.get_strategy_transactions(
            wallet_address=wallet_address, limit=limit, offset=offset
        )

    @status_tuple
    async def get_strategy_net_deposit(self, wallet_address: str) -> float:
        return await self.ledger_client.get_strategy_net_deposit(
            wallet_address=wallet_address
        )

    @status_tuple
    async def get_strategy_latest_transactions(
        self, wallet_address: str
    ) -> StrategyTransactionList:
        return await self.ledger_client.get_strategy_latest_transactions(
            wallet_address=wallet_address
        )

    @status_tuple
    async def record_deposit(
        self,
        wallet_address: str,
        chain_id: int,
        token_address: str,
        token_amount: str | float,
        usd_value: str | float,
        data: dict[str, Any] | None = None,
        strategy_name: str | None = None,
    ) -> TransactionRecord:
        return await self.ledger_client.add_strategy_deposit(
            wallet_address=wallet_address,
            chain_id=chain_id,
            token_address=token_address,
            token_amount=token_amount,
            usd_value=usd_value,
            data=data,
            strategy_name=strategy_name,
        )

    @status_tuple
    async def record_withdrawal(
        self,
        wallet_address: str,
        chain_id: int,
        token_address: str,
        token_amount: str | float,
        usd_value: str | float,
        data: dict[str, Any] | None = None,
        strategy_name: str | None = None,
    ) -> TransactionRecord:
        return await self.ledger_client.add_strategy_withdraw(
            wallet_address=wallet_address,
            chain_id=chain_id,
            token_address=token_address,
            token_amount=token_amount,
            usd_value=usd_value,
            data=data,
            strategy_name=strategy_name,
        )

    @status_tuple
    async def record_operation(
        self,
        wallet_address: str,
        operation_data: Operation,
        usd_value: str | float,
        strategy_name: str | None = None,
    ) -> TransactionRecord:
        op_dict = operation_data.model_dump(mode="json")
        return await self.ledger_client.add_strategy_operation(
            wallet_address=wallet_address,
            operation_data=op_dict,
            usd_value=usd_value,
            strategy_name=strategy_name,
        )

    async def get_transaction_summary(
        self, wallet_address: str, limit: int = 10
    ) -> tuple[bool, Any]:
        try:
            success, transactions_data = await self.get_strategy_transactions(
                wallet_address=wallet_address, limit=limit
            )

            if not success or isinstance(transactions_data, str):
                return (False, transactions_data)

            transactions = transactions_data.get("transactions", [])

            summary = {
                "total_transactions": len(transactions),
                "recent_transactions": transactions[:limit],
                "operations": {
                    "deposits": len(
                        [t for t in transactions if t.get("operation") == "DEPOSIT"]
                    ),
                    "withdrawals": len(
                        [t for t in transactions if t.get("operation") == "WITHDRAW"]
                    ),
                    "operations": len(
                        [
                            t
                            for t in transactions
                            if t.get("operation") not in ["DEPOSIT", "WITHDRAW"]
                        ]
                    ),
                },
            }

            return (True, summary)
        except Exception as e:
            self.logger.error(f"Error in get_transaction_summary: {e}")
            return (False, str(e))

    @status_tuple
    async def record_strategy_snapshot(
        self, wallet_address: str, strategy_status: dict[str, Any]
    ) -> None:
        await self.ledger_client.strategy_snapshot(
            wallet_address=wallet_address,
            strat_portfolio_value=strategy_status["portfolio_value"],
            net_deposit=strategy_status["net_deposit"],
            strategy_status=strategy_status["strategy_status"],
            gas_available=strategy_status["gas_available"],
            gassed_up=strategy_status["gassed_up"],
        )
