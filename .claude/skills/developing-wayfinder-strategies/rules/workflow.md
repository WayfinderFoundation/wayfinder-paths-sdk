# Workflow (Wayfinder Paths)

## Setup loop (fast, repeatable)

1) Install deps: `poetry install`
2) Generate local wallets (required for many tests/strategies): `just create-wallets`
3) Validate manifests: `just validate-manifests`
4) Run smoke tests: `just test-smoke`

## Run strategies locally

Preferred entrypoint: `wayfinder_paths/run_strategy.py`

Examples:
- Status: `poetry run python wayfinder_paths/run_strategy.py stablecoin_yield_strategy --action status --config config.json`
- Update once: `poetry run python wayfinder_paths/run_strategy.py stablecoin_yield_strategy --action update --config config.json`

Notes:
- `config.json` is gitignored; treat it as local runtime state.
- Strategy wallet lookup is label-based: `config.json.wallets[*].label == <strategy directory name>`.

## Scenario testing (dry-run) before live

For complex fund-moving flows (multi-step swaps, lending loops), run at least one **forked dry-run scenario** first:
- Use `--gorlami` on `wayfinder_paths/run_strategy.py`
- See `SIMULATION.md` and load `/simulation-dry-run`

## One-off execution (Claude Code)

If the user wants **immediate execution** (not a reusable strategy):
- For simple on-chain sends/swaps: use `mcp__wayfinder__execute`.
- For Hyperliquid perp orders/leverage: use `mcp__wayfinder__hyperliquid_execute`.
- Write a short script under `.wayfinder_runs/` (gitignored).
- Prefer running it via `mcp__wayfinder__run_script` so Claude Code shows a review prompt.

## “Explore first” approach

When exploring an unfamiliar adapter/strategy:
- Start from its `manifest.yaml` (capabilities, entrypoint, dependencies).
- Read its `examples.json` (expected inputs and runtime assumptions).
- Prefer read-only calls first; only move to execution after validating inputs/units.
