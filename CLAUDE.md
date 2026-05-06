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

- **Quote before swap (MANDATORY):** Before calling `mcp__wayfinder__execute(kind="swap")`, always call `mcp__wayfinder__quote_swap` first. Verify the resolved `from_token` and `to_token` (symbol, address, chain) match intent, then show the user the route, estimated output, and fee. Only proceed to `execute` after the user confirms — unless the user has explicitly said to skip quoting (e.g. "just do it", "skip the quote").
- **Route planning for non-trivial swaps:** Before quoting, assess whether a direct route is likely to exist between the two tokens. If the pair is illiquid, cross-chain, or involves a long-tail token, reason through candidate intermediate hops first (e.g. tokenA → USDC → tokenB, or tokenA → native gas token → tokenB). Quote the most promising paths and compare outputs before presenting to the user. Skip this planning step only for well-known liquid pairs on the same chain (e.g. ETH → USDC on Arbitrum).
- On-chain writes: use MCP `execute(...)` (swap/send). The hook shows a human-readable preview and asks for confirmation.
- Arbitrary EVM contract interactions: use MCP `contract_call(...)` (read-only) and `contract_execute(...)` (writes, gated by a review prompt).
  - ABI handling: pass a minimal `abi`/`abi_path` when you can. If omitted, the tools fall back to fetching the ABI from Etherscan V2 (requires `system.etherscan_api_key` or `ETHERSCAN_API_KEY`, and the contract must be verified). If the target is a proxy, tools attempt to resolve the implementation address and fetch the implementation ABI.
  - To fetch an ABI directly (without making a call), use MCP `contract_get_abi(...)`.
- Hyperliquid perp writes: use MCP `hyperliquid_execute(...)` (orders/leverage). Also gated by a review prompt.
- Polymarket writes: use MCP `polymarket_execute(...)` (bridge deposit/withdraw, buy/sell, limit orders, redemption). Also gated by a review prompt.
- Contract deploys: use MCP `deploy_contract(...)` (compile + deploy + verify). Also gated by a review prompt. Use `compile_contract(...)` for compilation only (read-only, no confirmation).
  - Deployments (and other contract actions) are recorded in wallet profiles. Read `wayfinder://wallets/{label}` and look at `profile.transactions` entries with `protocol: "contracts"` (also written to `.wayfinder_runs/wallet_profiles.json`).
  - **Artifact persistence:** Source code, ABI, and metadata are saved to `.wayfinder_runs/contracts/{chain_id}/{address}/` and survive scratch directory cleanup. Browse with `wayfinder://contracts` (list all) or `wayfinder://contracts/{chain_id}/{address}` (specific contract).
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
| ether.fi (eETH/weETH) | `/using-etherfi-adapter`         |
| Ethena (sUSDe)        | `/using-ethena-vault-adapter`    |
| Hyperliquid           | `/using-hyperliquid-adapter`     |
| Hyperlend             | `/using-hyperlend-adapter`       |
| Boros                 | `/using-boros-adapter`           |
| BRAP (swaps)          | `/using-brap-adapter`            |
| Polymarket            | `/using-polymarket-adapter`      |
| CCXT (CEX)            | `/using-ccxt-adapter`            |
| Uniswap (V3)          | `/using-uniswap-adapter`         |
| ProjectX (V3 fork)    | `/using-projectx-adapter`        |
| Alpha Lab             | `/using-alpha-lab`               |
| Delta Lab             | `/using-delta-lab`               |
| Pools/Tokens/Balances | `/using-pool-token-balance-data` |
| Simulation / Dry-run  | `/simulation-dry-run`            |
| Backtesting           | `/backtest-strategy`             |
| Contract Dev          | `/contract-development`          |
| Paths (search/install/update/build/publish) | `/developing-wayfinder-paths` |

Skills contain rules for correct method usage, common gotchas, and high-value read patterns. **Always load the skill first** — don't guess at adapter APIs.

## Backtesting Framework

Supports: perp/spot momentum, delta-neutral basis carry, lending yield rotation, carry trade. All data (price, funding, lending) is **hourly**. Oldest available: **~August 2025** (211-day retention).

**Always load `/backtest-strategy` skill first** — routing table, examples, config reference, and gotchas are all there. For yield/lending strategies also load `yield-strategies.md`.

All stats are decimals — format with `:.2%`. Key: `sharpe` (>2.0 excellent), `total_return`, `max_drawdown`, `total_funding` (negative = income received), `trade_count`.

Once validated: `just create-strategy "Name"` → implement deposit/update/withdraw/exit → smoke tests → deploy small capital first.


## Contract development

Before writing or deploying Solidity contracts, invoke `/contract-development`.

## Data accuracy (no guessing)

When answering questions about **rates/APYs/funding**:

- Never invent or estimate values.
- Always fetch the value via an adapter/client/tool call when possible.
- Before searching external docs, consult this repo's own adapters/clients (and their `manifest.yaml` + `examples.json`) first.
- If you cannot fetch it (auth/network/tooling), say so explicitly and provide the exact call/script needed to fetch it.

## Alpha Lab MCP resources (alpha discovery)

**Load `/using-alpha-lab` skill for detailed docs.** Quick reference below.

Alpha Lab is a **scored alpha insight feed** that surfaces actionable DeFi signals (tweets, chain flows, APY highlights, delta-neutral pairs). Results ranked by `insightfulness_score` (0-1). Read-only — discovery only, no execution.

**MCP resources:**

- `wayfinder://alpha-lab/types` - List available scan types
- `wayfinder://alpha-lab/search/{query}/{scan_type}/{created_after}/{created_before}/{limit}` - Search insights

**Search params:** use `_` as placeholder to skip optional params.

| Param            | Values                                                                                                                                            | Default |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `query`          | text search or `_` for none                                                                                                                       | `_`     |
| `scan_type`      | `all`, `twitter_post`, `defi_llama_chain_flow`, `defi_llama_overview`, `defi_llama_protocol`, `delta_lab_top_apy`, `delta_lab_best_delta_neutral` | `all`   |
| `created_after`  | ISO 8601 datetime or `_`                                                                                                                          | `_`     |
| `created_before` | ISO 8601 datetime or `_`                                                                                                                          | `_`     |
| `limit`          | 1-200                                                                                                                                             | `20`    |

**Examples:** `wayfinder://alpha-lab/search/_/all/_/_/20` (all), `wayfinder://alpha-lab/search/_/twitter_post/_/_/10` (twitter), `wayfinder://alpha-lab/search/ETH/all/_/_/10` (ETH). Client: `await ALPHA_LAB_CLIENT.search(scan_type="twitter_post", min_score=0.7, limit=20)`.

## Delta Lab (yield discovery)

**ALWAYS load `/using-delta-lab` before any yield, basis, APY, delta-neutral, lending-rate, perp-funding, or opportunity-screening work — and before writing any script that touches `DELTA_LAB_CLIENT`.** It is a load-bearing library; the skill documents the full Python client surface (entity / catalog / graph / search / TS / latest / bulk / `explore` / `fetch_backtest_bundle`), typed records, and composition recipes. Don't guess at method names.

- **APY format:** decimal floats (`0.98 = 98%`, NOT 0.98%). Multiply by 100 to display.
- **MCP philosophy:** quick snapshots only. Anything time-series, plot, filter, by-asset-id, multi-day, or bulk → Python client. Search / screening / opportunity MCP methods return dicts.
- **MCP URIs (full list — narrow on purpose):**
  - `wayfinder://delta-lab/symbols`
  - `wayfinder://delta-lab/top-apy/{LB}/{N}`
  - `wayfinder://delta-lab/{basis_symbol}/apy-sources/{LB}/{N}`
  - `wayfinder://delta-lab/{symbol}/basis`
  - `wayfinder://delta-lab/assets/search/{chain}/{query}/{limit}`
  - `wayfinder://delta-lab/screen/price/{sort}/{N}/{basis}`
  - `wayfinder://delta-lab/screen/lending/{sort}/{N}/{basis}`
  - `wayfinder://delta-lab/screen/perp/{sort}/{N}/{basis}`
  - `wayfinder://delta-lab/screen/borrow-routes/{sort}/{N}/{basis}/{borrow_basis}/{chain_id}`

  Anything beyond this (time series, asset-id lookups, by-asset-id screening, plotting, bulk data) — go straight to the Python client.

## Pack applets

When creating or updating a Wayfinder pack with a browser applet:

- browser applets must use the public Delta Lab browser-safe route:
  - prod: `https://strategies.wayfinder.ai/api/v1/delta-lab/public/assets/<symbol>/timeseries/`
  - dev: `https://strategies-dev.wayfinder.ai/api/v1/delta-lab/public/assets/<symbol>/timeseries/`
- authenticated Delta Lab routes (`/api/v1/delta-lab/assets/...`) are for SDK/server-side use, not browser applets
- take the base URL from the host bridge when available:
  - prefer `wf:state.apiBase`
  - otherwise use the `wf:hello` origin when embedded by the Strategies host
  - do not probe both dev and prod from the same applet build
- treat non-200 responses, especially `404`, as expected unavailability:
  - show a clear "data unavailable" or "waiting for host API" state
  - do not crash the applet on missing data
- ensure every referenced static resource is present under `applet/dist/`
- include explicit icon tags (`icon`, `shortcut icon`, `apple-touch-icon`) in the applet HTML to avoid implicit browser 404s for missing favicon resources
- do not call `/api/v1/delta-lab/symbols/`; that route does not exist for pack applets

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
- **Hyperliquid perps/spot/outcomes:** use `mcp__wayfinder__hyperliquid_execute` (market/limit, leverage, cancel; HIP-4 outcome markets via `place_outcome_order`). **Before your first `hyperliquid_execute` call in a session, invoke `/using-hyperliquid-adapter`** to load the MCP tool's required-parameter rules (`is_spot`, `leverage`, `usd_amount_kind`, outcome `outcome_id`/`side`, etc.). The skill covers both the MCP tool interface and the Python adapter.
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

HIP-3 dex abstraction (xyz/flx/vntl/hyna/km perps), HIP-4 outcome markets (binary daily prediction contracts, **collateralized in USDH**, not USDC), and Hyperliquid deposits/withdrawals: all handled in the Hyperliquid adapter/tooling — load `/using-hyperliquid-adapter` when scripting.

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

Sizing note (avoid ambiguity): if a user says "$X at Y× leverage", confirm whether `$X`is **notional** or **margin** (use`usd_amount_kind="notional"|"margin"`on`mcp**wayfinder**hyperliquid_execute`).

**Scripting helper for adapters:**

**Before writing any adapter script**, invoke the matching protocol skill (e.g. `/using-pendle-adapter`, `/using-hyperliquid-adapter`). Skills document method signatures, return shapes, and field names — guessing wastes iterations. See the protocol skills table above.

When writing scripts under `.wayfinder_runs/`, use `get_adapter()` to simplify setup:

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

# Single-wallet adapter (sign_callback + wallet_address)
adapter = await get_adapter(MoonwellAdapter, "main")
await adapter.set_collateral(mtoken=USDC_MTOKEN)

# Dual-wallet adapter (main + strategy, e.g. BalanceAdapter)
from wayfinder_paths.adapters.balance_adapter import BalanceAdapter
adapter = await get_adapter(BalanceAdapter, "main", "my_strategy")

# Read-only (no wallet needed)
adapter = await get_adapter(PendleAdapter)
```

`get_adapter()` auto-loads `config.json`, looks up wallets by label (local or remote), creates signing callbacks, and wires them into the adapter constructor. Works transparently for both local wallets (`private_key_hex`) and remote wallets (Privy server). It introspects the adapter's `__init__` signature to determine the wiring:

- `sign_callback` + `wallet_address` → single-wallet adapter (most adapters)
- `sign_hash_callback` → also wired if the adapter accepts it (e.g. PolymarketAdapter for CLOB signing)
- `main_sign_callback` + `strategy_sign_callback` → dual-wallet adapter (BalanceAdapter); requires two wallet labels

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

**0. Client vs Adapter return patterns — CRITICAL DIFFERENCE**

**Clients return data directly; Adapters return `(ok, data)` tuples.** This is the #1 source of script errors.

```python
# CLIENTS (return data directly, raise exceptions on errors)
from wayfinder_paths.core.clients.DeltaLabClient import DELTA_LAB_CLIENT
from wayfinder_paths.core.clients.PoolClient import POOL_CLIENT
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT

# WRONG — clients don't return tuples
ok, data = await DELTA_LAB_CLIENT.get_basis_apy_sources(...)  # ❌ ValueError: too many values to unpack

# RIGHT — clients return data directly
data = await DELTA_LAB_CLIENT.get_basis_apy_sources(...)  # ✅ dict
pools = await POOL_CLIENT.get_pools(...)  # ✅ LlamaMatchesResponse
token = await TOKEN_CLIENT.get_token_details(...)  # ✅ TokenDetails

# ADAPTERS (always return tuple[bool, data])
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter

adapter = await get_adapter(HyperliquidAdapter)

# WRONG — adapters always return tuples
data = await adapter.get_meta_and_asset_ctxs()  # ❌ data is actually (True, {...})

# RIGHT — destructure the tuple and check ok
ok, data = await adapter.get_meta_and_asset_ctxs()  # ✅
if not ok:
    raise RuntimeError(f"Adapter call failed: {data}")
meta, ctxs = data[0], data[1]
```

**Rule of thumb:** `wayfinder_paths.core.clients` → data directly. `wayfinder_paths.adapters` → `(ok, data)` tuple.

**1. `get_adapter()` already loads config — don't call `load_config()` first.** See `get_adapter()` examples in the scripting helper section above.

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

**8. Write the script file before calling `run_script`**

`mcp__wayfinder__run_script` executes a file at the given path — the file must exist first. Always `Write` the script, then call `run_script`.

**9. Funding rate sign (CRITICAL for perp trading)**

**CRITICAL: Negative funding means shorts PAY longs** (not the other way around).

```python
# WRONG interpretation
funding_rate = -0.08  # -8% annually
print("Negative = good for shorts!")  # ❌ BACKWARDS!

# RIGHT interpretation
funding_rate = -0.08  # -8% annually
if funding_rate > 0:
    # Positive funding: Longs pay shorts (good for shorts)
    print("Shorts receive funding")  # ✅
else:
    # Negative funding: Shorts pay longs (bad for shorts)
    print("Shorts PAY funding")  # ✅
```

This applies to:

- Hyperliquid perp funding rates
- Delta Lab perp opportunities
- Any perp trading strategy analysis

When evaluating perp positions, always verify the sign interpretation - it's backwards from intuition for many traders.

When a user wants a **repeatable/automated system** (recurring jobs):

- Create or modify a strategy under `wayfinder_paths/strategies/` and follow the normal manifests/tests workflow.
- Use the project-local runner to call strategy `update` on an interval (no cron needed).

Runner CLI (project-local state in `./.wayfinder/runner/`):

```bash
poetry run wayfinder runner start             # Start daemon (idempotent)
poetry run wayfinder runner add-job --name basis-update --type strategy --strategy basis_trading_strategy --action update --interval 600 --config ./config.json
poetry run wayfinder runner status | run-once | pause | resume | delete <job> | stop
```

See `RUNNER_ARCHITECTURE.md`.

Runner MCP tool: `mcp__wayfinder__runner(action=...)`.

Safety note:

- Runner executions are local automation and do **not** go through the Claude safety review prompt. Treat `update/deposit/withdraw/exit` as live fund-moving actions.

Supported chains:

| Chain     | ID    | Code        | Symbol | Native token ID                   |
| --------- | ----- | ----------- | ------ | --------------------------------- |
| Ethereum  | 1     | `ethereum`  | ETH    | `ethereum-ethereum`               |
| Base      | 8453  | `base`      | ETH    | `ethereum-base`                   |
| Arbitrum  | 42161 | `arbitrum`  | ETH    | `ethereum-arbitrum`               |
| Polygon   | 137   | `polygon`   | POL    | `polygon-ecosystem-token-polygon` |
| BSC       | 56    | `bsc`       | BNB    | `binancecoin-bsc`                 |
| Avalanche | 43114 | `avalanche` | AVAX   | `avalanche-avalanche`             |
| Plasma    | 9745  | `plasma`    | PLASMA | `plasma-plasma`                   |
| HyperEVM  | 999   | `hyperevm`  | HYPE   | `hyperliquid-hyperevm`            |

- **Plasma**: EVM chain where Pendle deploys PT/YT markets. Not Pendle-specific — it's its own chain.
- **HyperEVM**: Hyperliquid's EVM layer. On-chain tokens (HYPE, USDC) live here; perp/spot trading uses the Hyperliquid L1 (off-chain, not EVM).

Gas requirements (critical — assets get stuck without gas):

- **Before any on-chain operation**, check the wallet has native gas on that chain using `wayfinder://balances/{label}`.
- If bridging to a new chain for the first time: bridge gas first. If you need the native token ID, look it up via `wayfinder://tokens/search/{chain_code}/{query}`.

Token identifiers (important for quoting/execution/lookups):

- Use **token IDs** (`<coingecko_id>-<chain_code>`) or **address IDs** (`<chain_code>_<address>`). Full details: `.claude/skills/using-pool-token-balance-data/rules/tokens.md`.

### Path updates

- `poetry run wayfinder path update <slug>` compares the locally installed version in `.wayfinder/paths.lock.json` to the API's `active_bonded_version`.
- By default it updates only to the current live bonded version, not `latest_version` and not a pending upgrade.
- `--version <x.y.z>` overrides that default and installs a specific public version.
- After pulling the new version, the CLI tries to re-use recorded activation metadata; if none is stored, it tries one safe workspace default; otherwise it falls back to pull-only and prints the manual `path activate` command.

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

Creates `wayfinder_paths/strategies/<name>/` with strategy.py, manifest.yaml, test, examples.json, README, and a **dedicated wallet** in `config.json`.

**New adapter:**

```bash
just create-adapter "my_protocol"
# or: poetry run python scripts/create_adapter.py "my_protocol"
```

Creates `wayfinder_paths/adapters/<name>_adapter/` with adapter.py, manifest.yaml, test, examples.json, README. Use `--override` to replace existing.

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

## Configuration

Config priority: Constructor parameter > config.json > Environment variable (`WAYFINDER_API_KEY`)

Copy `config.example.json` to `config.json` (or run `python3 scripts/setup.py`) for local development.

## Key Patterns

- Adapters compose one or more clients and raise `NotImplementedError` for unsupported ops
- All async methods use `async/await` pattern
- Return types are `StatusTuple` (success bool, message str) or `StatusDict` (portfolio data)
- Wallet generation updates `config.json` in repo root
- Per-strategy wallets are created automatically via `just create-strategy`

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
- `wayfinder://contracts` - list all locally-deployed contracts (name, address, chain, verification status)
- `wayfinder://contracts/{chain_id}/{address}` - full metadata + ABI for a deployed contract

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
