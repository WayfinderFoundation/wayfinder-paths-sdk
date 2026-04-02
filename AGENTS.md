# AGENTS.md

This file provides guidance when working with code in this repository.

## First-Time Setup (Auto-detect)

**IMPORTANT: On every new conversation, check if setup is needed:**

1. Check if `config.json` exists in the repo root
2. If it does NOT exist, this is a first-time user. You MUST:
   - Tell the user: "Welcome to Wayfinder Paths! Let me set things up for you."
   - Run: `python3 scripts/setup.py`
   - After setup completes, ask the user: "Do you have a Wayfinder API key?"
     - If yes: Add it to `config.json` under `system.api_key`
     - If no: Direct them to **https://strategies.wayfinder.ai** to create an account and get one

3. If `config.json` exists but `system.api_key` is empty/missing:
   - Ask: "I see you haven't set up your API key yet. Do you have a Wayfinder API key?"
   - If yes: Help them add it to `config.json` under `system.api_key`
   - If no: Direct them to **https://strategies.wayfinder.ai** to get one

4. If everything is configured, proceed normally

## Project Overview

Wayfinder Paths is a Python 3.12 public SDK for community-contributed DeFi trading strategies and adapters. It provides the building blocks for automated trading: adapters (exchange/protocol integrations), strategies (trading algorithms), and clients (low-level API wrappers). In production it can be integrated with a separate execution service for hosted signing/execution.

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

## Running strategies

Strategy interface — all strategies implement these actions:

**Read-only actions:**
- `status` - Current positions, balances, and state
- `analyze` - Run strategy analysis with given deposit amount
- `snapshot` - Build batch snapshot for scoring
- `policy` - Get strategy policies
- `quote` - Get point-in-time expected APY for the strategy

**Fund-moving actions:**
- `deposit` - Add funds to the strategy (requires `main_token_amount`; optional `gas_token_amount`). First deposit should include `gas_token_amount` (e.g. `0.001`).
- `update` - Rebalance or execute the strategy logic
- `withdraw` - **Liquidate**: Close all positions and convert to stablecoins (funds stay in strategy wallet)
- `exit` - **Transfer**: Move funds from strategy wallet to main wallet (call after withdraw)

**Clarify withdraw vs exit** — these are separate steps:
- `withdraw` closes positions → `exit` transfers to main wallet
- "withdraw all" / "close everything" → run `withdraw` then `exit`
- "transfer remaining funds" (positions already closed) → just `exit`

**Mypy typing** - When adding or modifying Python code, ensure all _new/changed_ code is fully type-annotated and does not introduce new mypy errors.

Run strategies via CLI:
```bash
poetry run python -m wayfinder_paths.run_strategy <strategy_name> --action status --config config.json
```

## Execution modes (one-off vs recurring)

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
from wayfinder_paths.core.clients import DELTA_LAB_CLIENT, POOL_CLIENT, TOKEN_CLIENT

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

Runner CLI (project-local state in `./.wayfinder/runner/`):

```bash
poetry run wayfinder runner start
poetry run wayfinder runner add-job --name basis-update --type strategy --strategy basis_trading_strategy --action update --interval 600 --config ./config.json
poetry run wayfinder runner status | run-once | pause | resume | delete <job> | stop
```

See `RUNNER_ARCHITECTURE.md`.

## Common Commands

```bash
# Install dependencies
poetry install

# Generate test wallets (required before running tests/strategies)
just create-wallets

# Run all smoke tests
just test-smoke

# Test specific strategy or adapter
just test-strategy stablecoin_yield_strategy
just test-adapter pool_adapter

# Run all tests with coverage
just test-cov

# Lint and format
just lint
just format

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

### Manifests

Every adapter and strategy requires a `manifest.yaml` declaring capabilities, dependencies, and entrypoint. Manifests are validated in CI.

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
