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

- **Quote before swap (MANDATORY):** Before calling `mcp__wayfinder__core_execute(kind="swap")`, always call `mcp__wayfinder__onchain_quote_swap` first. Verify the resolved `from_token` and `to_token` (symbol, address, chain) match intent, then show the user the route, estimated output, and fee. Only proceed to `execute` after the user confirms — unless the user has explicitly said to skip quoting (e.g. "just do it", "skip the quote").
- **Route planning for non-trivial swaps:** Before quoting, assess whether a direct route is likely to exist between the two tokens. If the pair is illiquid, cross-chain, or involves a long-tail token, reason through candidate intermediate hops first (e.g. tokenA → USDC → tokenB, or tokenA → native gas token → tokenB). Quote the most promising paths and compare outputs before presenting to the user. Skip this planning step only for well-known liquid pairs on the same chain (e.g. ETH → USDC on Arbitrum).
- On-chain writes: use MCP `execute(...)` (swap/send). The hook shows a human-readable preview and asks for confirmation.
- Arbitrary EVM contract interactions: use MCP `contract_call(...)` (read-only) and `contract_execute(...)` (writes, gated by a review prompt).
  - ABI handling: pass a minimal `abi`/`abi_path` when you can. If omitted, the tools fall back to fetching the ABI from Etherscan V2 (requires `system.etherscan_api_key` or `ETHERSCAN_API_KEY`, and the contract must be verified). If the target is a proxy, tools attempt to resolve the implementation address and fetch the implementation ABI.
  - To fetch an ABI directly (without making a call), use MCP `contract_get_abi(...)`.
- Hyperliquid perp writes: use MCP `hyperliquid_execute(...)` (orders/leverage). Also gated by a review prompt.
- Polymarket writes: use MCP `polymarket_execute(...)` (bridge deposit/withdraw, buy/sell, limit orders, redemption). Also gated by a review prompt.
- Contract deploys: use MCP `deploy_contract(...)` (compile + deploy + verify). Also gated by a review prompt. Use `compile_contract(...)` for compilation only (read-only, no confirmation).
  - Deployments (and other contract actions) are recorded in wallet profiles. Call `core_get_wallets(label="...")` and look at `profile.transactions` entries with `protocol: "contracts"` (also written to `.wayfinder_runs/wallet_profiles.json`).
  - **Artifact persistence:** Source code, ABI, and metadata are saved to `.wayfinder_runs/contracts/{chain_id}/{address}/` and survive scratch directory cleanup. Browse with `contracts_list()` (list all) or `contracts_get(chain_id, address)` (specific contract — includes ABI).
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

## Contract development

Before writing or deploying Solidity contracts, invoke `/contract-development`.

## Data accuracy (no guessing)

When answering questions about **rates/APYs/funding**:

- Never invent or estimate values.
- Always fetch the value via an adapter/client/tool call when possible.
- Before searching external docs, consult this repo's own adapters/clients (and their `manifest.yaml` + `examples.json`) first.
- If you cannot fetch it (auth/network/tooling), say so explicitly and provide the exact call/script needed to fetch it.

## Alpha Lab (alpha discovery)

Alpha Lab is a **scored alpha insight feed** that surfaces actionable DeFi signals (tweets, chain flows, APY highlights, delta-neutral pairs). Read-only — discovery only, no execution.

MCP tools: `research_get_alpha_types()`, `research_search_alpha(query, scan_type, ...)`. Python client: `ALPHA_LAB_CLIENT.search(...)`. Load `/using-alpha-lab` for full method signatures, scan types, and ranking semantics.

## Delta Lab (yield discovery)

**ALWAYS load `/using-delta-lab` before any yield, basis, APY, delta-neutral, lending-rate, perp-funding, or opportunity-screening work — and before writing any script that touches `DELTA_LAB_CLIENT`.** Don't guess at method names.

- **APY format:** decimal floats (`0.98 = 98%`, NOT 0.98%). Multiply by 100 to display.
- **MCP tools** are quick snapshots only — `research_get_basis_symbols`, `research_get_top_apy`, `research_get_basis_apy_sources`, `research_get_asset_basis_info`, `research_search_delta_lab_assets`, `research_search_price`, `research_search_lending`, `research_search_perp`, `research_search_borrow_routes`. Anything time-series, by-asset-id, plotting, multi-day, or bulk → use the `DELTA_LAB_CLIENT` Python client (see the skill).

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
- if the applet renders an execution result, render the run snapshot artifact instead of re-fetching browser data; if it intentionally shows live browser data, label it as `Applet: live as of HH:MM`
- post `wf:state` with `state._meta.dataUpdatedAt` (ms epoch) whenever displayed applet data changes so hosts can show freshness next to CLI/run output
- treat non-200 responses, especially `404`, as expected unavailability:
  - show a clear "data unavailable" or "waiting for host API" state
  - do not crash the applet on missing data
- ensure every referenced static resource is present under `applet/dist/`
- include explicit icon tags (`icon`, `shortcut icon`, `apple-touch-icon`) in the applet HTML to avoid implicit browser 404s for missing favicon resources
- do not call `/api/v1/delta-lab/symbols/`; that route does not exist for pack applets

## Running strategies via MCP

When a user asks to run, check, or interact with a strategy:

1. **Always discover first** - Use MCP resource `core_get_adapters_and_strategies()` to list available strategies before attempting to run one. Strategy names use `snake_case` (e.g., `boros_hype_strategy`, not `hype_boros_strategy`).

2. **Standard strategy interface** - All strategies implement these actions via `mcp__wayfinder__core_run_strategy`:

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
   → core_get_adapters_and_strategies()  # Find exact name
   → run_strategy(strategy="boros_hype_strategy", action="status")

   # User: "what's the expected APY for the moonwell strategy?"
   → run_strategy(strategy="moonwell_wsteth_loop_strategy", action="quote")

   # User: "withdraw from the strategy"
   → run_strategy(strategy="boros_hype_strategy", action="withdraw")
   # Triggers safety review: "Withdraw from boros_hype_strategy"

   # User: "deposit $100 into the strategy"
   → run_strategy(strategy="boros_hype_strategy", action="deposit", main_token_amount=100.0, gas_token_amount=0.01)
   ```

4. **Don't guess strategy names** - If the user's name doesn't match exactly, use `core_get_adapters_and_strategies()` to find the correct name.

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
- **On-chain:** use `mcp__wayfinder__core_execute` (swap/send). The `amount` parameter is **human-readable** (e.g. `"5"` for 5 USDC), not wei.
- **Outcome / prediction markets — search both venues, let the user pick.** When a user mentions "outcome market" or "prediction market" without naming the platform, **search both venues in parallel** and present the candidates side-by-side so the user can choose where to trade. Two venues:
  - **Hyperliquid HIP-4** — daily binary price contracts, settled in USDH on the HL L1. Lineup rotates daily. Search via `mcp__wayfinder__hyperliquid_search_market(query=...)` and read the `outcomes` bucket.
  - **Polymarket** — long-form prediction markets (politics, sports, events, crypto milestones), settled in USDC.e on Polygon. Search via `mcp__wayfinder__polymarket_read(action="search", query=..., limit=...)`.

  Present results as a table grouped by venue, then ask the user which market to act on. Don't assume — the same theme (e.g. "BTC above X by date Y") can list on both venues with different sizes, expiries, and collateral.
- **Hyperliquid perps/spot/outcomes:** use `mcp__wayfinder__hyperliquid_execute` (market/limit, leverage, cancel; HIP-4 outcome markets via `place_outcome_order`). **Before your first `hyperliquid_execute` call in a session, invoke `/using-hyperliquid-adapter`** to load the MCP tool's required-parameter rules (`is_spot`, `leverage`, `usd_amount_kind`, outcome `outcome_id`/`side`, etc.). The skill covers both the MCP tool interface and the Python adapter.
- **Polymarket:** use `mcp__wayfinder__polymarket_read` (search/history) + `mcp__wayfinder__polymarket_get_state` (status) + `mcp__wayfinder__polymarket_execute` (bridge USDC↔USDC.e, buy/sell, limit orders, redeem). **Before your first Polymarket execution call in a session, invoke `/using-polymarket-adapter`** (USDC.e collateral + tradability filters + outcome selection).
- **Multi-step flows:** write a short Python script under `.wayfinder_runs/.scratch/<session_id>/` (see `$WAYFINDER_SCRATCH_DIR`) and execute it with `mcp__wayfinder__core_run_script`. Promote keepers into `.wayfinder_runs/library/<protocol>/` (see `$WAYFINDER_LIBRARY_DIR`).

### Complex transaction flow (multi-step or fund-moving)

For anything beyond a simple single swap, follow this checklist:

1. **Plan** — Break the transaction into ordered steps. Identify which chains, protocols, and tokens are involved. State the plan to the user before writing any code.
2. **Gather info** — Load the relevant protocol skill(s). Fetch current rates, balances, gas, and any addresses or parameters the script needs. Don't hardcode values you haven't verified.
3. **Quote all steps** — For every swap/bridge step, call `mcp__wayfinder__onchain_quote_swap` and collect the results. Then display a confirmation table to the user before executing anything:

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

- Search markets/events: `mcp__wayfinder__polymarket_read(action="search", query="bitcoin february 9", limit=10)`
- Full status (positions + PnL + balances + open orders): `mcp__wayfinder__polymarket_get_state(wallet_label="main")`
- Convert **native Polygon USDC (0x3c499c...) → USDC.e (0x2791..., required collateral)**: `mcp__wayfinder__polymarket_execute(action="bridge_deposit", wallet_label="main", amount=10)` (skip if you already have USDC.e)
- Buy shares (market order): `mcp__wayfinder__polymarket_execute(action="buy", wallet_label="main", market_slug="bitcoin-above-70k-on-february-9", outcome="YES", amount_usdc=2)`
- Close a position (sell full size): `mcp__wayfinder__polymarket_execute(action="close_position", wallet_label="main", market_slug="bitcoin-above-70k-on-february-9", outcome="YES")`
- Redeem after resolution: `mcp__wayfinder__polymarket_execute(action="redeem_positions", wallet_label="main", condition_id="0x...")`

Polymarket funding (USDC.e collateral):

- **Polygon USDC → USDC.e:** `polymarket_execute(action="bridge_deposit", amount=10)` converts native USDC (0x3c499c...) → USDC.e (0x2791...).
- **Already have USDC.e:** Trade immediately, skip `bridge_deposit`.
- **Funds on other chains:** BRAP swap to USDC.e: `execute(kind="swap", from_token="usd-coin-base", to_token="polygon_0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")`. Or use `bridge_deposit` with `from_chain_id` + `from_token_address` (see `PolymarketAdapter.bridge_supported_assets()`).

Sizing note (avoid ambiguity): if a user says "$X at Y× leverage", confirm whether `$X`is **notional** or **margin** (use`usd_amount_kind="notional"|"margin"`on`mcp**wayfinder**hyperliquid_execute`).

### MCP vs scripting — pick the right tool

Prefer **MCP tools** for simple, one-shot actions: a single quote, a single swap, reading a balance, placing one order, querying a strategy. They're already wired up, validated, and return structured results.

Reach for **scripts under `.wayfinder_runs/`** when the work is complex or repetitive: stitching multiple adapter calls together, fan-out across many wallets/chains, multi-step flows with conditional branches, or anything you'll want to re-run. Scripts can be scheduled via `runner(action="add_job", type="script", ...)` once they're stable.

Rough cut: if you can express it as one MCP call, use the MCP call. If you find yourself chaining three or more, write a script.

**Before writing any script, load `/writing-wayfinder-scripts`** — it covers `get_adapter()`, `web3_from_chain_id()`, and the common gotchas (clients vs adapters return shapes, async/await, ERC20 helpers, wei vs human amounts, funding-rate sign).

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

Runner MCP tool: `mcp__wayfinder__core_runner(action=...)`.

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

- **Before any on-chain operation**, check the wallet has native gas on that chain via `core_get_wallets(label="...")` and inspect the `balances` field.
- If bridging to a new chain for the first time: bridge gas first. If you need the native token ID, look it up via `onchain_fuzzy_search_tokens(chain_code, query)`.

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

Wallet info is exposed through MCP tools (resources were removed — opencode doesn't auto-load them).

**Quick reads:**

- `core_get_wallets()` — every wallet with profile, tracked protocols, and USD-aggregated per-chain balances inline.
- `core_get_wallets(label="main")` — same shape, single wallet.
- `onchain_get_wallet_activity(...)` — recent on-chain activity (best-effort).
- `contracts_list()` / `contracts_get(chain_id, address)` — locally-deployed contracts (ABI included on `_get`).

**Tool actions (`core_wallets`):**

- `create` — new wallet. On Wayfinder Shells, `remote=True` is mandatory.
- `annotate` — record a protocol interaction (internal use).
- `discover_portfolio` — query adapters for live positions.

**Automatic tracking:** Profiles auto-update when you call `core_execute`, `hyperliquid_execute`, or `core_run_script` with `wallet_label=...`.

**Portfolio discovery:**

- `core_wallets(action="discover_portfolio", wallet_label="main")` fetches positions across known protocols.
- Only queries protocols the wallet has previously interacted with.
- 3+ tracked protocols → returns a warning unless you pass `parallel=True`.
- Filter with `protocols=["hyperliquid"]` to query a subset.

**Manual annotation:** use `core_wallets(action="annotate", ...)` if a wallet has used a protocol not yet tracked.

**In Python scripts:** use `load_wallets()` / `find_wallet_by_label(label)` from `wayfinder_paths.core.utils.wallets` — same code path as `core_get_wallets`, returns remote wallets transparently. See `/writing-wayfinder-scripts`.
