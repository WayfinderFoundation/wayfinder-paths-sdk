# BRAP execution opportunities (writes)

## Execution surfaces in this repo

### Swap by token IDs

- Call: `BRAPAdapter.swap_from_token_ids(...)`
- Inputs:
  - `from_token_id`, `to_token_id` (Wayfinder token ids)
  - `from_address` (sender)
  - `amount` (string, **raw base units**)
  - `slippage` (float, decimal fraction)
  - optional: `strategy_name` (for ledger tagging)
- Output:
  - On success: a ledger record (or a structured operation object) depending on ledger availability.

### Swap from a quote

- Call: `BRAPAdapter.swap_from_quote(from_token, to_token, from_address, quote, ...)`
- What it can do:
  - Build a tx dict from `quote["calldata"]`
  - Submit ERC20 approvals if needed
  - Broadcast the swap tx

## Safety rails

- Some tokens require clearing allowance to 0 before re-approving (handled in adapter).

## Claude Code MCP “single write gateway”

For interactive use in Claude Code:
- Use `mcp__wayfinder__execute` with `kind="swap"` so the review hook can prompt before execution.

