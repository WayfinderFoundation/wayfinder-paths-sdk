# Data sources (what to use, where it comes from)

## Golden rule

Strategies should call **adapters** for domain actions. Clients are low-level wrappers.

Data flow: `Strategy → Adapter → Client(s) → Network/API`

## Data accuracy (no guessing)

- Never invent or “ballpark” rates/APYs/funding, even if they seem stable.
- Prefer a concrete adapter/client/tool call. If you can’t fetch, say “unavailable” and show the exact call needed.
- Before searching external docs, consult this repo’s own adapter/client surfaces (and their `manifest.yaml` + `examples.json`) first.

## High-value public data sources (read-heavy)

- Token metadata + prices: `wayfinder_paths/core/clients/TokenClient.py`
  - `get_token_details(query)` → token metadata (chain, decimals, symbol) + optional price fields
  - `get_gas_token(chain_code)` → native gas token metadata

- Pools + APY/TVL (and DefiLlama merges): `wayfinder_paths/core/clients/PoolClient.py`
  - `get_pools(chain_id=..., project=...)` (broad discovery)
  - `get_pools_by_ids(pool_ids=[...])` (shortlist by ids)

- Quotes for swaps/bridges: `wayfinder_paths/core/clients/BRAPClient.py`
  - `get_quote(...)` → routes + best quote + calldata (read-only)

- Hyperlend market snapshots + time series: `wayfinder_paths/core/clients/HyperlendClient.py`
  - `get_stable_markets(...)`
  - `get_assets_view(...)`
  - `get_lend_rate_history(...)` (time series)

- Pendle PT/YT market discovery + time series: `wayfinder_paths/adapters/pendle_adapter/adapter.py`
  - `list_active_pt_yt_markets(...)` (normalized scanner output)
  - `fetch_market_history(...)` / `fetch_market_snapshot(...)`
  - `sdk_swap_v2(...)` (build swap payloads via Hosted SDK)

## Exchange-native data sources

- Hyperliquid market data + time series: `wayfinder_paths/adapters/hyperliquid_adapter/adapter.py`
  - Market meta + contexts, user state, orderbooks (see the `using-hyperliquid-adapter` skill).
  - Funding time series: use `HyperliquidDataClient.get_funding_history(...)` or the underlying SDK `Info.funding_history(...)` (not an adapter method).

- Boros market data: `wayfinder_paths/adapters/boros_adapter/adapter.py`
  - Markets → orderbook → APR quoting and tick math; see the `using-boros-adapter` skill.
