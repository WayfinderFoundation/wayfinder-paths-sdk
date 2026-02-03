# BRAP reads (quotes + route data)

## Primary data sources

- Client: `wayfinder_paths/core/clients/BRAPClient.py`
- Adapter: `wayfinder_paths/adapters/brap_adapter/adapter.py`

## High-value read calls

### Quote (lowest-level)

- Call: `BRAPClient.get_quote(...)`
- Inputs (most important):
  - `from_token`, `to_token` (ERC20 addresses)
  - `from_chain`, `to_chain` (ints)
  - `from_wallet` (EVM address)
  - `from_amount` (string, **raw base units**, not human units)
  - optional: `slippage` (float, e.g. `0.005` for 0.5%)
- Output:
  - A quote payload that typically contains `quotes`, `best_quote`, and `calldata` for execution.
  - Treat response as schema-flexible; check keys before indexing.

### Best quote (adapter convenience)

- Call: `BRAPAdapter.best_quote(...)`
- Returns the “best_quote” object (a single route) or an error string.
  - If you pass `preferred_providers=[...]`, it will try to select the best route among those providers first.

### Route comparison (diagnostics)

BRAP route diagnostics are easiest by inspecting the raw quote response:

- Call: `BRAPClient.get_quote(...)`
- Inspect: `result["quotes"]` (all routes) vs `result["best_quote"]` (selected route)

## Claude Code MCP helper

If you’re exploring interactively, prefer:
- `mcp__wayfinder__quote_swap` (does token lookup + human→raw conversion + returns a preview + compact best-quote summary)
  - Use `include_calldata=true` only if you explicitly need calldata in the response (it can be large).

### Token identifiers (avoid ambiguous lookups)

Best practice: pass **canonical token ids** in the form `<asset>-<chain_code>`.

Examples:
- Native ETH on Arbitrum: `ethereum-arbitrum` (expects address `0x0000000000000000000000000000000000000000`)
- USDT0 on Arbitrum: `usdt0-arbitrum`

Also supported (when you know the contract): **chain-scoped address ids** in the form `<chain_code>_0x<address>`.

Examples:
- USDT0 contract on Arbitrum: `arbitrum_0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9`
- WETH contract on Arbitrum: `arbitrum_0x82af49447d8a07e3bd95bd0d56f35241523fbab1`

Notes:
- Inputs like `"ETH arbitrum"` / `"USDT0 arbitrum"` are accepted, but can be ambiguous (symbol/name searches can resolve to a wrapped/staked token). Always confirm what it resolved to in the response (`from_token.symbol`, `from_token.address`, `from_token.chain_id`).
- If you meant a specific ERC20 (e.g. wstETH), pass its token id (e.g. `wsteth-arbitrum`) or its address.
- If a chain-scoped address id resolves to a *different* address than the one you specified, treat that as ambiguous and switch to `<asset>-<chain_code>` or the raw `0x...` address.

### Recommended loop

1) Call `mcp__wayfinder__quote_swap(...)`
2) Inspect `from_token` / `to_token` in the response to ensure the right asset + chain.
3) Pass `suggested_execute_request` directly into `mcp__wayfinder__execute` (the only fund-moving tool).
