# Hyperlend reads (markets + time series)

## Data accuracy (no guessing)

- Do **not** invent or “roughly estimate” APYs, borrow rates, staking yields, or token yields.
- Only report values that come from Hyperlend API responses (via `HyperlendClient` / `HyperlendAdapter`) or another explicit data source.
- If you can’t fetch data (missing auth/network), respond with “unavailable” and show the exact fetch call needed.

## Primary data sources

- Client: `wayfinder_paths/core/clients/HyperlendClient.py`
- Adapter: `wayfinder_paths/adapters/hyperlend_adapter/adapter.py`

## Authentication note

HyperlendClient methods call `_authed_request(...)` even for `/public/hyperlend/*` routes.
Plan for auth to be required via `config.json` or env vars.

## High-value reads

### Stable markets (opportunity list)

- Call: `HyperlendClient.get_stable_markets(chain_id, required_underlying_tokens?, buffer_bps?, min_buffer_tokens?, is_stable_symbol?)`
- Output: `list[StableMarket]` where each entry commonly includes:
  - `chain_id`, `token_address`, `symbol`, `name`
  - liquidity/buffer fields: `underlying_tokens`, `buffer_bps`, `min_buffer_tokens`

### User assets view (portfolio view)

- Call: `HyperlendClient.get_assets_view(chain_id, user_address)`
- Output: `AssetsView` containing:
  - `chain_id`, `user_address`, `assets: list[dict]`, optional `total_value`

### Lend rate history (time series)

- Call: `HyperlendClient.get_lend_rate_history(chain_id, token_address, lookback_hours)`
- Output: `LendRateHistory` containing:
  - `chain_id`, `token_address`, `lookback_hours`
  - `rates: list[dict]` (time series records)
