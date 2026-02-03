# Ledger Adapter

Adapter for strategy transaction recording and bookkeeping.

- **Type**: `LEDGER`
- **Module**: `wayfinder_paths.adapters.ledger_adapter.adapter.LedgerAdapter`

## Overview

The LedgerAdapter provides:
- Transaction history tracking
- Net deposit calculations
- Deposit/withdrawal recording
- Strategy operation logging

## Usage

```python
from wayfinder_paths.adapters.ledger_adapter.adapter import LedgerAdapter

adapter = LedgerAdapter()
```

## Methods

### get_strategy_transactions

Get transaction history for a strategy wallet.

```python
success, data = await adapter.get_strategy_transactions(
    wallet_address="0x...",
    limit=10,
    offset=0,
)
if success:
    transactions = data.get("transactions", [])
```

### get_strategy_net_deposit

Get the net deposit amount for a strategy.

```python
success, data = await adapter.get_strategy_net_deposit(
    wallet_address="0x..."
)
if success:
    net_deposit = float(data) if data is not None else 0
    print(f"Net deposit: {net_deposit} USDC")
else:
    print(f"Error: {data}")
```

### record_deposit

Record a deposit transaction.

```python
success, data = await adapter.record_deposit(
    wallet_address="0x...",
    chain_id=8453,
    token_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    token_amount="1000000000",
    usd_value="1000.00",
    strategy_name="my_strategy",
)
```

### record_withdrawal

Record a withdrawal transaction.

```python
success, data = await adapter.record_withdrawal(
    wallet_address="0x...",
    chain_id=8453,
    token_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    token_amount="500000000",
    usd_value="500.00",
    strategy_name="my_strategy",
)
```

### record_operation

Record a strategy operation (swap, rebalance, etc.).

```python
from wayfinder_paths.adapters.ledger_adapter.models import SWAP

operation = SWAP(
    from_token_id="0x...",
    to_token_id="0x...",
    from_amount="1000000000",
    to_amount="995000000",
    from_amount_usd=1000.0,
    to_amount_usd=995.0,
)

success, data = await adapter.record_operation(
    wallet_address="0x...",
    operation_data=operation,
    usd_value="1000.00",
    strategy_name="my_strategy",
)
```

### record_strategy_snapshot

Record a strategy status snapshot.

```python
success, data = await adapter.record_strategy_snapshot(
    wallet_address="0x...",
    strategy_status={"portfolio_value": 1000.0, ...},
)
```

## Dependencies

- `LedgerClient` - Low-level API client

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/ledger_adapter/ -v
```
