# Script pattern: one codepath, two modes (fork vs live)

## Recommended CLI safety rails

For any script that can broadcast:
- `--gorlami` (dry run on a fork)
- `--confirm-live` (required to broadcast to real RPCs)

Default should be safe:
- Prefer `--gorlami` by default, or refuse to run live unless `--confirm-live` is present.

## Use `gorlami_fork(...)` for simulation

Use the context manager in `wayfinder_paths/core/utils/gorlami.py`:

- Creates a fork for `chain_id`
- Seeds native + ERC20 balances via Gorlami REST endpoints
- Temporarily overrides `strategy.rpc_urls[chain_id]` so normal Web3 code uses the fork
- Deletes the fork on exit

This keeps strategy and adapter code unchanged.

## Post-step verification

After each fund-moving call:
- read balances / positions
- assert the change happened (avoid silent failures)

## Live gating example

If you add a live broadcast mode to a script, keep the default safe and gate live sends behind an explicit `--confirm-live` flag (while preserving `--gorlami` for dry-runs).
