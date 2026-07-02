# Hyperliquid reads (market data + time series)

## Data accuracy (no guessing)

- Do **not** invent funding rates or prices. Always fetch using the adapter, MCP
  `hyperliquid_get_state(...)` / `hyperliquid_search_mid_prices(...)`, or the
  read-only history tools below, and label timestamps.
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

### Candles and funding history (time series)

Important: `HyperliquidAdapter` does **not** implement historical candle or funding
helpers. Do not use `adapter.info`; adapter instances do not expose a stable public
`.info` handle.

Use one of:
- **MCP tools** (preferred in agent runs):
  - `hyperliquid_get_candles(asset_name="HYPE", interval="5m", lookback_hours=24)`
  - `hyperliquid_get_candles(asset_name="xyz:SPCX", interval="15m", lookback_hours=72)`
  - `hyperliquid_get_funding_history(asset_name="HYPE-USDC", lookback_hours=168)`
- **Wayfinder API/client** (preferred for scripts/strategy analytics):
  - `HyperliquidDataClient.get_candles(coin, start_ms, end_ms, interval="1h")`
  - `HyperliquidDataClient.get_funding_history(coin, start_ms, end_ms)`
  - Client: `wayfinder_paths/core/clients/HyperliquidDataClient.py` (`HYPERLIQUID_DATA_CLIENT`)

Candles return Hyperliquid raw field names: `t`, `T`, `o`, `h`, `l`, `c`, and
when available `v` (volume) and `n` (trade count). Do not expect
`open`/`high`/`low`/`close` unless you are reading chart-normalized rows.

Symbol rules:
- Core perp candles accept `HYPE` or `HYPE-USDC`; the backend normalizes to `HYPE`.
- HIP-3 / dex perps require the dex prefix, for example `xyz:SPCX`.
- Plain `SPCX` is not enough for candles unless a provider search first maps it
  to the canonical dex coin.

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

`mcp__wayfinder__hyperliquid_get_state(label)` returns all four account surfaces in one shot:

- `perp.state` — perp clearinghouse (margin summary, asset positions, withdrawable).
- `spot.state.balances` — pure spot balances (USDC / HYPE / USDH / …). `+N` HIP-4 outcome entries are filtered out into the `outcomes` bucket.
- `open_orders.orders` — every open order across all dexes, from `frontendOpenOrders`: resting limit orders AND untriggered trigger orders (`isTrigger`, `triggerPx`, `orderType`, `isPositionTpsl`, `reduceOnly`). No separate call needed to see stop losses / take profits.
- `outcomes.positions` — outcome positions only (`+N` entries with non-zero total), parsed `outcome_id` / `side`. See `rules/outcomes.md`.

For selected perp/HIP-3 trade capacity, use `mcp__wayfinder__hyperliquid_get_trade_asset(label, asset_name)`. This reads Hyperliquid `activeAssetData` and returns side-specific available margin, max order notional, max base size, current leverage, max leverage, compatible margin modes, and the live position. Do not derive available-to-open capacity from spot USDC balance, withdrawable, account value, or `crossMarginSummary`.

Adapter calls (raw, no filtering — both still expose outcome `+N` entries on the spot side):

- Perp account state: `HyperliquidAdapter.get_user_state(address)`
- Spot balances: `HyperliquidAdapter.get_spot_user_state(address)`
- Orders/fills:
  - `get_frontend_open_orders(address)` (rich response: order type, trigger info, cloid, original size, etc.)
  - `get_open_orders(address)` (delegates to `get_frontend_open_orders`)
  - `get_user_fills(address)`
  - `get_order_status(address, order_id)`
