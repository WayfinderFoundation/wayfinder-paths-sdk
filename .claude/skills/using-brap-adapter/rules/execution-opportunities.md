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
  - `to_address`: destination-chain wallet; required when bridging EVM → Solana
- Output:
  - On success: a ledger record (or a structured operation object) depending on ledger availability.

### Swap from a quote

- Call: `BRAPAdapter.swap_from_quote(from_token, to_token, from_address, quote, ...)`
- Pass the complete `BRAPClient` best quote or the MCP's `execution_quote`, not
  its compact preview. Use `onchain_swap` for Solana-source execution.
- What it can do:
  - Build a tx dict from `quote["calldata"]`
  - Submit the route's `prerequisite_transactions` in order, waiting for each
    successful receipt before continuing (including two-step Permit2 approvals)
  - Use the existing ERC20 allowance helper for routes without solver-managed approvals
  - Broadcast the swap tx

The same prerequisite handling applies to `onchain_swap`, including when
`wait_for_receipt=false`: only the final swap may skip its receipt wait. A failed
prerequisite stops execution. Direct Pons and Uniswap routes are supported when
the backend returns an executable quote; `atomic_calls` / `pons_v2_batch` routes
require a batch-capable executor and are rejected, never split into separate swaps.

## Safety rails

- Some tokens require clearing allowance to 0 before re-approving (handled in adapter).
- Preserve the quoted transaction's **value even when selling an ERC-20**: bridge
  routes may need native currency for relayer fees. Approve the quote's spender
  and keep its calldata intact; do not rebuild and broadcast from the hex data alone.

## Claude Code MCP “single write gateway”

For interactive use in Claude Code:
- Use `mcp__wayfinder__onchain_swap` so the review hook can prompt before execution.
