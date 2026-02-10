# Simulation / Dry-Run Guide (Gorlami forks)

When writing or changing **fund-moving** scripts/strategies, run at least one **scenario** on a fork first (dry run) before broadcasting to a real RPC. This catches unit issues, allowance bugs, calldata shape changes, and multi-step sequencing problems without risking real funds.

Think of these dry-runs as **virtual testnets**: a forked chain state where you can execute **sequential operations** (approve → swap → lend → borrow → repay, etc.) and each step updates on-chain state for the next step—without touching real funds.

This repo supports dry-runs via **Gorlami** (Wayfinder’s virtual testnet service) by forking a chain and temporarily routing RPC traffic to the fork.

## Configure Gorlami

Gorlami is proxied through the Wayfinder API at `strategies.wayfinder.ai/gorlami/`. Authentication uses your existing Wayfinder API key (`system.api_key` in `config.json`).

No additional configuration is needed — if your `api_key` is set, dry-runs work out of the box. To override the default Gorlami URL, add `gorlami_base_url` to `config.json`:

```json
{
  "system": {
    "api_key": "wk_...",
    "gorlami_base_url": "https://strategies.wayfinder.ai/api/v1/blockchain/gorlami"
  }
}
```

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

## Cross-chain simulation

Gorlami forks a **single chain** at a time, but cross-chain flows (e.g. bridge USDC from Arbitrum → BSC, then swap on BSC) can be simulated by spinning up **multiple forks** and manually amending balances on the destination fork to represent the bridge delivery.

**Pattern: simulate bridge + destination activity**

1. Create a fork for the **source chain** (e.g. Arbitrum).
2. Create a fork for the **destination chain** (e.g. BSC).
3. Execute the bridge/send transaction on the source fork — verify it succeeds (receipt `status=1`).
4. If it succeeds, **seed the expected tokens** on the destination fork using `set_erc20_balance` (simulating what the bridge would deliver).
5. Continue executing destination chain operations (swaps, lends, etc.) on the destination fork.

This works because each `gorlami_fork()` context manager overrides `web3_from_chain_id(chain_id)` for its chain, so you can have multiple forks active simultaneously by nesting or managing them manually.

**Script example (bridge Arbitrum USDC → BSC, then swap on BSC):**

```python
from wayfinder_paths.core.utils.gorlami import gorlami_fork
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

WALLET = "0xYourWallet"
USDC_ARB = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"  # USDC on Arbitrum
USDC_BSC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"  # USDC on BSC
BRIDGE_AMOUNT_WEI = 10 * 10**6  # 10 USDC

async def run():
    # Fork both chains
    async with gorlami_fork(
        42161,  # Arbitrum
        native_balances={WALLET: 10**17},  # 0.1 ETH for gas
        erc20_balances=[(USDC_ARB, WALLET, BRIDGE_AMOUNT_WEI)],
    ) as (arb_client, arb_info):

        # Step 1: Execute bridge tx on Arbitrum fork
        # ... (your bridge call here — verify receipt status=1)

        # Step 2: Fork BSC and seed the "bridged" tokens
        async with gorlami_fork(
            56,  # BSC
            native_balances={WALLET: 10**17},  # 0.1 BNB for gas
            erc20_balances=[(USDC_BSC, WALLET, BRIDGE_AMOUNT_WEI)],
        ) as (bsc_client, bsc_info):

            # Step 3: Execute destination chain activity on BSC fork
            # ... (swap USDC → target token, etc.)
            pass
```

**Key point:** The bridge relayer doesn't run between forks — you manually seed what the bridge *would* deliver. This validates the source chain tx and the destination chain activity independently, which catches most real bugs (approval issues, calldata, slippage, gas).

## Known fork-mode gotchas

- Fork RPCs can intermittently return 502/503/504; the SDK retries some RPC calls.
- `eth_estimateGas` can fail for complex multicalls on forks; the SDK falls back to a generous gas limit in fork mode.
- Fork runs default to **0 confirmations** (don't wait for extra blocks that may never arrive on a fork).
- **Cross-chain bridges:** The bridge relayer does not operate between forks. Seed destination balances manually after verifying the source tx succeeds (see "Cross-chain simulation" above).
