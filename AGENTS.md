# AGENTS.md

## Project Overview

Wayfinder Paths is a public Python SDK for DeFi trading strategies and adapters. It provides the building blocks for automated trading: adapters (exchange/protocol integrations), strategies (trading algorithms), and clients (low-level API wrappers).

## Personality

- Cost Efficient, you don't waste time exploring random information, you only call tools minimally, everything has a strong time cost.
- Precise, you always understand and execute the user's requirements exactly.

## Notes

- If confused about wallet balances, fetch fresh balances! Since the user has the private key and other ways to fund wallets, they might have modified wallet state themselves, we want to proactively check misalignments in wallet expectations.

## First-Time Setup (Auto-detect)

**IMPORTANT: On every new conversation, Detect Shells Instance first.**

Probe `http://localhost:4096/global/health`. If it returns `{ "healthy": true, ... }`, you are running inside a Shells instance — the SDK is already installed at `/wf/sdk`, the API key is already in the environment, and remote wallets are managed for you. **Do NOT run `setup.py`, do NOT prompt for an API key, do NOT touch `config.json`** — proceed normally.

## Wallets

**On Wayfinder Shells Instances, ALL wallets MUST be remote. No local wallets — ever.** Remote wallets are managed for you and provide analytics, activity tracking, and session-aware policies. Local wallets are invisible to the rest of the platform and break those guarantees. The `wallets` MCP tool enforces this and will reject local-wallet creation when running on Wayfinder Shells.

## Wayfinder Shells Instance Environment Variables

When the SDK runs inside Wayfinder Shells, two env vars are injected at startup:

| Variable               | What it is                                                                             |
| ---------------------- | -------------------------------------------------------------------------------------- |
| `WAYFINDER_API_KEY`    | The user's `wf_…` Wayfinder API key. Picked up automatically by config priority below. |
| `OPENCODE_INSTANCE_ID` | The Wayfinder Shells identifier for this runtime. Useful for logs / diagnostics.       |

## Safety defaults

- **Quote before swap (MANDATORY):** Before executing any swap, always quote first. Verify the resolved `from_token` and `to_token` (symbol, address, chain) match intent, then show the user the route, estimated output, and fee. Only proceed after the user confirms.
- **Route planning for non-trivial swaps:** Before quoting, assess whether a direct route is likely to exist between the two tokens. If the pair is illiquid, cross-chain, or involves a long-tail token, reason through candidate intermediate hops first (e.g. tokenA → USDC → tokenB). Quote the most promising paths and compare outputs.

Transaction outcome rules (don't assume a tx hash means success):

- A transaction is only successful if the on-chain receipt has `status=1`.
- The SDK raises `TransactionRevertedError` when a receipt returns `status=0`.
- If a fund-moving step fails/reverts, stop the flow and report the error; don't continue executing dependent steps.

## Simulation / scenario testing (vnet only)

- Before broadcasting complex fund-moving flows live, run at least one forked **dry-run scenario** (Gorlami). These are EVM virtual testnets (vnets) that simulate **sequential on-chain operations** with real EVM state changes.
- **Cross-chain:** For flows spanning multiple EVM chains, spin up a fork per chain. Execute the source tx on the source fork, seed the expected tokens on the destination fork (simulating bridge delivery), then continue on the destination fork.
- **Scope:** Vnets only cover EVM chains (Base, Arbitrum, etc.). Off-chain or non-EVM protocols like Hyperliquid **cannot** be simulated.

## Backtesting Framework

Supports: perp/spot momentum, delta-neutral basis carry, lending yield rotation, carry trade. All data (price, funding, lending) is **hourly**. Oldest available: **~August 2025** (211-day retention).

All stats are decimals — format with `:.2%`. Key: `sharpe` (>2.0 excellent), `total_return`, `max_drawdown`, `total_funding` (negative = income received), `trade_count`.

Once validated: `just create-strategy "Name"` → implement deposit/update/withdraw/exit → smoke tests → deploy small capital first.

## Data accuracy (no guessing)

When answering questions about **rates/APYs/funding**:

- Never invent or estimate values.
- Always fetch the value via an adapter/client/tool call when possible.
- Before searching external docs, consult this repo's own adapters/clients (and their `manifest.yaml` + `examples.json`) first.
- If you cannot fetch it, say so explicitly and provide the exact call/script needed to fetch it.

## Execution modes (one-off vs recurring)

### MCP vs scripting — pick the right tool

Prefer **MCP tools** for simple, one-shot actions: a single quote, a single swap, reading a
balance, placing one order, querying a strategy. They're already wired up, validated, and
return structured results.

Reach for **scripts under `.wayfinder_runs/`** when the work is complex or repetitive: stitching
multiple adapter calls together, fan-out across many wallets/chains, multi-step flows with
conditional branches, or anything you'll want to re-run. Scripts can be scheduled via
`runner(action="add_job", type="script", ...)` once they're stable.

Rough cut: if you can express it as one MCP call, use the MCP call. If you find yourself
chaining three or more, write a script.

### Scripting helper for adapters

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

`get_adapter()` auto-loads `config.json`, looks up wallets by label (local or remote), creates signing callbacks, and wires them into the adapter constructor. It introspects the adapter's `__init__` signature to determine the wiring:

- `sign_callback` + `wallet_address` → single-wallet adapter (most adapters)
- `sign_hash_callback` → also wired if the adapter accepts it (e.g. PolymarketAdapter for CLOB signing)
- `main_sign_callback` + `strategy_sign_callback` → dual-wallet adapter (BalanceAdapter); requires two wallet labels

For direct Web3 usage in scripts, **do not hardcode RPC URLs**. Use `web3_from_chain_id(chain_id)` from `wayfinder_paths.core.utils.web3` — it's an **async context manager**:

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
ok, data = await DELTA_LAB_CLIENT.get_basis_apy_sources(...)  # ❌

# RIGHT — clients return data directly
data = await DELTA_LAB_CLIENT.get_basis_apy_sources(...)  # ✅

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
```

**Rule of thumb:** `wayfinder_paths.core.clients` → data directly. `wayfinder_paths.adapters` → `(ok, data)` tuple.

**1. `get_adapter()` already loads config — don't call `load_config()` first.**

**2. `load_config()` returns `None` — it mutates a global**

```python
# WRONG
config = load_config("config.json")
api_key = config["system"]["api_key"]  # TypeError!

# RIGHT
from wayfinder_paths.core.config import load_config, CONFIG
load_config("config.json")
api_key = CONFIG["system"]["api_key"]

# OR — plain dict:
from wayfinder_paths.core.config import load_config_json
config = load_config_json("config.json")
```

**3. `web3_from_chain_id()` is an async context manager, not a function call**

```python
# WRONG
w3 = web3_from_chain_id(8453)

# RIGHT
async with web3_from_chain_id(8453) as w3:
    ...
```

**4. All Web3 calls are async — always `await`**

```python
# WRONG
balance = w3.eth.get_balance(addr)

# RIGHT
balance = await w3.eth.get_balance(addr)
```

**5. Use existing ERC20 helpers — don't inline ABIs**

```python
# RIGHT — one-liner
from wayfinder_paths.core.utils.tokens import get_token_balance
balance = await get_token_balance(token_address, chain_id=8453, wallet_address=addr)

# OR if you need the contract object:
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
contract = w3.eth.contract(address=token, abi=ERC20_ABI)
```

**6. Python `quote_swap` amounts are wei strings, not human-readable**

```python
# WRONG
quote = await quote_swap(from_token="usd-coin-base", to_token="ethereum-base", amount="10.0", ...)

# RIGHT — convert to wei first
from wayfinder_paths.core.utils.units import to_erc20_raw
amount_wei = str(to_erc20_raw(10.0, decimals=6))  # USDC has 6 decimals
quote = await quote_swap(from_token="usd-coin-base", to_token="ethereum-base", amount=amount_wei, ...)
```

**7. Write the script file before running it.** The file must exist first.

**8. Funding rate sign (CRITICAL for perp trading)**

**Negative funding means shorts PAY longs** (not the other way around).

```python
if funding_rate > 0:
    # Positive: Longs pay shorts (good for shorts)
    pass
else:
    # Negative: Shorts pay longs (bad for shorts)
    pass
```

### Key domain knowledge

Hyperliquid minimums:

- **Minimum deposit: $5 USD** (deposits below this are **lost**)
- **Minimum order: $10 USD notional** (applies to both perp and spot)

Hyperliquid surfaces in the adapter/MCP: perp, spot, HIP-3 builder-deployed perp dexes (`xyz`/`flx`/`vntl`/`hyna`/`km`...), and HIP-4 outcome markets (binary/multi-outcome prediction contracts). Outcomes use a separate asset-id space (`100_000_000 + 10*outcome_id + side`) and integer contract sizes; **settle in USDH** (token 360), not USDC; settle daily at 06:00 UTC; written via `hyperliquid_execute(action="place_outcome_order", ...)`. See `/using-hyperliquid-adapter` rules for details.

**Outcome / prediction markets — search both venues, let the user pick.** When a user mentions "outcome market" or "prediction market" without naming the platform, **search both venues in parallel** and present candidates side-by-side so the user can choose. Two venues:

- **Hyperliquid HIP-4** — daily binary price contracts settled in USDH on the HL L1; rotating daily lineup. Search via `mcp__wayfinder__hyperliquid_search_market(query=...)` (read the `outcomes` bucket).
- **Polymarket** — long-form prediction markets (politics, sports, events, crypto milestones), settled in USDC.e on Polygon. Search via `mcp__wayfinder__polymarket(action="search", query=..., limit=...)`.

Present results as a table grouped by venue, then ask which market to trade — the same theme can list on both venues with different sizes, expiries, and collateral. Load `/using-hyperliquid-adapter` or `/using-polymarket-adapter` once the user picks.

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

- **Plasma**: EVM chain where Pendle deploys PT/YT markets.
- **HyperEVM**: Hyperliquid's EVM layer. On-chain tokens (HYPE, USDC) live here; perp/spot trading uses the Hyperliquid L1 (off-chain, not EVM).

Gas requirements (critical — assets get stuck without gas):

- **Before any on-chain operation**, check the wallet has native gas on that chain.
- If bridging to a new chain for the first time: bridge gas first.

Token identifiers (important for quoting/execution/lookups):

- Use **token IDs** (`<coingecko_id>-<chain_code>`) or **address IDs** (`<chain_code>_<address>`).

### Recurring automation (Runner)

**All scheduled/recurring tasks MUST go through the runner daemon.** Do not use cron, systemd timers, or background loops. The daemon handles job persistence, failure tracking, timeouts, and session notifications.

```
runner(action="ensure_started")                       # idempotent — safe to call multiple times
runner(action="add_job",                              # schedule a strategy
       name="basis-update",
       type="strategy",
       strategy="basis_trading_strategy",
       strategy_action="update",
       interval_seconds=600,
       config="./config.json")
runner(action="add_job",                              # schedule a script
       name="check-balances",
       type="script",
       script_path=".wayfinder_runs/check_balances.py",
       interval_seconds=300)
runner(action="status")                               # show daemon + all jobs
runner(action="run_once", name="<name>")              # trigger immediate run
runner(action="pause_job", name="<name>")
runner(action="resume_job", name="<name>")
runner(action="delete_job", name="<name>")
runner(action="daemon_stop")                          # shut down daemon
```

See `RUNNER_ARCHITECTURE.md`.

## Path updates

- `poetry run wayfinder path update <slug>` is the single-path update command for installed paths.
- Default target selection is the API's `active_bonded_version`, not `latest_version` and not a pending version still in probation.
- `--version <x.y.z>` lets the user choose a specific public version explicitly.
- The CLI checks `.wayfinder/paths.lock.json` for the installed version, pulls the target version when newer, and then tries to re-use stored activation metadata.
- If activation metadata is missing, it tries one safe workspace default; if it still cannot determine an activation target, it completes the pull and prints the manual `path activate` command instead of failing.

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

**Always use the scaffolding scripts** when creating new strategies or adapters.

**New strategy:**

```bash
just create-strategy "My Strategy Name"
```

Creates `wayfinder_paths/strategies/<name>/` with strategy.py, manifest.yaml, test, examples.json, README, and a **dedicated wallet** in `config.json`.

**New adapter:**

```bash
just create-adapter "my_protocol"
```

Creates `wayfinder_paths/adapters/<name>_adapter/` with adapter.py, manifest.yaml, test, examples.json, README.

### Built-in Adapters

- **BALANCE** - Wallet balances, token transfers, ledger recording
- **POOL** - Pool discovery, analytics, high-yield searches
- **BRAP** - Cross-chain quotes, swaps, fee breakdowns
- **TOKEN** - Token metadata, price snapshots
- **LEDGER** - Transaction recording, cashflow tracking
- **HYPERLEND** - Lending protocol integration
- **PENDLE** - PT/YT market discovery, time series, Hosted SDK swap tx building

### Session vs strategy wallets

Remote wallets come in two flavours — pick based on how the wallet will be used:

- **Session wallet** (default, recommended for normal trading) — 1-hour TTL, refreshed while the user has the UI open. Use this for day-to-day trading where a human is present and approving actions.
- **Strategy wallet** — 7-day TTL, intended for longer-running scheduled automation that signs without a human in the loop. Higher blast radius if the wallet leaks, so reach for it only when you actually need unattended signing across many hours; default to a session wallet otherwise.

```
# Session wallet (default, 1-hour TTL)
wallets(action="create", label="main", remote=True, wallet_type="session")

# Strategy wallet (7-day TTL) — pair with a strategy job on the runner
wallets(action="create", label="my_strategy", remote=True, wallet_type="strategy")
```

**Always read wallets through the MCP resources below. Never grep `config.json` for `wallets[]` or read wallet files directly.** They are the only source of truth — on Wayfinder Shells the remote wallets are not in `config.json`, so reading the file misses them entirely.

| Resource                       | What you get                                                         |
| ------------------------------ | -------------------------------------------------------------------- |
| `wayfinder://wallets`          | List all wallets (remote on Shells, merged local + remote elsewhere) |
| `wayfinder://wallets/{label}`  | Single wallet by label (includes profile / tracked protocols)        |
| `wayfinder://balances/{label}` | USD-aggregated balances, per-chain breakdown, spam-filtered          |
| `wayfinder://activity/{label}` | Recent on-chain activity                                             |

On a Wayfinder Shells Instance, always pass `remote=True` when creating wallets — local wallets are rejected.

In Python scripts, prefer the helpers in `wayfinder_paths.mcp.utils` (`load_wallets`, `find_wallet_by_label`) — they hit the same code path as the resource and return remote wallets transparently.

## Messaging the user (Shells instances only)

If you detected a Wayfinder Shells instance in "First-Time Setup", you may email the owner to report completed work, surface decisions that need them, or flag anything you can't resolve. Backend only delivers when `email_verified` is true on the user, and throttles to **4 emails / user / day** — budget your sends accordingly.

See `/using-shells-notify` for the MCP tool, Python client, limits, and Markdown formatting tips.

## Frontend Context (Shells instances only)

If you detected a Wayfinder Shells instance, you can read what the user is currently viewing (active chart) and project overlays (price lines, markers, ranges, trends) onto their chart in real-time.

See `/using-shells-projections` for the MCP tools, Python client, projection types, and gotchas.

## Scheduled Jobs (Shells instances only)

On Wayfinder Shells instances (`OPENCODE_INSTANCE_ID` set), the runner daemon automatically syncs job and run state to vault-backend. This happens transparently — no agent action needed.

- **Job sync**: When a job is added, updated, paused, resumed, or deleted, the daemon pushes the current state to `PUT /instances/{id}/jobs/{name}/`
- **Run sync**: After each run completes, the daemon pushes the full log output to `POST /instances/{id}/jobs/{name}/runs/`
- **Local-only**: On non-Shells instances (no `OPENCODE_INSTANCE_ID`), sync is skipped silently

The frontend shows synced jobs and runs in the "Scheduled" tab of the shells sidebar.

**Don't silence `job_result` notifications.** When a scheduled job posts a `job_result` into the conversation, treat it as an event you must respond to — read the result, decide whether action is needed, and reply (act, escalate via `notify`, or acknowledge). Never skip past it silently or fold it into an unrelated turn.
