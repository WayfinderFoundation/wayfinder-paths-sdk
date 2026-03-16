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

## Claude Code MCP + Skills

This repo ships a project-scoped MCP server (`.mcp.json`), a safety review hook (`.claude/settings.json`) that forces confirmation before fund-moving calls, Claude Code skills under `.claude/skills/`, and a local gitignored runs directory at `.wayfinder_runs/` for one-off scripts.

Simulation / scenario testing (vnet only):

- Before broadcasting complex fund-moving flows live, run at least one forked **dry-run scenario** (Gorlami). Use `/simulation-dry-run` for full details.
- **Cross-chain:** For flows spanning multiple EVM chains, spin up a fork per chain. See `/simulation-dry-run` for the pattern.
- **Scope:** Vnets only cover EVM chains. Off-chain or non-EVM protocols like Hyperliquid **cannot** be simulated.

## Safety Defaults

- **Quote before swap (MANDATORY):** Before calling `mcp__wayfinder__execute(kind="swap")`, always call `mcp__wayfinder__quote_swap` first. Verify the resolved `from_token` and `to_token` (symbol, address, chain) match intent, then show the user the route, estimated output, and fee. Only proceed to `execute` after the user confirms — unless the user has explicitly said to skip quoting (e.g. "just do it", "skip the quote").
- **Route planning for non-trivial swaps:** Before quoting, assess whether a direct route is likely to exist between the two tokens. If the pair is illiquid, cross-chain, or involves a long-tail token, reason through candidate intermediate hops first. Quote the most promising paths and compare outputs before presenting to the user. Skip this planning step only for well-known liquid pairs on the same chain.
- On-chain writes: use MCP `execute(...)` (swap/send). The hook shows a human-readable preview and asks for confirmation.
- Arbitrary EVM contract interactions: use MCP `contract_call(...)` (read-only) and `contract_execute(...)` (writes, gated by a review prompt).
  - ABI handling: pass a minimal `abi`/`abi_path` when you can. If omitted, the tools fall back to fetching the ABI from Etherscan V2 (requires `system.etherscan_api_key` or `ETHERSCAN_API_KEY`, and the contract must be verified). If the target is a proxy, tools attempt to resolve the implementation address and fetch the implementation ABI.
  - To fetch an ABI directly (without making a call), use MCP `contract_get_abi(...)`.
- Hyperliquid perp writes: use MCP `hyperliquid_execute(...)` (orders/leverage). Also gated by a review prompt.
- Polymarket writes: use MCP `polymarket_execute(...)` (bridge deposit/withdraw, buy/sell, limit orders, redemption). Also gated by a review prompt.
- Contract deploys: use MCP `deploy_contract(...)` (compile + deploy + verify). Also gated by a review prompt. Use `compile_contract(...)` for compilation only (read-only, no confirmation).
  - Deployments are recorded in wallet profiles. Browse with `wayfinder://contracts` or `wayfinder://contracts/{chain_id}/{address}`.
  - **Artifact persistence:** Source code, ABI, and metadata are saved to `.wayfinder_runs/contracts/{chain_id}/{address}/`.
- One-off local scripts: use MCP `run_script(...)` (gated by a review prompt) and keep scripts under `.wayfinder_runs/`.
- **Hyperliquid minimums:** $5 min deposit (below = lost), $10 min order notional.

## Transaction Outcome Rules

- A transaction is only successful if the on-chain receipt has `status=1`.
- The SDK raises `TransactionRevertedError` when a receipt returns `status=0` (often includes `gasUsed`/`gasLimit` and may indicate out-of-gas).
- If a fund-moving step fails/reverts, stop the flow and report the error; don't continue executing dependent steps.

## Protocol Skills (load before using adapters)

Before writing scripts or using adapters for a specific protocol, **invoke the relevant skill** to load usage patterns and gotchas:

| Protocol              | Skill                            |
| --------------------- | -------------------------------- |
| Moonwell              | `/using-moonwell-adapter`        |
| Aave V3               | `/using-aave-v3-adapter`         |
| Morpho                | `/using-morpho-adapter`          |
| Pendle                | `/using-pendle-adapter`          |
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

Skills contain rules for correct method usage, common gotchas, and high-value read patterns. **Always load the skill first** — don't guess at adapter APIs.

## Data Accuracy (no guessing)

When answering questions about **rates/APYs/funding**:

- Never invent or estimate values.
- Always fetch the value via an adapter/client/tool call when possible.
- Before searching external docs, consult this repo's own adapters/clients (and their `manifest.yaml` + `examples.json`) first.
- If you cannot fetch it (auth/network/tooling), say so explicitly and provide the exact call/script needed to fetch it.

## Supported Chains

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

## Architecture

```
Strategy → Adapter → Client(s) → Network/API
```

- `wayfinder_paths/core/` — Core engine (clients, base classes, services)
- `wayfinder_paths/adapters/` — Community-contributed protocol integrations
- `wayfinder_paths/strategies/` — Community-contributed trading strategies
- `wayfinder_paths/mcp/` — MCP server + scripting helpers
- `.wayfinder_runs/` — Local gitignored runs directory (scratch scripts, library scripts, contracts)

Common commands:

```bash
poetry install                         # Install dependencies
just create-wallets                    # Generate test wallets
just test-smoke                        # Run smoke tests
just test-strategy <name>              # Test specific strategy
just test-adapter <name>               # Test specific adapter
just test-cov                          # All tests with coverage
just lint                              # Ruff lint (--fix)
just format                            # Ruff format
just validate-manifests                # Validate all manifests
just create-strategy "My Strategy"     # Scaffold new strategy + wallet
just create-adapter "my_protocol"      # Scaffold new adapter
just publish                           # Publish to PyPI (main only)
poetry run python -m wayfinder_paths.run_strategy <name> --action status --config config.json
```

Full reference: @.claude/docs/architecture.md

## Scripting

Before writing any `.wayfinder_runs/` script, read:
- @.claude/reference/scripting-gotchas.md
- @.claude/reference/execution-modes.md

## Key Docs

- Execution modes & strategy interface: @.claude/reference/execution-modes.md
- Scripting gotchas: @.claude/reference/scripting-gotchas.md
- Architecture deep dive: @.claude/docs/architecture.md
