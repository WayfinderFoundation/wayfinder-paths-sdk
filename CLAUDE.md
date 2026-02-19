# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## First-Time Setup (Auto-detect)

**IMPORTANT: On every new conversation, check if setup is needed:**

1. Check if `config.json` exists in the repo root
2. If it does NOT exist, this is a first-time user. You MUST:
   - Tell the user: "Welcome to Wayfinder Paths! Let me set things up for you."
   - Run: `python3 scripts/setup.py`
   - The script may skip the API key prompt in non-interactive terminals - that's OK
   - After setup completes, ask the user: "Do you have a Wayfinder API key?"
     - If yes: Use the Edit tool to add it to `config.json` under `system.api_key`
     - If no: Direct them to **https://strategies.wayfinder.ai** to create an account and get one
   - After config is complete, tell the user: **"Please restart Claude Code to load the MCP server, then we can continue."**

3. If `config.json` exists but `system.api_key` is empty/missing:
   - Ask: "I see you haven't set up your API key yet. Do you have a Wayfinder API key?"
   - If yes: Help them add it to `config.json` under `system.api_key`
   - If no: Direct them to **https://strategies.wayfinder.ai** to get one

4. If everything is configured, proceed normally

**To re-run setup at any time:** User can type `/setup` or ask "run setup"

## Project Overview

Wayfinder Paths is a Python 3.12 public SDK for community-contributed DeFi trading strategies and adapters. It provides the building blocks for automated trading: adapters (exchange/protocol integrations), strategies (trading algorithms), and clients (low-level API wrappers). In production it can be integrated with a separate execution service for hosted signing/execution.

## Claude Code MCP + Skills (project-scoped)

This repo ships:

- A project-scoped MCP server config at `.mcp.json` (Claude Code will prompt to enable it).
- A safety review hook at `.claude/settings.json` that forces confirmation before fund-moving calls.
- Claude Code skills under `.claude/skills/` for strategy development + adapter exploration.
- A local, gitignored runs directory at `.wayfinder_runs/` for one-off “execution mode” scripts.

MCP server entrypoint:

- `poetry run python -m wayfinder_paths.mcp.server`

Simulation / scenario testing (vnet only):

- Before broadcasting complex fund-moving flows live, run at least one forked **dry-run scenario** (Gorlami). These are EVM virtual testnets (vnets) that simulate **sequential on-chain operations** with real EVM state changes. Use `/simulation-dry-run` for full details.
- **Cross-chain:** For flows spanning multiple EVM chains, spin up a fork per chain. Execute the source tx on the source fork, seed the expected tokens on the destination fork (simulating bridge delivery), then continue on the destination fork. See `/simulation-dry-run` for the pattern.
- **Scope:** Vnets only cover EVM chains (Base, Arbitrum, etc.). Off-chain or non-EVM protocols like Hyperliquid **cannot** be simulated — dry-runs only apply to on-chain EVM transactions.

Safety defaults:

- On-chain writes: use MCP `execute(...)` (swap/send). The hook shows a human-readable preview and asks for confirmation.
- Hyperliquid perp writes: use MCP `hyperliquid_execute(...)` (orders/leverage). Also gated by a review prompt.
- Polymarket writes: use MCP `polymarket_execute(...)` (bridge deposit/withdraw, buy/sell, limit orders, redemption). Also gated by a review prompt.
- Contract deploys: use MCP `deploy_contract(...)` (compile + deploy + verify). Also gated by a review prompt. Use `compile_contract(...)` for compilation only (read-only, no confirmation).
- One-off local scripts: use MCP `run_script(...)` (gated by a review prompt) and keep scripts under `.wayfinder_runs/`.

Transaction outcome rules (don’t assume a tx hash means success):

- A transaction is only successful if the on-chain receipt has `status=1`.
- The SDK raises `TransactionRevertedError` when a receipt returns `status=0` (often includes `gasUsed`/`gasLimit` and may indicate out-of-gas).
- If a fund-moving step fails/reverts, stop the flow and report the error; don’t continue executing dependent steps “hoping it worked”.

## Protocol skills (load before using adapters)

Before writing scripts or using adapters for a specific protocol, **invoke the relevant skill** to load usage patterns and gotchas:

| Protocol              | Skill                            |
| --------------------- | -------------------------------- |
| Moonwell              | `/using-moonwell-adapter`        |
| Aave V3               | `/using-aave-v3-adapter`         |
| Morpho                | `/using-morpho-adapter`          |
| Pendle                | `/using-pendle-adapter`          |
| Hyperliquid           | `/using-hyperliquid-adapter`     |
| Hyperlend             | `/using-hyperlend-adapter`       |
| Boros                 | `/using-boros-adapter`           |
| BRAP (swaps)          | `/using-brap-adapter`            |
| Polymarket            | `/using-polymarket-adapter`      |
| CCXT (CEX)            | `/using-ccxt-adapter`            |
| Uniswap (V3)          | `/using-uniswap-adapter`         |
| ProjectX (V3 fork)    | `/using-projectx-adapter`        |
| Pools/Tokens/Balances | `/using-pool-token-balance-data` |
| Simulation / Dry-run  | `/simulation-dry-run`            |
| Contract Dev          | `/contract-development`          |

Skills contain rules for correct method usage, common gotchas, and high-value read patterns. **Always load the skill first** — don't guess at adapter APIs.

Before writing or deploying Solidity contracts, invoke `/contract-development`.

## Data accuracy (no guessing)

When answering questions about **rates/APYs/funding**:

- Never invent or estimate values.
- Always fetch the value via an adapter/client/tool call when possible.
- Before searching external docs, consult this repo's own adapters/clients (and their `manifest.yaml` + `examples.json`) first.
- If you cannot fetch it (auth/network/tooling), say so explicitly and provide the exact call/script needed to fetch it.

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
   - `deposit` - Add funds to the strategy (requires `main_token_amount`; optional `gas_token_amount`)
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

7. **Mypy typing** - When adding or modifying Python code, ensure all *new/changed* code is fully type-annotated and does not introduce new mypy errors (existing legacy errors may remain).

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
3. **Script** — Write the script under `$WAYFINDER_SCRATCH_DIR`. Use `get_adapter()` and the patterns from the loaded skill.
4. **Offer simulation** — Before executing, ask the user if they'd like to dry-run it first. Simulation (Gorlami forks) is also valuable for **iterating on complex scripts** — use it to verify logic, catch reverts, and debug multi-step flows without spending real funds. Available for **EVM on-chain transactions only** (Base, Arbitrum, Ethereum, etc.). **Hyperliquid L1, CEXes, and other off-chain protocols cannot be simulated.** If the flow mixes both (e.g. swap on Base then deposit to Hyperliquid), simulate the on-chain portion and flag the off-chain steps as live-only.
5. **Execute** — Run the script (or simulate first if requested). Check each step's result before proceeding to the next — don't continue past a failed/reverted transaction.

Hyperliquid minimums:

- **Minimum deposit: $5 USD** (deposits below this are **lost**)
- **Minimum order: $10 USD notional** (applies to both perp and spot)

HIP-3 dex abstraction (required for multi-dex trading):

- Trading on HIP-3 dexes (xyz, flx, vntl, hyna, km, etc.) requires **dex abstraction** to be enabled on the user's account.
- The adapter calls `ensure_dex_abstraction(address)` automatically before `place_market_order`, `place_limit_order`, and `place_trigger_order`. It queries the current state via `Info.query_user_dex_abstraction_state(user)` and enables it if needed — this is a one-time on-chain action per account.
- If you're writing a custom script that places orders directly, call `await adapter.ensure_dex_abstraction(address)` before your first order.

Hyperliquid deposits (Bridge2):

- Deposit asset is **USDC on Arbitrum (chain_id 42161)**; deposits are made by transferring Arbitrum USDC to `HYPERLIQUID_BRIDGE_ADDRESS`.
- Deposit flow: `mcp__wayfinder__execute(kind="hyperliquid_deposit", wallet_label="main", amount="8")` → `mcp__wayfinder__hyperliquid(action="wait_for_deposit", expected_increase=...)` (deposit tool hard-codes Arbitrum USDC + bridge address).
- Withdraw flow: `mcp__wayfinder__hyperliquid_execute(action="withdraw", amount_usdc=...)` → `mcp__wayfinder__hyperliquid(action="wait_for_withdrawal")`.

Polymarket quick flows:

- Search markets/events: `mcp__wayfinder__polymarket(action="search", query="bitcoin february 9", limit=10)`
- Full status (positions + PnL + balances + open orders): `mcp__wayfinder__polymarket(action="status", wallet_label="main")`
- Convert **native Polygon USDC (0x3c499c...) → USDC.e (0x2791..., required collateral)**: `mcp__wayfinder__polymarket_execute(action="bridge_deposit", wallet_label="main", amount=10)` (skip if you already have USDC.e)
- Buy shares (market order): `mcp__wayfinder__polymarket_execute(action="buy", wallet_label="main", market_slug="bitcoin-above-70k-on-february-9", outcome="YES", amount_usdc=2)`
- Close a position (sell full size): `mcp__wayfinder__polymarket_execute(action="close_position", wallet_label="main", market_slug="bitcoin-above-70k-on-february-9", outcome="YES")`
- Redeem after resolution: `mcp__wayfinder__polymarket_execute(action="redeem_positions", wallet_label="main", condition_id="0x...")`

Polymarket funding (USDC.e collateral):

- **Have native Polygon USDC (0x3c499c...) on Polygon:** Use `mcp__wayfinder__polymarket_execute(action="bridge_deposit", wallet_label="main", amount=10)` to convert it → USDC.e (0x2791...).
- **Already have USDC.e (0x2791...) on Polygon:** You can trade immediately; skip `bridge_deposit`.
- **No USDC on Polygon (funds on Base, Arbitrum, etc.):** Use `mcp__wayfinder__execute(kind="swap", wallet_label="main", amount="10", from_token="usd-coin-base", to_token="polygon_0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")` to BRAP swap directly to USDC.e.
- **Alternative (bridge service):** `polymarket_execute bridge_deposit` also supports depositing from other EVM chains/tokens via the Polymarket Bridge fallback; pass `from_chain_id` + `from_token_address` (see `PolymarketAdapter.bridge_supported_assets()` for what’s accepted). BRAP is Polygon-only.

Sizing note (avoid ambiguity):

- If a user says "$X at Y× leverage", confirm whether `$X`is **notional** (position size) or **margin** (collateral):`margin ≈ notional / leverage`, `notional = margin \* leverage`.
- `mcp__wayfinder__hyperliquid_execute` supports `usd_amount` with `usd_amount_kind="notional"|"margin"` so this is explicit.

**Scripting helper for adapters:**

**Before writing any adapter script**, invoke the matching protocol skill (e.g. `/using-pendle-adapter`, `/using-hyperliquid-adapter`). Skills document method signatures, return shapes, and field names — guessing wastes iterations. See the protocol skills table above.

When writing scripts under `.wayfinder_runs/`, use `get_adapter()` to simplify setup:

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

adapter = get_adapter(MoonwellAdapter, "main")  # Auto-wires config + signing
await adapter.set_collateral(mtoken=USDC_MTOKEN)
```

This auto-loads `config.json`, looks up the wallet by label, creates a signing callback, and wires everything together. For read-only adapters (e.g., PendleAdapter), omit the wallet label.

For direct Web3 usage in scripts, **do not hardcode RPC URLs**. Use `web3_from_chain_id(chain_id)` from `wayfinder_paths.core.utils.web3` — it's an **async context manager** (see gotchas below):

```python
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

async with web3_from_chain_id(8453) as w3:
    balance = await w3.eth.get_balance(addr)
```

It reads RPCs from `strategy.rpc_urls` in your config (defaults to repo-root `config.json`, or override via `WAYFINDER_CONFIG_PATH`). For sync access, use `get_web3s_from_chain_id(chain_id)` instead.

Run scripts with poetry: `poetry run python .wayfinder_runs/my_script.py`

### Scripting gotchas (`.wayfinder_runs/` scripts)

Common mistakes when writing run scripts. **Read before writing any script.**

**1. `get_adapter()` already loads config — don't call `load_config()` first**

```python
# WRONG — redundant, and load_config() returns None anyway
config = load_config("config.json")
adapter = MoonwellAdapter(config=config, ...)

# RIGHT — get_adapter() handles config + wallet + signing internally
from wayfinder_paths.mcp.scripting import get_adapter
adapter = get_adapter(MoonwellAdapter, "main")

# For read-only adapters, omit the wallet label:
adapter = get_adapter(HyperliquidAdapter)
```

**2. `load_config()` returns `None` — it mutates a global**

```python
# WRONG — config will be None
config = load_config("config.json")
api_key = config["system"]["api_key"]  # TypeError!

# RIGHT — use the CONFIG global, or use load_config_json() for a dict
from wayfinder_paths.core.config import load_config, CONFIG
load_config("config.json")
api_key = CONFIG["system"]["api_key"]

# OR — if you need a plain dict:
from wayfinder_paths.core.config import load_config_json
config = load_config_json("config.json")
```

**3. `web3_from_chain_id()` is an async context manager, not a function call**

```python
# WRONG — returns an async generator object, not a Web3 instance
w3 = web3_from_chain_id(8453)

# RIGHT
async with web3_from_chain_id(8453) as w3:
    ...
```

**4. All Web3 calls are async — always `await`**

```python
# WRONG — returns a coroutine, not the result
balance = w3.eth.get_balance(addr)
result = contract.functions.balanceOf(addr).call()

# RIGHT
balance = await w3.eth.get_balance(addr)
result = await contract.functions.balanceOf(addr).call()
```

**5. Use existing ERC20 helpers — don't inline ABIs**

```python
# WRONG — verbose, error-prone
abi = [{"inputs": [{"name": "account", ...}], ...}]
contract = w3.eth.contract(address=token, abi=abi)
balance = await contract.functions.balanceOf(addr).call()

# RIGHT — one-liner
from wayfinder_paths.core.utils.tokens import get_token_balance
balance = await get_token_balance(token_address, chain_id=8453, wallet_address=addr)

# OR if you need the contract object:
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
contract = w3.eth.contract(address=token, abi=ERC20_ABI)
```

**6. Python `quote_swap` amounts are wei strings, not human-readable**

Note: This applies to the Python `quote_swap()` function in scripts. The MCP `execute(...)` tool takes **human-readable** amounts (e.g. `"5"` for 5 USDC).

```python
# WRONG — "10.0" is not a valid wei amount
quote = await quote_swap(from_token="usd-coin-base", to_token="ethereum-base", amount="10.0", ...)

# RIGHT — convert to wei first
from wayfinder_paths.core.utils.units import to_erc20_raw
amount_wei = str(to_erc20_raw(10.0, decimals=6))  # USDC has 6 decimals
quote = await quote_swap(from_token="usd-coin-base", to_token="ethereum-base", amount=amount_wei, ...)
```

**7. Cross-chain simulation IS possible** — fork both chains, seed expected tokens on the destination fork, then continue. Load `/simulation-dry-run` for the full pattern.

**8. Adapter read methods return `(ok, data)` tuples — always destructure**

```python
# WRONG — treats the tuple as the data itself
data = await adapter.get_meta_and_asset_ctxs()
meta = data[0]  # This is the bool, not the meta!

# RIGHT — destructure the ok/data tuple, check ok
ok, data = await adapter.get_meta_and_asset_ctxs()
if not ok:
    raise RuntimeError(f"API call failed: {data}")
meta, ctxs = data[0], data[1]
```

This applies to virtually all adapter read methods (`get_meta_and_asset_ctxs`, `get_spot_meta`, `get_markets`, `get_user_state`, etc.). The pattern is universal across Hyperliquid, Moonwell, Pendle, and all other adapters.

**9. Load the protocol skill before writing adapter scripts**

Before writing *any* script that uses a protocol adapter, invoke the matching skill (e.g. `/using-hyperliquid-adapter`, `/using-moonwell-adapter`). Skills document method signatures, return shapes, required parameters, and gotchas that aren't obvious from method names alone. Guessing at adapter APIs wastes iterations. See the protocol skills table above.

**10. Write the script file before calling `run_script`**

`mcp__wayfinder__run_script` executes a file at the given path — the file must exist first. Always `Write` the script, then call `run_script`. Don't call `run_script` on a path you haven't written to yet.

When a user wants a **repeatable/automated system** (recurring jobs):

- Create or modify a strategy under `wayfinder_paths/strategies/` and follow the normal manifests/tests workflow.
- Use the project-local runner to call strategy `update` on an interval (no cron needed).

Runner CLI (project-local state in `./.wayfinder/runner/`):

```bash
# Start the daemon (recommended: detached/background)
poetry run wayfinder runner start --detach

# Idempotent: start if needed, otherwise no-op
poetry run wayfinder runner ensure

# Add an interval job (every 10 minutes)
poetry run wayfinder runner add-job \
  --name basis-update \
  --type strategy \
  --strategy basis_trading_strategy \
  --action update \
  --interval 600 \
  --config ./config.json

# Add an interval job for a local one-off script (must live in .wayfinder_runs/ by default)
poetry run wayfinder runner add-job \
  --name hourly-report \
  --type script \
  --script-path .wayfinder_runs/report.py \
  --arg --verbose \
  --interval 3600

# Inspect / control
poetry run wayfinder runner status
poetry run wayfinder runner run-once basis-update
poetry run wayfinder runner pause basis-update
poetry run wayfinder runner resume basis-update
poetry run wayfinder runner delete basis-update
poetry run wayfinder runner stop
```

Architecture/extensibility notes live in `RUNNER_ARCHITECTURE.md`.

Runner MCP tool (controls the daemon via its local Unix socket):

- `mcp__wayfinder__runner(action="status")`
- `mcp__wayfinder__runner(action="daemon_status")`
- `mcp__wayfinder__runner(action="ensure_started")` (starts detached if needed)
- `mcp__wayfinder__runner(action="daemon_stop")`
- `mcp__wayfinder__runner(action="add_job", name="basis-update", interval_seconds=600, strategy="basis_trading_strategy", strategy_action="update", config="./config.json")`
- `mcp__wayfinder__runner(action="add_job", name="hourly-report", type="script", interval_seconds=3600, script_path=".wayfinder_runs/report.py", args=["--verbose"])`
- `mcp__wayfinder__runner(action="pause_job", name="basis-update")`
- `mcp__wayfinder__runner(action="resume_job", name="basis-update")`
- `mcp__wayfinder__runner(action="delete_job", name="basis-update")`
- `mcp__wayfinder__runner(action="run_once", name="basis-update")`
- `mcp__wayfinder__runner(action="job_runs", name="basis-update", limit=20)`
- `mcp__wayfinder__runner(action="run_report", run_id=123, tail_bytes=4000)`

Safety note:

- Runner executions are local automation and do **not** go through the Claude safety review prompt. Treat `update/deposit/withdraw/exit` as live fund-moving actions.

Supported chains:

| Chain     | ID    | Code         | Symbol | Native token ID        |
| --------- | ----- | ------------ | ------ | ---------------------- |
| Ethereum  | 1     | `ethereum`   | ETH    | `ethereum-ethereum`    |
| Base      | 8453  | `base`       | ETH    | `ethereum-base`        |
| Arbitrum  | 42161 | `arbitrum`   | ETH    | `ethereum-arbitrum`    |
| Polygon   | 137   | `polygon`    | POL    | `polygon-ecosystem-token-polygon`|
| BSC       | 56    | `bsc`        | BNB    | `binancecoin-bsc`      |
| Avalanche | 43114 | `avalanche`  | AVAX   | `avalanche-avalanche`|
| Plasma    | 9745  | `plasma`     | PLASMA | `plasma-plasma`        |
| HyperEVM  | 999   | `hyperevm`   | HYPE   | `hyperliquid-hyperevm` |

- **Plasma**: EVM chain where Pendle deploys PT/YT markets. Not Pendle-specific — it's its own chain.
- **HyperEVM**: Hyperliquid's EVM layer. On-chain tokens (HYPE, USDC) live here; perp/spot trading uses the Hyperliquid L1 (off-chain, not EVM).

Gas requirements (critical — assets get stuck without gas):

- **Every on-chain action requires the destination chain's native gas token in the wallet.** Without gas, the wallet cannot transact and assets are effectively stuck until gas is provided.
- **Before any operation on a chain**, check the wallet has sufficient native gas on that chain using `wayfinder://balances/{label}`.
- **Bridging to a new chain for the first time:** The wallet needs native gas before it can do anything. Bridge the native gas token (e.g. ETH) to the destination chain first, then bridge or swap for the target token. Use the native token IDs from the chain table (e.g. `ethereum-base` for ETH on Base).
- Use the native token IDs from the chain table above when bridging gas (e.g. `ethereum-base` for ETH on Base, `plasma-plasma` for PLASMA on Plasma).

Token identifiers (important for quoting/execution/lookups):

All token functions (`get_token_details`, `quote_swap`, `execute`, etc.) expect **token IDs**, not free-text search queries.

- **Token ID format:** `<coingecko_id>-<chain_code>` — the first part is the coingecko_id, NOT the symbol.
  - `usd-coin-base` (USDC on Base — coingecko_id is `usd-coin`, NOT `usdc`)
  - `ethereum-arbitrum` (ETH on Arbitrum)
  - `usdt0-arbitrum` (USDT on Arbitrum)
  - `hyperliquid-hyperevm` (HYPE on HyperEVM)
- **Address ID format:** `<chain_code>_<address>` when you know the ERC20 contract (e.g., `base_0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`).
- **Do NOT pass symbol-chain** (`usdc-base`) or free-text queries (`USDC plasma`) — these will fail. Always use one of the two ID formats above.
- If you don't know a token's coingecko_id, use the MCP resource `wayfinder://tokens/search/{chain_code}/{query}` to find it first, then use the returned token ID.
- See `.claude/skills/using-pool-token-balance-data/rules/tokens.md` for full details.

## Common Commands

Note: `just` is a command runner (install via `brew install just` or `cargo install just`). If you don't have `just`, use the poetry commands directly.

```bash
# Install dependencies
poetry install

# Generate test wallets (required before running tests/strategies)
just create-wallets                    # or: poetry run python scripts/make_wallets.py -n 1

# Run all smoke tests
just test-smoke                        # or: poetry run pytest -k smoke -v

# Test specific strategy or adapter
just test-strategy stablecoin_yield_strategy
just test-adapter pool_adapter

# Run all tests with coverage
just test-cov                          # or: poetry run pytest --cov=wayfinder-paths --cov-report=html -v

# Lint and format
just lint                              # or: poetry run ruff check --fix
just format                            # or: poetry run ruff format

# Validate all manifests
just validate-manifests

# Create new strategy with dedicated wallet
just create-strategy "My Strategy Name"

# Create new adapter
just create-adapter "my_protocol"

# Run a strategy locally
poetry run python -m wayfinder_paths.run_strategy stablecoin_yield_strategy --action status --config config.json

# Publish to PyPI (main branch only)
just publish
```

## Architecture

### Data Flow

```
Strategy → Adapter → Client(s) → Network/API
```

**Strategies** should call **adapters** (not clients directly) for domain actions. Clients are low-level wrappers that handle auth, retries, and response parsing.

### Key Directories

- `wayfinder_paths/core/` - Core engine maintained by team (clients, base classes, services)
- `wayfinder_paths/adapters/` - Community-contributed protocol integrations
- `wayfinder_paths/strategies/` - Community-contributed trading strategies

### Creating New Strategies and Adapters

**Always use the scaffolding scripts** when creating new strategies or adapters. They generate the correct directory structure, boilerplate files, and (for strategies) a dedicated wallet.

**New strategy:**

```bash
just create-strategy "My Strategy Name"
# or: poetry run python scripts/create_strategy.py "My Strategy Name"
```

Creates under `wayfinder_paths/strategies/<name>/`:
- `strategy.py` - Strategy class with required method stubs
- `manifest.yaml` - Strategy manifest (entrypoint, adapters, permissions)
- `test_strategy.py` - Smoke test template
- `examples.json` - Test data file
- `README.md` - Documentation template
- **Dedicated wallet** added to `config.json` with the strategy name as label

**New adapter:**

```bash
just create-adapter "my_protocol"
# or: poetry run python scripts/create_adapter.py "my_protocol"
```

Creates under `wayfinder_paths/adapters/<name>_adapter/`:
- `adapter.py` - Adapter class extending `BaseAdapter`
- `manifest.yaml` - Adapter manifest (entrypoint, capabilities, dependencies)
- `test_adapter.py` - Basic test template
- `examples.json` - Test data file
- `README.md` - Documentation template

Use `--override` flag to replace an existing strategy/adapter.

### Manifests

Every adapter and strategy requires a `manifest.yaml` declaring capabilities, dependencies, and entrypoint. Manifests are validated in CI and serve as the single source of truth.

**Adapter manifest** declares: `entrypoint`, `capabilities`, `dependencies` (client classes)
**Strategy manifest** declares: `entrypoint`, `permissions.policy`, `adapters` with required capabilities

### Built-in Adapters

- **BALANCE** - Wallet balances, token transfers, ledger recording
- **POOL** - Pool discovery, analytics, high-yield searches
- **BRAP** - Cross-chain quotes, swaps, fee breakdowns
- **TOKEN** - Token metadata, price snapshots
- **LEDGER** - Transaction recording, cashflow tracking
- **HYPERLEND** - Lending protocol integration
- **PENDLE** - PT/YT market discovery, time series, Hosted SDK swap tx building

### Strategy Base Class

Strategies extend `wayfinder_paths.core.strategies.Strategy` and must implement:

- `deposit(**kwargs)` → `StatusTuple` (bool, str)
- `update()` → `StatusTuple`
- `status()` → `StatusDict`
- `withdraw(**kwargs)` → `StatusTuple`

## Testing Requirements

### Strategies

- **Required**: `examples.json` file (documentation + test data)
- **Required**: Smoke test exercising deposit → update → status → withdraw
- **Required**: Tests must load data from `examples.json`, never hardcode values

### Adapters

- **Required**: Basic functionality tests with mocked dependencies
- **Optional**: `examples.json` file

### Test Markers

- `@pytest.mark.smoke` - Basic functionality validation
- `@pytest.mark.requires_wallets` - Tests needing local wallets configured
- `@pytest.mark.requires_config` - Tests needing config.json

## Configuration

Config priority: Constructor parameter > config.json > Environment variable (`WAYFINDER_API_KEY`)

Copy `config.example.json` to `config.json` (or run `python3 scripts/setup.py`) for local development.

## CI/CD Pipeline

PRs are tested with:

1. Lint & format checks (Ruff)
2. Smoke tests
3. Adapter tests (mocked dependencies)
4. Integration tests (PRs only)
5. Security scans (Bandit, Safety)

## Key Patterns

- Adapters compose one or more clients and raise `NotImplementedError` for unsupported ops
- All async methods use `async/await` pattern
- Return types are `StatusTuple` (success bool, message str) or `StatusDict` (portfolio data)
- Wallet generation updates `config.json` in repo root
- Per-strategy wallets are created automatically via `just create-strategy`

## Publishing

Publishing to PyPI is restricted to `main` branch. Order of operations:

1. Merge changes to main
2. Bump version in `pyproject.toml`
3. Run `just publish`
4. Then dependent apps can update their dependencies

## Wallet management and portfolio discovery

Read-only wallet information is exposed via MCP resources, and fund-moving / tracking actions via the `mcp__wayfinder__wallets` tool.

**Quick balance check:**

- Use MCP resource `wayfinder://balances/{label}` for enriched token balances (USD totals + chain breakdown).
- Use MCP resource `wayfinder://wallets/{label}` for tracked protocol history for a wallet label.
- Use `mcp__wayfinder__wallets(action="discover_portfolio", ...)` for live protocol position discovery (Hyperliquid perp, Moonwell supplies, etc.).

**Read-only resources:**

- `wayfinder://wallets` - list all wallets and tracked protocols
- `wayfinder://wallets/{label}` - full profile for a wallet (protocol interactions, transactions)
- `wayfinder://balances/{label}` - enriched token balances
- `wayfinder://activity/{label}` - recent wallet activity (best-effort)

**Tool actions (`mcp__wayfinder__wallets`):**

- `create` - create a new local wallet (writes to `config.json`)
- `annotate` - record a protocol interaction (internal use)
- `discover_portfolio` - query adapters for positions

**Automatic tracking:**

- Profiles auto-update when you use `mcp__wayfinder__execute`, `mcp__wayfinder__hyperliquid_execute`, or `mcp__wayfinder__run_script` (with `wallet_label`)

**Portfolio discovery:**

- Use `mcp__wayfinder__wallets(action="discover_portfolio", wallet_label="main")` to fetch all positions
- Only queries protocols the wallet has previously interacted with
- **Warning:** If 3+ protocols are tracked, tool returns a warning and asks for confirmation or use `parallel=true`
- Use `protocols=["hyperliquid"]` to query specific protocols only

**Manual annotation:**

- Use `action="annotate"` if you know a wallet has used a protocol not yet tracked

**Best practices:**

- Use `wayfinder://wallets` to see all wallets and their tracked protocols at a glance
- Annotate manually if a protocol interaction predates this system
