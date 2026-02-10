# Hyperliquid reads (market data + time series)

## Data accuracy (no guessing)

- Do **not** invent funding rates or prices. Always fetch using the adapter (or MCP `hyperliquid(...)`) and label timestamps.
- If Hyperliquid data calls fail, return “unavailable” and include the exact call that failed.

## Primary data source

- Adapter: `wayfinder_paths/adapters/hyperliquid_adapter/adapter.py`

This adapter wraps the `hyperliquid` SDK `Info` client for read paths.

## High-value reads

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

- Perp account state: `HyperliquidAdapter.get_user_state(address)`
- Spot balances: `HyperliquidAdapter.get_spot_user_state(address)`
- Orders/fills:
  - `get_frontend_open_orders(address)` (rich response: order type, trigger info, cloid, original size, etc.)
  - `get_open_orders(address)` (delegates to `get_frontend_open_orders`)
  - `get_user_fills(address)`
  - `get_order_status(address, order_id)`
