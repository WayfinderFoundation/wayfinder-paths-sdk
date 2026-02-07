# Simulation / Dry-Run Guide (Gorlami forks)

When writing or changing **fund-moving** scripts/strategies, run at least one **scenario** on a fork first (dry run) before broadcasting to a real RPC. This catches unit issues, allowance bugs, calldata shape changes, and multi-step sequencing problems without risking real funds.

Think of these dry-runs as **virtual testnets**: a forked chain state where you can execute **sequential operations** (approve → swap → lend → borrow → repay, etc.) and each step updates on-chain state for the next step—without touching real funds.

This repo supports dry-runs via **Gorlami** (Wayfinder’s virtual testnet service) by forking a chain and temporarily routing RPC traffic to the fork.

## Configure Gorlami

Add these fields to `config.json`:

```json
{
  "system": {
    "gorlami_base_url": "https://app.wayfinder.ai/gorlami/api/v1/gornet",
    "gorlami_api_key": "gorlami_..."
  }
}
```

Notes:
- Gorlami uses an `Authorization: <api_key>` header (raw key; not `Bearer ...`).
- `config.json` is gitignored; do not commit keys.

## Dry-run a strategy (preferred)

Use `wayfinder_paths/run_strategy.py` with `--gorlami`:

```bash
# Status on a Base fork
poetry run python wayfinder_paths/run_strategy.py moonwell_wsteth_loop_strategy \
  --action status \
  --gorlami \
  --config config.json

# Deposit + update on a fork (seed balances automatically)
poetry run python wayfinder_paths/run_strategy.py moonwell_wsteth_loop_strategy \
  --action deposit \
  --main-token-amount 20 \
  --gorlami \
  --config config.json

poetry run python wayfinder_paths/run_strategy.py moonwell_wsteth_loop_strategy \
  --action update \
  --gorlami \
  --config config.json
```

Fork funding options:
- By default, `--gorlami` seeds **0.1 ETH** to `main_wallet` and `strategy_wallet` (unless you pass `--gorlami-no-default-gas`).
- For `deposit`, if you don’t pass `--gorlami-fund-erc20`, the runner will best-effort seed the deposit token to the main wallet based on `--main-token-amount`.

Manual seeding flags:

```bash
# Seed native ETH: ADDRESS:ETH
--gorlami-fund-native-eth 0xabc...:0.5

# Seed ERC20: TOKEN:WALLET:AMOUNT:DECIMALS (AMOUNT is human units)
--gorlami-fund-erc20 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913:0xabc...:10:6
```

If chain inference fails, pass `--gorlami-chain-id <id>`.

## Dry-run a script (recommended pattern)

Use `wayfinder_paths.core.utils.gorlami.gorlami_fork()` to create a fork, seed balances, and temporarily route `web3_from_chain_id(...)` to the fork:

- Context manager: `wayfinder_paths/core/utils/gorlami.py`
- Example scripts:
  - `scripts/brap_swap.py` (supports `--gorlami`, and requires `--confirm-live` for real broadcast)
  - `scripts/moonwell_dry_run.py` (Moonwell `deposit -> update` on a Base fork)

For new scripts, prefer this safety pattern:
- Default to `--gorlami` (dry run).
- Require an explicit `--confirm-live` flag to broadcast to a real RPC.
- After each step, verify **receipt status** and at least one **state assertion** (balances/position changed as expected).

## Scenario testing checklist (before “live”)

For a complex strategy or multi-step script:
1. Run read-only calls first (`status`, `quote`, `analyze`) to validate inputs and unit conversions.
2. Run one “happy path” scenario on a fork with seeded balances.
3. Add at least one failure scenario (insufficient balance, allowance missing, slippage too tight) and confirm errors are handled cleanly.
4. Only after the fork run is clean, broadcast live with **small size** and explicit confirmation.

## Known fork-mode gotchas

- Fork RPCs can intermittently return 502/503/504; the SDK retries some RPC calls.
- `eth_estimateGas` can fail for complex multicalls on forks; the SDK falls back to a generous gas limit in fork mode.
- Fork runs default to **0 confirmations** (don’t wait for extra blocks that may never arrive on a fork).
