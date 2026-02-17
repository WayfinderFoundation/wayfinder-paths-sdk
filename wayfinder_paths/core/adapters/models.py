from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class OperationBase(BaseModel):
    # These are provided by adapters at execution time, but tests and callers may
    # construct operations without them (e.g., for bookkeeping payloads).
    adapter: str = "unknown"
    transaction_hash: str | None = None
    transaction_chain_id: int | None = None


class SWAP(OperationBase):
    type: Literal["SWAP"] = "SWAP"
    from_token_id: str
    to_token_id: str
    from_amount: str
    to_amount: str
    from_amount_usd: float
    to_amount_usd: float
    transaction_status: str | None = None
    transaction_receipt: dict[str, Any] | None = None


class LEND(OperationBase):
    type: Literal["LEND"] = "LEND"
    token_address: str
    pool_address: str
    amount: str
    amount_usd: float
    transaction_status: str | None = None
    transaction_receipt: dict[str, Any] | None = None


class UNLEND(OperationBase):
    type: Literal["UNLEND"] = "UNLEND"
    token_address: str
    pool_address: str
    amount: str
    amount_usd: float
    transaction_status: str | None = None
    transaction_receipt: dict[str, Any] | None = None


Operation = SWAP | LEND | UNLEND


class STRAT_OP(BaseModel):
    op_data: Annotated[Operation, Field(discriminator="type")]


class FeeEstimation(BaseModel):
    fee_total_usd: float | None = None
    fee_breakdown: list[Any] = []


class EvmTxn(BaseModel):
    txn_type: Literal["evm"] = "evm"
    txn: dict[str, Any]
    gas_estimate: int
    fee_estimate: FeeEstimation = FeeEstimation()
    chain_id: int
    quote: dict[str, Any] | None = None
