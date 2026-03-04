# Avantis gotchas

## Base-only + single-vault scope

This adapter is Base-only (`chain_id=8453`) and defaults to the avUSDC vault:
- Vault: `AVANTIS_AVUSDC` (share token)
- Underlying: `BASE_USDC`
- Manager: `AVANTIS_VAULT_MANAGER`

Prefer importing these from `wayfinder_paths/core/constants/contracts.py` instead of hard-coding addresses.

## This is an ERC-4626 vault adapter (not perps/trading)

This adapter only covers the avUSDC ERC-4626 vault + VaultManager reads and deposit/redeem execution. It does not implement Avantis perps/trading features.

## ERC-4626 units: deposit takes assets, withdraw takes shares

- `deposit(amount=...)` calls `deposit(assets, receiver)` where `assets` is **USDC base units**.
- `withdraw(amount=...)` calls `redeem(shares, receiver, owner)` where `shares` is **avUSDC base units**.

If you want to “withdraw X USDC”, you must first convert assets→shares (e.g., via ERC-4626 `previewWithdraw` / `convertToShares`), which this adapter does not currently expose.

## Use `get_pos()` before redeeming

`get_pos()` returns:
- `max_redeem` (max shares you can redeem)
- `max_withdraw` (max assets you can withdraw)

Use these to avoid reverts and to understand whether you should redeem shares vs target an asset amount.

## `redeem_full=True` can return a non-tx message

If the wallet has no shares, `withdraw(..., redeem_full=True)` returns:
- `ok=True, "no shares to redeem"`

Handle this as a successful no-op.

## Deposits may require an approval tx

`deposit(...)` calls `ensure_allowance(...)` with `approval_amount=MAX_UINT256`, so the first deposit for a wallet may produce a separate ERC20 approval transaction before the actual deposit.

## No borrow/repay (not a lending market)

`borrow()` and `repay()` are intentionally unsupported for this LP vault:
- they return `ok=False` with an explanatory message

## USD values may be unavailable

`include_usd=True` uses `TOKEN_CLIENT` price lookups. If pricing is missing/unavailable, USD fields can be `None`.
