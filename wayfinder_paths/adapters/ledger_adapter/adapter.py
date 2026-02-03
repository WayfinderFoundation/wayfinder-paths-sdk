from typing import Any

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.adapters.models import Operation
from wayfinder_paths.core.clients.LedgerClient import (
    LedgerClient,
    StrategyTransactionList,
    TransactionRecord,
)
from wayfinder_paths.core.strategies.Strategy import StatusDict


class LedgerAdapter(BaseAdapter):
    adapter_type: str = "LEDGER"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        ledger_client: LedgerClient | None = None,
    ):
        super().__init__("ledger_adapter", config)
        self.ledger_client = ledger_client or LedgerClient()

    async def get_strategy_transactions(
        self, wallet_address: str, limit: int = 50, offset: int = 0
    ) -> tuple[bool, StrategyTransactionList | str]:
        try:
            data = await self.ledger_client.get_strategy_transactions(
                wallet_address=wallet_address, limit=limit, offset=offset
            )
            return (True, data)
        except Exception as e:
            self.logger.error(f"Error fetching strategy transactions: {e}")
            return (False, str(e))

    async def get_strategy_net_deposit(
        self, wallet_address: str
    ) -> tuple[bool, float | str]:
        try:
            data = await self.ledger_client.get_strategy_net_deposit(
                wallet_address=wallet_address
            )
            return (True, data)
        except Exception as e:
            self.logger.error(f"Error fetching strategy net deposit: {e}")
            return (False, str(e))

    async def get_strategy_latest_transactions(
        self, wallet_address: str
    ) -> tuple[bool, StrategyTransactionList | str]:
        try:
            data = await self.ledger_client.get_strategy_latest_transactions(
                wallet_address=wallet_address
            )
            return (True, data)
        except Exception as e:
            self.logger.error(f"Error fetching strategy last transactions: {e}")
            return (False, str(e))

    async def record_deposit(
        self,
        wallet_address: str,
        chain_id: int,
        token_address: str,
        token_amount: str | float,
        usd_value: str | float,
        data: dict[str, Any] | None = None,
        strategy_name: str | None = None,
    ) -> tuple[bool, TransactionRecord | str]:
        try:
            result = await self.ledger_client.add_strategy_deposit(
                wallet_address=wallet_address,
                chain_id=chain_id,
                token_address=token_address,
                token_amount=token_amount,
                usd_value=usd_value,
                data=data,
                strategy_name=strategy_name,
            )
            return (True, result)
        except Exception as e:
            self.logger.error(f"Error recording deposit: {e}")
            return (False, str(e))

    async def record_withdrawal(
        self,
        wallet_address: str,
        chain_id: int,
        token_address: str,
        token_amount: str | float,
        usd_value: str | float,
        data: dict[str, Any] | None = None,
        strategy_name: str | None = None,
    ) -> tuple[bool, TransactionRecord | str]:
        try:
            result = await self.ledger_client.add_strategy_withdraw(
                wallet_address=wallet_address,
                chain_id=chain_id,
                token_address=token_address,
                token_amount=token_amount,
                usd_value=usd_value,
                data=data,
                strategy_name=strategy_name,
            )
            return (True, result)
        except Exception as e:
            self.logger.error(f"Error recording withdrawal: {e}")
            return (False, str(e))

    async def record_operation(
        self,
        wallet_address: str,
        operation_data: Operation,
        usd_value: str | float,
        strategy_name: str | None = None,
    ) -> tuple[bool, TransactionRecord | str]:
        try:
            op_dict = operation_data.model_dump(mode="json")
            result = await self.ledger_client.add_strategy_operation(
                wallet_address=wallet_address,
                operation_data=op_dict,
                usd_value=usd_value,
                strategy_name=strategy_name,
            )
            return (True, result)
        except Exception as e:
            self.logger.error(f"Error recording operation: {e}")
            return (False, str(e))

    async def get_transaction_summary(
        self, wallet_address: str, limit: int = 10
    ) -> tuple[bool, Any]:
        try:
            success, transactions_data = await self.get_strategy_transactions(
                wallet_address=wallet_address, limit=limit
            )

            if not success:
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
            self.logger.error(f"Error creating transaction summary: {e}")
            return (False, str(e))

    async def record_strategy_snapshot(
        self, wallet_address: str, strategy_status: StatusDict
    ) -> tuple[bool, None | str]:
        try:
            await self.ledger_client.strategy_snapshot(
                wallet_address=wallet_address,
                strat_portfolio_value=strategy_status["portfolio_value"],
                net_deposit=strategy_status["net_deposit"],
                strategy_status=strategy_status["strategy_status"],
                gas_available=strategy_status["gas_available"],
                gassed_up=strategy_status["gassed_up"],
            )
            return (True, None)
        except Exception as e:
            self.logger.error(f"Error recording strategy snapshot: {e}")
            return (False, str(e))
