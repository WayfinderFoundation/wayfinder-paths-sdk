# Balance Adapter

Adapter for wallet and token balances with cross-wallet transfer capabilities.

- **Type**: `BALANCE`
- **Module**: `wayfinder_paths.adapters.balance_adapter.adapter.BalanceAdapter`

## Overview

The BalanceAdapter provides:
- Token balance queries for any wallet
- Cross-wallet transfers between main and strategy wallets
- Automatic ledger recording for deposits/withdrawals

## Usage

```python
from wayfinder_paths.adapters.balance_adapter.adapter import BalanceAdapter
from wayfinder_paths.mcp.scripting import get_adapter

adapter = get_adapter(BalanceAdapter, "main", "my_strategy")
```

## Methods

### get_balance

Get token balance for a wallet.

```python
success, balance = await adapter.get_balance(
    token_id="usd-coin-base",
    wallet_address="0x...",
    chain_id=8453,  # optional, auto-resolved from token
)
```

**Returns**: `(bool, int)` - success flag and raw balance (in token units)

### get_balance_details

Get token balance for a wallet, including decimals and a human-readable balance.

```python
ok, data = await adapter.get_balance_details(
    token_id="usd-coin-base",
    wallet_address="0x...",
)
```

**Returns**: `(bool, dict)` with keys like `balance_raw`, `decimals`, and `balance_decimal`.

**API behavior**:
- If `token_id` is a human slug (e.g. `usd-coin-base`), the adapter resolves it via `TokenClient` first.
- If `token_id` already encodes chain+address (e.g. `base_0x...` or `0x..._base`), it skips the API and goes straight to RPC.

### move_from_main_wallet_to_strategy_wallet

Transfer tokens from main wallet to strategy wallet with ledger recording.

```python
success, tx_hash = await adapter.move_from_main_wallet_to_strategy_wallet(
    token_id="usd-coin-base",
    amount=100.0,  # human-readable amount
    strategy_name="my_strategy",
    skip_ledger=False,
)
```

### move_from_strategy_wallet_to_main_wallet

Transfer tokens from strategy wallet back to main wallet.

```python
success, tx_hash = await adapter.move_from_strategy_wallet_to_main_wallet(
    token_id="usd-coin-base",
    amount=50.0,
    strategy_name="my_strategy",
    skip_ledger=False,
)
```

### send_to_address

Send tokens to an arbitrary address (e.g., bridge contract).

```python
success, tx_hash = await adapter.send_to_address(
    token_id="usd-coin-base",
    amount=1000000,  # raw amount
    from_wallet=strategy_wallet_address,
    to_address="0xBridgeContract...",
    signing_callback=strategy_sign_callback,
)
```

## Configuration

The adapter takes wallet addresses and signing callbacks directly (no config lookup):

```python
adapter = BalanceAdapter(
    config=config,
    main_sign_callback=main_cb,
    strategy_sign_callback=strategy_cb,
    main_wallet_address="0x...",
    strategy_wallet_address="0x...",
)
```

Or use `get_adapter()` which wires everything automatically from wallet labels.

## Dependencies

- `TokenClient` - For token metadata
- `LedgerAdapter` - For transaction recording
- `TokenAdapter` - For price lookups

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/balance_adapter/ -v
```
