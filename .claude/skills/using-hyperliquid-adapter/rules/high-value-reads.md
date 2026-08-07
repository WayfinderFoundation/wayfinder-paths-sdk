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

`mcp__wayfinder__hyperliquid_get_state(label)` returns the whole account in one shot:

- `summary` — the ONLY place money appears; shape branches on `account_abstraction`:
  - `"unifiedAccount"` (one USDC ledger backing both perp margin and spot) → `unified_usdc_settled` (realized cash, including margin holds), `unified_usdc_settled_and_unrealized` (equity — the account value the HL UI shows; quote this when the user asks how much money they have), `unified_usdc_margin_used` (margin committed to open positions), `unified_usdc_margin_available` (equity − margin used; capacity for new cross positions — per-asset caps still come from `hyperliquid_get_trade_asset`), `unified_usdc_withdrawable` (settled − holds; what can actually leave right now), `unified_usdc_liquidation_floor` (cross liquidates when equity falls to this). Perp margin is held out of spot USDC, so a perp account value of ~0 with no open positions is normal, not "no funds".
  - `"default"` → `perp_account_value`, `perp_withdrawable`, `spot_usdc_total` (separate ledgers).
- `perp_positions` — open perp/HIP-3 positions (entry, size, leverage, liquidation price, unrealized PnL).
- `spot_positions` — non-zero, non-USDC spot balances (HYPE / USDH / …). The USDC row lives in `summary`.
- `outcome_positions` — HIP-4 outcome positions (`+N` entries with non-zero total), parsed `outcome_id` / `side`. See `rules/outcomes.md`.
- `open_orders` — every open order across all dexes, from `frontendOpenOrders`: resting limit orders AND untriggered trigger orders (`isTrigger`, `triggerPx`, `orderType`, `isPositionTpsl`, `reduceOnly`). No separate call needed to see stop losses / take profits.

Positions, open orders, and outcome positions each carry a canonical **`asset_name`** (`kBONK-USDC`, `xyz:NVDA`, `PURR/USDC`, `#40`) next to the raw HL `coin`. Feed `asset_name` straight into `hyperliquid_search_mid_prices` / `hyperliquid_get_trade_asset` / the order tools — it is the same format every HL tool speaks, so never re-derive the `-USDC` suffix from `coin` yourself.

For selected perp/HIP-3 trade capacity, use `mcp__wayfinder__hyperliquid_get_trade_asset(label, asset_name)`. This reads Hyperliquid `activeAssetData` and returns side-specific available margin, max order notional, max base size, current leverage, max leverage, compatible margin modes, and the live position. Do not derive available-to-open capacity from spot USDC balance, withdrawable, account value, or `crossMarginSummary`.

Adapter calls (raw, no filtering — both still expose outcome `+N` entries on the spot side):

- Perp account state: `HyperliquidAdapter.get_user_state(address)`
- Spot balances: `HyperliquidAdapter.get_spot_user_state(address)`
- Orders/fills:
  - `get_frontend_open_orders(address)` (rich response: order type, trigger info, cloid, original size, etc.)
  - `get_open_orders(address)` (delegates to `get_frontend_open_orders`)
  - `get_user_fills(address)`
  - `get_order_status(address, order_id)`
