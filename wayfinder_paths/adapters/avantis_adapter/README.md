# AvantisAdapter

Adapter for the Avantis **avUSDC** ERC-4626 LP vault on **Base**.

- **Type**: `AVANTIS`
- **Module**: `wayfinder_paths.adapters.avantis_adapter.adapter.AvantisAdapter`

## Overview

ERC-4626 vault adapter:

- `get_all_markets()` — reads vault TVL/share price and returns raw plus normalized `*_usdc` fields
- `fetch_trailing_apy()` — reads Avantis trailing APY over the API-provided window; it is not a guaranteed forward APY
- `deposit(amount)` — calls ERC-4626 `deposit(assets, receiver)` (USDC → avUSDC shares)
- `withdraw(amount)` — calls ERC-4626 `redeem(shares, receiver, owner)` (avUSDC shares → USDC)

`borrow()` / `repay()` are intentionally unsupported (LP vault, not a lending market).

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/avantis_adapter/ -v
```
