# Polymarket gotchas (avoid the common failures)

## Read vs write surfaces (MCP)

- Use `mcp__wayfinder__polymarket` for reads (search/markets/history/status).
- Use `mcp__wayfinder__polymarket_execute` for writes (bridge, approvals, buy/sell, limit/cancel, redeem). It should always require a confirmation in Claude Code.

## USDC vs USDC.e (collateral mismatch)

- Trading collateral is **USDC.e** on Polygon (`0x2791…4174`), not native Polygon USDC (`0x3c49…3359`).
- If you only have USDC, convert to USDC.e first (see `rules/deposits-withdrawals.md`).

## Market is “found” but not tradable

Always filter search results:

- `enableOrderBook` must be true
- `clobTokenIds` must exist
- `acceptingOrders` must be true
- `active` must be true and `closed` must not be true

Fallback to `list_markets(... order="volume24hr" ...)` when fuzzy search returns stale/closed items.

## Outcomes are not always YES/NO

- Many markets are multi-outcome (sports/player props).
- Use `resolve_clob_token_id(..., outcome="<string>")` when possible.
- In agent flows, add a robust fallback: `outcome=0` (first outcome) when “YES” doesn’t exist.

## Gamma field shapes (JSON strings)

Gamma frequently returns these fields as **JSON-encoded strings**:

- `outcomes`, `outcomePrices`, `clobTokenIds`

The adapter normalizes them into Python lists, but if you bypass the adapter and hit Gamma directly, you must parse them.

## Price history limitations

- `prices-history` is best-effort; some markets may have sparse history at certain fidelities/intervals.
- If you need deeper history, use the Data API `trades` endpoint and build candles locally.

## Rate limiting (429) and analysis scans

- Don’t fire hundreds of `prices-history` calls concurrently.
- Use a semaphore (e.g. 4–8 concurrent requests) and retry/backoff on failures.

## “Buy then immediately sell” can fail

- CLOB settlement/match can lag; you may not have shares available to sell instantly.
- Wait for the buy response’s `transactionsHashes[0]` confirmation before SELL if you’re doing automated round-trips.

## Token IDs aren’t ERC20 addresses

- `clobTokenIds` are CLOB market identifiers (strings), not token contract addresses.
- Outcome shares are ERC1155 positions under ConditionalTokens (on-chain).

## Open orders require the signing wallet

- CLOB open orders require Level-2 auth, which requires a configured signing wallet (`private_key_hex`).
- In MCP, pass `wallet_label="main"` to `mcp__wayfinder__polymarket(action="status", ...)` to include open orders.

## Redemption requires the right `conditionId`

- Redemption uses ConditionalTokens `redeemPositions()` and depends on `conditionId` (from Gamma).
- Some markets use non-zero `parentCollectionId` or an adapter collateral wrapper; the adapter preflights and handles unwrap best-effort.
