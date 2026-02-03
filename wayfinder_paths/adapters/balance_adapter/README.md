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

adapter = BalanceAdapter(
    config=config,
    main_wallet_signing_callback=main_signing_cb,
    strategy_wallet_signing_callback=strategy_signing_cb,
)
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
    from_wallet=config["strategy_wallet"],
    to_address="0xBridgeContract...",
    signing_callback=strategy_signing_cb,
)
```

## Configuration

The adapter requires wallet configuration in `config`:

```python
config = {
    "main_wallet": {"address": "0x..."},
    "strategy_wallet": {"address": "0x..."},
}
```

## Dependencies

- `TokenClient` - For token metadata
- `LedgerAdapter` - For transaction recording
- `TokenAdapter` - For price lookups

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/balance_adapter/ -v
```
