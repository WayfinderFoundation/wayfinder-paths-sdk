# Hyperliquid reads (market data + time series)

## Data accuracy (no guessing)

- Do **not** invent funding rates or prices. Always fetch using the adapter (or MCP `hyperliquid(...)`) and label timestamps.
- If Hyperliquid data calls fail, return “unavailable” and include the exact call that failed.

## Primary data source

- Adapter: `wayfinder_paths/adapters/hyperliquid_adapter/adapter.py`

This adapter wraps the `hyperliquid` SDK `Info` client for read paths.

## High-value reads

### Market search / discovery (preferred)

- Call: `hyperliquid_search_markets(query, market_type="both", limit=50)`
- Use this first for broad questions like “oil futures”, “BTC markets”, “HYPE spot”, or “energy perps”.
- It fetches the current HL universe and fuzzily scores every perp/spot market name.
- It is intentionally high-recall: treat low-confidence rows as candidates to inspect, not as confirmed user intent.
- It handles common HL wrappers/aliases such as `bonk → kBONK`, `sol/solana → uSOL`, `btc → UBTC`, and `eth → UETH`.
- Use `market_type="perp"`, `"spot"`, or `"both"` to bound the result set.
- Only call `hyperliquid_get_markets()` when the user explicitly asks for the full universe or search results are insufficient.

### Perp market metadata + contexts

- Call: `HyperliquidAdapter.get_meta_and_asset_ctxs()`
- Output: `[meta, assetCtxs]` (SDK-native shape)
- Typical use:
  - enumerate perp markets
  - map `asset_id ↔ coin` and extract risk/margin fields from contexts

### Funding history (time series)

Important: `HyperliquidAdapter` does **not** implement `get_funding_history(...)`.

Use one of:
- **Wayfinder API** (preferred for strategy analytics): `HyperliquidDataClient.get_funding_history(coin, start_ms, end_ms)`
  - Client: `wayfinder_paths/core/clients/HyperliquidDataClient.py` (`HYPERLIQUID_DATA_CLIENT`)
- **Hyperliquid SDK (direct)**: `adapter.info.funding_history(name, startTime, endTime)` (milliseconds)
  - Note: this is the SDK `Info` client method (not async). It returns rows containing `time` and `fundingRate`.

### Spot metadata

- Call: `HyperliquidAdapter.get_spot_meta()`
- Output: dict with at least:
  - `tokens: list[...]`
  - `universe: list[...]` (pairs)

### Spot assets mapping (good for discovery)

- Call: `HyperliquidAdapter.get_spot_assets()`
- Output: mapping like `{ "HYPE/USDC": 10107, ... }`

### Order books

- Perp/spot by coin string:
  - Call: `HyperliquidAdapter.get_l2_book(coin)`
- Spot by asset id:
  - Call: `HyperliquidAdapter.get_spot_l2_book(spot_asset_id)`

### Account state

Three MCP resources, one per asset surface (split so the agent doesn't have to disambiguate):

- `wayfinder://hyperliquid/{label}/state` — perp clearinghouse (margin summary, asset positions, withdrawable).
- `wayfinder://hyperliquid/{label}/spot` — **pure spot only** (USDC / HYPE / USDH / …). `+N` HIP-4 outcome entries are filtered out at the resource layer.
- `wayfinder://hyperliquid/{label}/outcomes` — outcome positions only (`+N` entries with non-zero total), parsed `outcome_id` / `side`. See `rules/outcomes.md`.

Adapter calls (raw, no filtering — both still expose outcome `+N` entries on the spot side):

- Perp account state: `HyperliquidAdapter.get_user_state(address)`
- Spot balances: `HyperliquidAdapter.get_spot_user_state(address)`
- Orders/fills:
  - `get_frontend_open_orders(address)` (rich response: order type, trigger info, cloid, original size, etc.)
  - `get_open_orders(address)` (delegates to `get_frontend_open_orders`)
  - `get_user_fills(address)`
  - `get_order_status(address, order_id)`
