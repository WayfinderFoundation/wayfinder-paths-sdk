# AvantisAdapter

Adapter for the Avantis **avUSDC** ERC-4626 LP vault on **Base**.

- **Type**: `AVANTIS`
- **Module**: `wayfinder_paths.adapters.avantis_adapter.adapter.AvantisAdapter`

## Overview

Maps Wayfinder-style lending primitives onto ERC-4626:

- `lend()` → `deposit(assets, receiver)` (USDC → avUSDC shares)
- `unlend()` → `redeem(shares, receiver, owner)` (avUSDC shares → USDC)

`borrow()` / `repay()` are intentionally unsupported for this vault.

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/avantis_adapter/ -v
```

