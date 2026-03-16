# Execution Modes

## Running strategies via MCP

When a user asks to run, check, or interact with a strategy:

1. **Always discover first** - Use MCP resource `wayfinder://strategies` to list available strategies before attempting to run one. Strategy names use `snake_case` (e.g., `boros_hype_strategy`, not `hype_boros_strategy`).

2. **Standard strategy interface** - All strategies implement these actions via `mcp__wayfinder__run_strategy`:

   **Read-only actions (no confirmation):**
   - `status` - Current positions, balances, and state
   - `analyze` - Run strategy analysis with given deposit amount
   - `snapshot` - Build batch snapshot for scoring
   - `policy` - Get strategy policies
   - `quote` - Get point-in-time expected APY for the strategy

   **Fund-moving actions (require safety review):**
   - `deposit` - Add funds to the strategy (requires `main_token_amount`; optional `gas_token_amount`). **First deposit?** Always include `gas_token_amount` (e.g. `0.001`) — the strategy wallet starts with no gas.
   - `update` - Rebalance or execute the strategy logic
   - `withdraw` - **Liquidate**: Close all positions and convert to stablecoins (funds stay in strategy wallet)
   - `exit` - **Transfer**: Move funds from strategy wallet to main wallet (call after withdraw)

3. **Workflow examples**:

   ```
   # User: "check the boros strategy"
   → ReadMcpResourceTool(server="wayfinder", uri="wayfinder://strategies")  # Find exact name
   → run_strategy(strategy="boros_hype_strategy", action="status")

   # User: "what's the expected APY for the moonwell strategy?"
   → run_strategy(strategy="moonwell_wsteth_loop_strategy", action="quote")

   # User: "withdraw from the strategy"
   → run_strategy(strategy="boros_hype_strategy", action="withdraw")
   # Triggers safety review: "Withdraw from boros_hype_strategy"

   # User: "deposit $100 into the strategy"
   → run_strategy(strategy="boros_hype_strategy", action="deposit", main_token_amount=100.0, gas_token_amount=0.01)
   ```

4. **Don't guess strategy names** - If the user's name doesn't match exactly, use `wayfinder://strategies` to find the correct name.

5. **Clarify withdraw vs exit** - These are separate steps:
   - `withdraw` - **Liquidate**: Closes all positions and converts to stablecoins (funds stay in strategy wallet)
   - `exit` - **Transfer**: Moves funds from strategy wallet to main wallet

   **Typical full exit flow**: `withdraw` first (closes positions), then `exit` (transfers to main).
   When a user says "withdraw all" or "close everything", run `withdraw` then `exit`.
   When a user says "transfer remaining funds" (positions already closed), just use `exit`.

6. **Safety review** - Fund-moving actions (deposit, update, withdraw, exit) are gated by a safety review hook that shows a preview and asks for confirmation.

7. **Mypy typing** - When adding or modifying Python code, ensure all _new/changed_ code is fully type-annotated and does not introduce new mypy errors (existing legacy errors may remain).

## Execution modes (one-off vs recurring)

When a user wants **immediate, one-off execution**:

- **Gas check first:** Before any on-chain execution, verify the wallet has native gas on the target chain (see "Gas requirements" under Supported chains). If bridging to a new chain, bridge once and swap locally — don't do two separate bridges.
- **On-chain:** use `mcp__wayfinder__execute` (swap/send). The `amount` parameter is **human-readable** (e.g. `"5"` for 5 USDC), not wei.
- **Hyperliquid perps/spot:** use `mcp__wayfinder__hyperliquid_execute` (market/limit, leverage, cancel). **Before your first `hyperliquid_execute` call in a session, invoke `/using-hyperliquid-adapter`** to load the MCP tool's required-parameter rules (`is_spot`, `leverage`, `usd_amount_kind`, etc.). The skill covers both the MCP tool interface and the Python adapter.
- **Polymarket:** use `mcp__wayfinder__polymarket` (search/status/history) + `mcp__wayfinder__polymarket_execute` (bridge USDC↔USDC.e, buy/sell, limit orders, redeem). **Before your first Polymarket execution call in a session, invoke `/using-polymarket-adapter`** (USDC.e collateral + tradability filters + outcome selection).
- **Multi-step flows:** write a short Python script under `.wayfinder_runs/.scratch/<session_id>/` (see `$WAYFINDER_SCRATCH_DIR`) and execute it with `mcp__wayfinder__run_script`. Promote keepers into `.wayfinder_runs/library/<protocol>/` (see `$WAYFINDER_LIBRARY_DIR`).

### Complex transaction flow (multi-step or fund-moving)

For anything beyond a simple single swap, follow this checklist:

1. **Plan** — Break the transaction into ordered steps. Identify which chains, protocols, and tokens are involved. State the plan to the user before writing any code.
2. **Gather info** — Load the relevant protocol skill(s). Fetch current rates, balances, gas, and any addresses or parameters the script needs. Don't hardcode values you haven't verified.
3. **Quote all steps** — For every swap/bridge step, call `mcp__wayfinder__quote_swap` and collect the results. Then display a confirmation table to the user before executing anything:

   | Step | From | To | Est. Output | Fee (USD) | Route |
   |------|------|----|-------------|-----------|-------|
   | 1    | ...  | .. | ...         | ...       | ...   |

   Wait for explicit user confirmation before proceeding. Skip this only if the user has explicitly said to (e.g. "just execute").

4. **Script** — Write the script under `$WAYFINDER_SCRATCH_DIR`. Use `get_adapter()` and the patterns from the loaded skill.
5. **Offer simulation** — Use Gorlami forks for **EVM on-chain steps only**. Off-chain protocols (Hyperliquid L1, CEXes) are live-only.
6. **Execute** — Run the script (or simulate first if requested). Check each step's result before proceeding to the next — don't continue past a failed/reverted transaction.

Hyperliquid minimums:

- **Minimum deposit: $5 USD** (deposits below this are **lost**)
- **Minimum order: $10 USD notional** (applies to both perp and spot)

HIP-3 dex abstraction + Hyperliquid deposits/withdrawals: handled in the Hyperliquid adapter/tooling — load `/using-hyperliquid-adapter` when scripting.

Polymarket quick flows:

- Search markets/events: `mcp__wayfinder__polymarket(action="search", query="bitcoin february 9", limit=10)`
- Full status (positions + PnL + balances + open orders): `mcp__wayfinder__polymarket(action="status", wallet_label="main")`
- Convert **native Polygon USDC (0x3c499c...) → USDC.e (0x2791..., required collateral)**: `mcp__wayfinder__polymarket_execute(action="bridge_deposit", wallet_label="main", amount=10)` (skip if you already have USDC.e)
- Buy shares (market order): `mcp__wayfinder__polymarket_execute(action="buy", wallet_label="main", market_slug="bitcoin-above-70k-on-february-9", outcome="YES", amount_usdc=2)`
- Close a position (sell full size): `mcp__wayfinder__polymarket_execute(action="close_position", wallet_label="main", market_slug="bitcoin-above-70k-on-february-9", outcome="YES")`
- Redeem after resolution: `mcp__wayfinder__polymarket_execute(action="redeem_positions", wallet_label="main", condition_id="0x...")`

Polymarket funding (USDC.e collateral):

- **Polygon USDC → USDC.e:** `polymarket_execute(action="bridge_deposit", amount=10)` converts native USDC (0x3c499c...) → USDC.e (0x2791...).
- **Already have USDC.e:** Trade immediately, skip `bridge_deposit`.
- **Funds on other chains:** BRAP swap to USDC.e: `execute(kind="swap", from_token="usd-coin-base", to_token="polygon_0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")`. Or use `bridge_deposit` with `from_chain_id` + `from_token_address` (see `PolymarketAdapter.bridge_supported_assets()`).

Sizing note (avoid ambiguity): if a user says "$X at Y× leverage", confirm whether `$X`is **notional** or **margin** (use`usd_amount_kind="notional"|"margin"`on`mcp__wayfinder__hyperliquid_execute`).

### Scripting helper for adapters

**Before writing any adapter script**, invoke the matching protocol skill (e.g. `/using-pendle-adapter`, `/using-hyperliquid-adapter`). Skills document method signatures, return shapes, and field names — guessing wastes iterations. See the protocol skills table in the root CLAUDE.md.

When writing scripts under `.wayfinder_runs/`, use `get_adapter()` to simplify setup:

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

# Single-wallet adapter (sign_callback + wallet_address)
adapter = get_adapter(MoonwellAdapter, "main")
await adapter.set_collateral(mtoken=USDC_MTOKEN)

# Dual-wallet adapter (main + strategy, e.g. BalanceAdapter)
from wayfinder_paths.adapters.balance_adapter import BalanceAdapter
adapter = get_adapter(BalanceAdapter, "main", "my_strategy")

# Read-only (no wallet needed)
adapter = get_adapter(PendleAdapter)
```

`get_adapter()` auto-loads `config.json`, looks up wallets by label, creates signing callbacks, and wires them into the adapter constructor. It introspects the adapter's `__init__` signature to determine the wiring:

- `sign_callback` + `wallet_address` → single-wallet adapter (most adapters)
- `main_sign_callback` + `strategy_sign_callback` → dual-wallet adapter (BalanceAdapter); requires two wallet labels

For direct Web3 usage in scripts, **do not hardcode RPC URLs**. Use `web3_from_chain_id(chain_id)` from `wayfinder_paths.core.utils.web3` — it's an **async context manager** (see scripting gotchas):

```python
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

async with web3_from_chain_id(8453) as w3:
    balance = await w3.eth.get_balance(addr)
```

It reads RPCs from `strategy.rpc_urls` in your config (defaults to repo-root `config.json`, or override via `WAYFINDER_CONFIG_PATH`). For sync access, use `get_web3s_from_chain_id(chain_id)` instead.

Run scripts with poetry: `poetry run python .wayfinder_runs/my_script.py`

## Recurring / automated execution

When a user wants a **repeatable/automated system** (recurring jobs):

- Create or modify a strategy under `wayfinder_paths/strategies/` and follow the normal manifests/tests workflow.
- Use the project-local runner to call strategy `update` on an interval (no cron needed).

Runner CLI (project-local state in `./.wayfinder/runner/`):

```bash
poetry run wayfinder runner start --detach   # Start daemon
poetry run wayfinder runner ensure            # Idempotent start
poetry run wayfinder runner add-job --name basis-update --type strategy --strategy basis_trading_strategy --action update --interval 600 --config ./config.json
poetry run wayfinder runner status | run-once | pause | resume | delete <job> | stop
```

See `RUNNER_ARCHITECTURE.md`.

Runner MCP tool: `mcp__wayfinder__runner(action=...)`.

Safety note:

- Runner executions are local automation and do **not** go through the Claude safety review prompt. Treat `update/deposit/withdraw/exit` as live fund-moving actions.
