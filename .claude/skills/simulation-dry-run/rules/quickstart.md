# Quickstart: scenario testing on a fork (Gorlami)

## 1) Prerequisites

Gorlami is proxied through the Wayfinder API. If your `system.api_key` is set in `config.json`, dry-runs work out of the box — no extra config needed.

## 2) Run a strategy on a fork

Preferred entrypoint: `wayfinder_paths/run_strategy.py` with `--gorlami`.

This runs against a **virtual testnet fork** where each transaction updates state for the next step (sequential operations).

```bash
poetry run python wayfinder_paths/run_strategy.py moonwell_wsteth_loop_strategy \
  --action status \
  --gorlami \
  --config config.json
```

For deposit flows:

```bash
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

Fork funding controls (optional):
- By default, `--gorlami` seeds **0.1 ETH** to `main_wallet` and `strategy_wallet` (unless you pass `--gorlami-no-default-gas`).
- For `deposit`, the runner will best-effort seed the deposit token based on `--main-token-amount`.
- `--gorlami-fund-native-eth ADDRESS:ETH` — seed native gas manually
- `--gorlami-fund-erc20 TOKEN:WALLET:AMOUNT:DECIMALS` — seed ERC20 (AMOUNT in human units)
- `--gorlami-no-default-gas` — disable default ETH seeding
- If chain inference fails, pass `--gorlami-chain-id <id>`.

## 3) Run a script on a fork

Use `gorlami_fork()` from `wayfinder_paths/core/utils/gorlami.py` — it creates a fork, seeds balances, and temporarily routes `web3_from_chain_id(...)` to the fork.

Examples in this repo:
- `scripts/moonwell_dry_run.py` (Moonwell `deposit -> update` on a Base fork)

## 4) Cross-chain simulation

Gorlami forks a **single chain** at a time, but cross-chain flows can be simulated by spinning up **multiple forks** and manually seeding balances on the destination fork to represent bridge delivery.

**Pattern:**

1. Fork the **source chain** (e.g. Arbitrum).
2. Fork the **destination chain** (e.g. BSC).
3. Execute the bridge tx on the source fork — verify receipt `status=1`.
4. Seed the expected tokens on the destination fork using `set_erc20_balance`.
5. Continue executing on the destination fork.

Each `gorlami_fork()` overrides `web3_from_chain_id` for its chain only, so you can nest them.

**Example:**

```python
from wayfinder_paths.core.utils.gorlami import gorlami_fork

WALLET = "0xYourWallet"
USDC_ARB = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
USDC_BSC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"
BRIDGE_AMOUNT_WEI = 10 * 10**6  # 10 USDC

async def run():
    async with gorlami_fork(
        42161,  # Arbitrum
        native_balances={WALLET: 10**17},
        erc20_balances=[(USDC_ARB, WALLET, BRIDGE_AMOUNT_WEI)],
    ) as (arb_client, arb_info):
        # Execute bridge tx on Arbitrum fork — verify receipt status=1

        async with gorlami_fork(
            56,  # BSC
            native_balances={WALLET: 10**17},
            erc20_balances=[(USDC_BSC, WALLET, BRIDGE_AMOUNT_WEI)],
        ) as (bsc_client, bsc_info):
            # Execute destination chain activity on BSC fork
            pass
```

The bridge relayer doesn't run between forks — you manually seed what the bridge *would* deliver.
