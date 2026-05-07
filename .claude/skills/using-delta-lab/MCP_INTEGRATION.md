# Delta Lab MCP Integration

Delta Lab is exposed via the Wayfinder MCP server as a small set of **read-only research tools**.

## ⚠️ APY Value Format (CRITICAL)

**APY values are returned as decimal floats, NOT percentages:**

- `0.98` means **98% APY** (not 0.98%)
- `2.40` means **240% APY** (not 2.40%)
- `0.05` means **5% APY** (not 0.05%)

To display as percentage: **multiply by 100** (e.g., `apy * 100` = `98%`)

This applies to all Delta Lab outputs.

## MCP Tools

The MCP surface is intentionally narrow — snapshots only. Anything time-series, by-asset-id, plotting, or bulk requires the Python client (`DELTA_LAB_CLIENT`).

### 1. Top APY (All Symbols)
**Tool:** `research_get_top_apy(lookback_days, limit)`

**Purpose:** Get top APY opportunities across ALL basis symbols (not symbol-specific). Returns LONG opportunities covering all protocols: perps, Pendle PTs, Boros IRS, yield-bearing tokens, and lending.

**Parameters:**
- `lookback_days` - Days to average over (default `"7"`, min `"1"`)
- `limit` - Max opportunities to return (default `"50"`, max `"500"`)

**Examples:**
```python
# Default: 7-day lookback, top 50 across all symbols
research_get_top_apy()

# Custom: 14-day lookback, top 100
research_get_top_apy(lookback_days="14", limit="100")
```

### 2. APY Sources (Symbol-Specific)
**Tool:** `research_get_basis_apy_sources(basis_symbol, lookback_days, limit)`

**Parameters:**
- `basis_symbol` - Uppercase symbol (e.g., `"BTC"`, `"ETH"`, `"HYPE"`)
- `lookback_days` - Days to average over (default `"7"`, min `"1"`)
- `limit` - Max opportunities to return (default `"10"`, max `"1000"`)

**Examples:**
```python
# Default: 7-day lookback, top 10
research_get_basis_apy_sources(basis_symbol="BTC")

# Custom: 30-day lookback, top 100
research_get_basis_apy_sources(basis_symbol="BTC", lookback_days="30", limit="100")
```

### 3. Basis Symbols
**Tool:** `research_get_basis_symbols()`

Returns the list of available basis symbols.

### 4. Asset Basis Info
**Tool:** `research_get_asset_basis_info(symbol)`

**Example:**
```python
research_get_asset_basis_info(symbol="ETH")
```

### 5. Asset Search
**Tool:** `research_search_delta_lab_assets(query, chain, limit)`

**Purpose:** Find Delta Lab assets when you only know an approximate symbol/name (e.g. `sUSDai`, `wsteth`, `usdc`).

**Parameters:**
- `query` - Search term (symbol/name/address/coingecko_id/asset_id)
- `chain` - Optional chain filter (chain ID like `"8453"` or chain code like `"base"`). Use `"all"` for no filter (default).
- `limit` - Max results (default `"25"`, max `"200"`)

**Examples:**
```python
# Search across all chains
research_search_delta_lab_assets(query="sUSDai")

# Base only (chain code)
research_search_delta_lab_assets(query="usdc", chain="base")

# Base only + smaller limit
research_search_delta_lab_assets(query="usdc", chain="base", limit="10")
```

### 6. Screen Price
**Tool:** `research_search_price(sort, limit, basis)`

**Purpose:** Screen assets by price features — returns, volatility, drawdowns. Useful for quickly finding top movers or most volatile assets.

**Parameters:**
- `sort` - Column to sort by. Options: `price_usd`, `ret_1d`, `ret_7d`, `ret_30d`, `ret_90d`, `vol_7d`, `vol_30d`, `vol_90d`, `mdd_30d`, `mdd_90d`
- `limit` - Max rows to return (default `"100"`, max `"1000"`)
- `basis` - Basis symbol filter (e.g. `"ETH"`, `"BTC"`) or `"all"` for no filter

**Examples:**
```python
# Top 10 daily movers (all assets)
research_search_price(sort="ret_1d", limit="10")

# Most volatile ETH-basis assets (30d)
research_search_price(sort="vol_30d", limit="20", basis="ETH")
```

For exact asset-id filtering, use `DELTA_LAB_CLIENT.screen_price()` directly.

### 7. Screen Lending
**Tool:** `research_search_lending(sort, limit, basis)`

**Purpose:** Screen lending markets by surface features — supply/borrow APRs, TVL, utilization, z-scores. Frozen/paused markets are excluded by default.

**Parameters:**
- `sort` - Column to sort by. Options: `net_supply_apr_now`, `net_supply_mean_7d`, `net_supply_mean_30d`, `combined_net_supply_apr_now`, `net_borrow_apr_now`, `supply_tvl_usd`, `liquidity_usd`, `util_now`, `borrow_spike_score`
- `limit` - Max rows to return (default `"100"`, max `"1000"`)
- `basis` - Basis symbol filter (e.g. `"ETH"`) or `"all"` for no filter

**Examples:**
```python
# Top 20 lending rates across all assets
research_search_lending(sort="net_supply_apr_now", limit="20")

# Best ETH lending rates
research_search_lending(sort="net_supply_apr_now", limit="20", basis="ETH")

# Highest borrow spike scores (potential rate anomalies)
research_search_lending(sort="borrow_spike_score", limit="10")
```

**Client-only filters (use `DELTA_LAB_CLIENT.screen_lending()` for):**
- `venue` - Filter by venue name (e.g. "aave", "morpho", "moonwell")
- `min_tvl` - Minimum supply TVL in USD
- `exclude_frozen` - Toggle frozen/paused market exclusion (MCP always excludes)
- `asset_ids` - Exact asset IDs

### 8. Screen Perp
**Tool:** `research_search_perp(sort, limit, basis)`

**Purpose:** Screen perpetual markets by surface features — funding rates, basis, OI, volume.

**Parameters:**
- `sort` - Column to sort by. Options: `funding_now`, `funding_mean_7d`, `funding_mean_30d`, `basis_now`, `basis_mean_7d`, `basis_mean_30d`, `oi_now`, `volume_24h`, `mark_price`
- `limit` - Max rows to return (default `"100"`, max `"1000"`)
- `basis` - Basis symbol filter (e.g. `"BTC"`) or `"all"` for no filter

**Examples:**
```python
# Top 20 highest funding rates right now
research_search_perp(sort="funding_now", limit="20")

# ETH perps sorted by 30-day mean funding
research_search_perp(sort="funding_mean_30d", limit="20", basis="ETH")
```

**Client-only filters (use `DELTA_LAB_CLIENT.screen_perp()` for):**
- `venue` - Filter by venue name (e.g. "hyperliquid", "binance")
- `order` - Switch to ascending sort (MCP defaults to descending)
- `asset_ids` - Exact asset IDs

### 9. Screen Borrow Routes
**Tool:** `research_search_borrow_routes(sort, limit, basis, borrow_basis, chain_id)`

**Purpose:** Screen lending borrow routes (collateral → borrow) by route configuration (LTV, liquidation thresholds, debt ceilings).

**Parameters:**
- `sort` - Column to sort by. Options: `ltv_max`, `liq_threshold`, `liquidation_penalty`, `debt_ceiling_usd`, `venue_name`, `market_label`, `created_at`
- `limit` - Max rows to return (default `"100"`, max `"1000"`)
- `basis` - Collateral basis symbol filter (e.g. `"ETH"`) or `"all"` for no filter
- `borrow_basis` - Borrow basis symbol filter (e.g. `"USD"`) or `"all"` for no filter
- `chain_id` - Optional chain filter (chain ID like `"8453"` or chain code like `"base"`). Use `"all"` for no filter.

**Examples:**
```python
# ETH collateral -> USD borrow routes by max LTV
research_search_borrow_routes(sort="ltv_max", limit="50", basis="ETH", borrow_basis="USD")

# Screen across all collateral/borrow pairs
research_search_borrow_routes(sort="ltv_max", limit="100")

# Base chain only
research_search_borrow_routes(sort="ltv_max", limit="50", basis="ETH", borrow_basis="USD", chain_id="8453")
```

**Client-only filters (use `DELTA_LAB_CLIENT.screen_borrow_routes()` for):**
- `venue` - Filter by venue name
- `market_id` - Filter by market ID
- `topology` - Filter by route topology (e.g. "POOLED", "ISOLATED_PAIR")
- `mode_type` - Filter by route mode type (e.g. "BASE", "EMODE")

## Implementation Details

**File:** `wayfinder_paths/mcp/tools/delta_lab.py`

Async tool functions that wrap `DELTA_LAB_CLIENT` methods. All tools:
- Return dicts (JSON-serializable)
- Handle errors gracefully (return `{"error": "..."}`)
- Auto-uppercase basis symbols for consistency

**Server registration:** `wayfinder_paths/mcp/server.py`

## When to Use MCP Tools vs Direct Client

### Use MCP tools (interactive):
- Quick one-off queries in Claude conversation
- No script needed, immediate results
- Screening with sort + basis filter

### Use Direct Client (scripting):
- Extra filters: `venue`, `min_tvl`, `exclude_frozen`, `asset_ids`, `order`
- Complex filtering/processing logic
- Multiple API calls with transformations
- **Timeseries data as DataFrames** for plotting/analysis
- Delta-neutral pair discovery
- Per-asset-id queries / asset lookup by id / by-address
