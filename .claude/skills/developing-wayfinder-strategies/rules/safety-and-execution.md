# Safety and execution patterns

## Wallets and keys

- Local dev wallets live in `config.json` under the `wallets` key (gitignored).
- Strategies match wallets by label: label == strategy directory name.
- Never commit private keys or live credentials.

## Execution in this repo

EVM execution happens through:
- `wayfinder_paths/core/utils/transaction.py` (`send_transaction` waits for receipts)
- `wayfinder_paths/core/utils/tokens.py` (tx building + `ensure_allowance` for ERC20 approvals)
- `wayfinder_paths/core/utils/web3.py` (`web3_from_chain_id` RPC wiring)

For Claude Code interactive execution, the MCP `execute(...)` tool is the preferred gateway:
- `wayfinder_paths/mcp/tools/execute.py`

Common hazards:
- Unit mismatches (human units vs raw units)
- ERC20 approvals (some tokens require allowance reset to 0 before approve)
- Recipient vs sender mismatch for sends/swaps

## Claude Code MCP safety boundary

This repo ships a small MCP server + a `PreToolUse` review hook:
- Prefer the MCP tools for anything with side effects:
  - `execute(...)` for EVM sends/swaps (review prompt + structured preview).
  - `hyperliquid_execute(...)` for Hyperliquid perp orders/leverage (review prompt + structured preview).
  - `run_script(...)` for one-off local scripts in `.wayfinder_runs/` (review prompt + script excerpt).
- Server-side revalidation still applies; never rely on client-side prompting alone.
