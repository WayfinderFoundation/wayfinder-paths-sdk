# Wayfinder Paths SDK

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/wayfinder-paths.svg)](https://pypi.org/project/wayfinder-paths/)
[![Discord](https://img.shields.io/badge/discord-join-7289da.svg)](https://discord.gg/fUVwGMXjm3)

**An open-source SDK for building and managing automated DeFi strategies.**
It provides strategy abstractions, protocol adapters, and an MCP server for Claude Code.

## What is Wayfinder Paths?

Wayfinder Paths is a Python SDK that lets you:

- **Run DeFi strategies**: deposit, rebalance, withdraw, and exit across multiple chains
- **Build new paths**: create adapters and strategies for any protocol
- **Expose safe operations to Claude**: local MCP server for balances, swaps, perps, and strategy management

Think of it as programmable DeFi infrastructure that connects your wallets to yield strategies, perpetuals, lending markets, and cross-chain routers.

## Repository Layout

- `wayfinder_paths/core`: shared config, clients, constants, and utilities
- `wayfinder_paths/adapters`: protocol integrations (Moonwell, Hyperliquid, etc.)
- `wayfinder_paths/strategies`: strategy implementations and metadata
- `wayfinder_paths/mcp`: MCP server, tools, and resources for Claude Code
- `scripts/`: setup, wallet generation, and scaffolding helpers
- `tests/` and `wayfinder_paths/tests`: test suites

## Requirements

- Python 3.12
- Poetry (recommended)

## Quick Start

```bash
# Clone the repository
git clone https://github.com/WayfinderFoundation/wayfinder-paths.git
cd wayfinder-paths

# One-command setup (installs Poetry + deps, writes config.json, updates .mcp.json)
python3 scripts/setup.py

# Check strategy status
poetry run python -m wayfinder_paths.run_strategy boros_hype_strategy --action status --config config.json
```

### Manual Setup (if you don't want the bootstrap script)

```bash
poetry install
cp config.example.json config.json
# Edit config.json and set system.api_key

# Create a main wallet for local testing
poetry run python scripts/make_wallets.py -n 1
```

## Configuration

Use `config.example.json` as a template. The SDK reads `config.json` by default.

Key fields:

- `system.api_key`: Wayfinder API key (or set `WAYFINDER_API_KEY` env var)
- `system.api_base_url`: API base URL (defaults to `https://wayfinder.ai/api` if omitted)
- `strategy.rpc_urls`: chain ID -> RPC URL(s) (string or list)
- `wallets`: local wallets with `label`, `address`, and `private_key_hex`

Example:

```json
{
  "system": {
    "api_base_url": "https://strategies.wayfinder.ai/api/v1",
    "api_key": "wk_your_api_key_here"
  },
  "strategy": {
    "rpc_urls": {
      "1": ["https://eth.llamarpc.com"],
      "8453": ["https://mainnet.base.org"],
      "42161": ["https://arb1.arbitrum.io/rpc"],
      "999": ["https://rpc.hyperliquid.xyz/evm"]
    }
  },
  "wallets": [
    {
      "label": "main",
      "address": "0x...",
      "private_key_hex": "0x..."
    }
  ]
}
```

For detailed config documentation, see `CONFIG_GUIDE.md`.

### Supported Chains

The SDK includes built-in support for these chain IDs:

| Chain    | ID    | Code       |
| -------- | ----- | ---------- |
| Ethereum | 1     | `ethereum` |
| Base     | 8453  | `base`     |
| Arbitrum | 42161 | `arbitrum` |
| Polygon  | 137   | `polygon`  |
| BSC      | 56    | `bsc`      |
| HyperEVM | 999   | `hyperevm` |

## Strategies

The repository ships with several strategies. Each strategy folder contains a README with details.

| Strategy (directory) | Summary | Primary Chain | Status | Docs |
| --- | --- | --- | --- | --- |
| `stablecoin_yield_strategy` | USDC yield optimization across Base pools | Base | WIP | `wayfinder_paths/strategies/stablecoin_yield_strategy/README.md` |
| `hyperlend_stable_yield_strategy` | HyperLend stablecoin allocator | HyperEVM | Stable | `wayfinder_paths/strategies/hyperlend_stable_yield_strategy/README.md` |
| `moonwell_wsteth_loop_strategy` | Leveraged wstETH carry trade | Base | Stable | `wayfinder_paths/strategies/moonwell_wsteth_loop_strategy/README.md` |
| `basis_trading_strategy` | Delta-neutral funding rate capture | Hyperliquid | Stable | `wayfinder_paths/strategies/basis_trading_strategy/README.md` |
| `boros_hype_strategy` | HYPE yield with Boros + Hyperliquid hedging | Multi-chain | Stable | `wayfinder_paths/strategies/boros_hype_strategy/README.md` |

> **Note:** WIP (work-in-progress) strategies may have incomplete features or known issues. Running them via MCP will show a warning but execution is not blocked.

## Adapters

Adapters live in `wayfinder_paths/adapters` and encapsulate protocol-specific logic:

- `BalanceAdapter` (wallet balances + transfers)
- `BRAPAdapter` (cross-chain swaps + bridges)
- `BorosAdapter` (Boros lending positions)
- `HyperliquidAdapter` (perps, spot, deposits, withdrawals)
- `HyperlendAdapter` (HyperLend stable lending)
- `MoonwellAdapter` (Moonwell lending/borrowing)
- `PendleAdapter` (PT/YT and hosted SDK operations)
- `MulticallAdapter` (batch contract calls)
- `LedgerAdapter` (transaction recording)
- `TokenAdapter` (token metadata + pricing)
- `PoolAdapter` (pool analytics)

## CLI Reference

Run strategies from the CLI via `wayfinder_paths.run_strategy`:

```bash
# Status
poetry run python -m wayfinder_paths.run_strategy boros_hype_strategy --action status --config config.json

# Deposit
poetry run python -m wayfinder_paths.run_strategy boros_hype_strategy --action deposit \
  --main-token-amount 100 --gas-token-amount 0.01 --config config.json

# Update / Exit / Withdraw
poetry run python -m wayfinder_paths.run_strategy boros_hype_strategy --action update --config config.json
poetry run python -m wayfinder_paths.run_strategy boros_hype_strategy --action exit --config config.json
poetry run python -m wayfinder_paths.run_strategy boros_hype_strategy --action withdraw --config config.json

# Analyze / Quote (if supported by the strategy)
poetry run python -m wayfinder_paths.run_strategy boros_hype_strategy --action analyze --main-token-amount 1000
poetry run python -m wayfinder_paths.run_strategy boros_hype_strategy --action quote --amount 100

# Run continuously (loop interval defaults to 60s)
poetry run python -m wayfinder_paths.run_strategy boros_hype_strategy --action run --interval 300
```

## Claude MCP Integration

The repo includes an MCP server for Claude Code (see `.mcp.json`).
Start it with:

```bash
poetry run python -m wayfinder_paths.mcp.server
```

### MCP Tools (actions)

| Tool | Description |
| --- | --- |
| `quote_swap` | Quote swaps without executing |
| `execute` | Execute swaps, transfers, and Hyperliquid deposits |
| `hyperliquid` | Read-only Hyperliquid market/user data |
| `hyperliquid_execute` | Place orders, update leverage, withdraw |
| `run_strategy` | Status, policy, and strategy actions |
| `run_script` | Execute a local Python script inside `.wayfinder_runs/` |
| `wallets` | Create or list local wallets |

### MCP Resources (read-only)

- `wayfinder://adapters` and `wayfinder://adapters/{name}`
- `wayfinder://strategies` and `wayfinder://strategies/{name}`
- `wayfinder://wallets` and `wayfinder://wallets/{label}`
- `wayfinder://balances/{label}` and `wayfinder://activity/{label}`
- `wayfinder://tokens/resolve/{query}`
- `wayfinder://tokens/gas/{chain_code}`
- `wayfinder://tokens/search/{chain_code}/{query}`
- `wayfinder://hyperliquid/{label}/state`
- `wayfinder://hyperliquid/{label}/spot`
- `wayfinder://hyperliquid/prices` and `wayfinder://hyperliquid/prices/{coin}`
- `wayfinder://hyperliquid/markets`
- `wayfinder://hyperliquid/spot-assets`
- `wayfinder://hyperliquid/book/{coin}`

## Scripts and Helpers

- `scripts/setup.py`: bootstrap Poetry, config, wallets, and MCP
- `scripts/make_wallets.py`: create local dev wallets (optionally keystores)
- `scripts/create_strategy.py`: scaffold a new strategy from templates

`justfile` shortcuts (requires `just`):

```bash
just lint
just format
just test
just test-smoke
just create-strategy "My Strategy Name"
just create-wallets
just create-wallet stablecoin_yield_strategy
```

## Contributing

We welcome contributions!

### Add a New Strategy

```bash
just create-strategy "My Strategy Name"
# or
poetry run python scripts/create_strategy.py "My Strategy Name"
```

Implement:

- `deposit()`
- `update()`
- `exit()`
- `_status()`

### Add a New Adapter

```bash
cp -r wayfinder_paths/templates/adapter wayfinder_paths/adapters/my_adapter
```

Implement protocol-specific methods and return `(success, data)` tuples.

### Tests and Style

- Tests: `poetry run pytest -v`
- Smoke tests: `poetry run pytest -k smoke -v`
- Adapter/strategy tests: `just test-adapter <name>` / `just test-strategy <name>`
- Lint/format: `just lint` and `just format`

More details in `TESTING.md`.

## Security Notes

- **Never commit `config.json`** (contains private keys)
- **Use test wallets** for development
- **RPCs are required**: set `strategy.rpc_urls` for each chain you use

## Community

- [Discord](https://discord.gg/fUVwGMXjm3)
- [GitHub Issues](https://github.com/WayfinderFoundation/wayfinder-paths/issues)
- [Wayfinder](https://wayfinder.ai)

## License

MIT License - see [LICENSE](LICENSE) for details.
