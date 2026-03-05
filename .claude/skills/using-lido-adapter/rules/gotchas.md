# Lido gotchas

## Chain support

- Only Ethereum mainnet is supported (`chain_id=1`). The adapter will error on other `chain_id`s.

## `stake_eth(...)` limits + return shapes

- By default, `stake_eth(..., check_limits=True)` checks:
  - whether staking is paused, and
  - the current per-tx stake limit.
- `receive="wstETH"` is a **two-transaction** flow: submit ETH → mint `stETH`, then approve + wrap into `wstETH`.
- Return shapes:
  - `receive="stETH"`: returns a tx hash string.
  - `receive="wstETH"`: returns a dict with `stake_tx` and (usually) `wrap_tx`.

## Withdrawals are async

- `request_withdrawal(...)` transfers `stETH`/`wstETH` into the WithdrawalQueue and mints an `unstETH` NFT.
- `claim_withdrawals(...)` only works once requests are finalized.

## Withdrawal splitting (min/max chunk size)

- `request_withdrawal(...)` splits `amount_wei` into chunks to satisfy WithdrawalQueue constraints:
  - `WITHDRAWAL_MIN_WEI = 100`
  - `WITHDRAWAL_MAX_WEI = 1000 * 1e18`
- If you request a very large withdrawal, you’ll see multiple chunk amounts in the returned payload.

## Units + decimals

- All amounts are raw wei integers.
- `include_usd=True` in `get_full_user_state(...)` is best-effort (depends on token pricing availability).

