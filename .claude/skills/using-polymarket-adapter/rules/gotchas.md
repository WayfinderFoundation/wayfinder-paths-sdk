# Polymarket gotchas (avoid the common failures)

## Read vs write surfaces (MCP)

- Use `mcp__wayfinder__polymarket_read` for reads (search/markets/history) and `mcp__wayfinder__polymarket_get_state` for account state (positions/orders/activity/trades).
- Use `mcp__wayfinder__polymarket_execute` for writes (bridge, approvals, buy/sell, limit/cancel, redeem). It should always require a confirmation in Claude Code.
- Use `mcp__wayfinder__polymarket_read(action="quote", ...)` before a sized buy/sell when you need average execution from the current book.

## `price` is not `quote`

- `get_price(...)` / `mcp__wayfinder__polymarket_read(action="price", ...)` returns the current quoted price.
- `quote_market_order(...)` / `mcp__wayfinder__polymarket_read(action="quote", ...)` walks the live book and returns weighted-average execution, worst fill, and partial-fill status.
- For quote requests: `BUY` uses pUSD, `SELL` uses shares.

## pUSD vs USDC / USDC.e (collateral mismatch)

- Trading collateral is **pUSD** on Polygon, not native Polygon USDC.
- **USDC.e** is the direct wrap asset for pUSD on Polygon.
- If you only have Polygon USDC, use the adapter’s preparation flow to reach pUSD (see `rules/deposits-withdrawals.md`).

## Trading wallet ≠ owner EOA (V2 deposit wallet)

- Orders execute from a per-user **deposit wallet** (smart contract derived from owner EOA), not the owner EOA itself.
- Positions, pUSD collateral used for orders, and conditional shares all live on the deposit wallet — querying the owner EOA’s pUSD balance won’t reflect tradeable collateral.
- Use `adapter.deposit_wallet_address()` to get the trading address. `get_full_user_state(wallet_label=...)` reads from it automatically.
- Funding the deposit wallet is **explicit** — `polymarket_execute(action="fund_deposit_wallet", amount=...)`. Order placement does **not** auto-fund.
- See `rules/deposit-wallet.md` for the full pattern.

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

- CLOB open orders require Level-2 auth, which requires a configured signing wallet (local or remote).
- In MCP, pass `wallet_label="main"` to `mcp__wayfinder__polymarket_get_state(...)` to include open orders.

## Redemption requires the right `conditionId`

- Redemption uses ConditionalTokens `redeemPositions()` and depends on `conditionId` (from Gamma).
- Some markets use non-zero `parentCollectionId` or an adapter collateral wrapper; the adapter preflights and handles unwrap best-effort.
