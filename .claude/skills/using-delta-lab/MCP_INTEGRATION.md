# Delta Lab MCP Integration

Delta Lab is now available in the Wayfinder MCP server as **read-only resources**.

## ⚠️ APY Value Format (CRITICAL)

**APY values are returned as decimal floats, NOT percentages:**

- `0.98` means **98% APY** (not 0.98%)
- `2.40` means **240% APY** (not 2.40%)
- `0.05` means **5% APY** (not 0.05%)

To display as percentage: **multiply by 100** (e.g., `apy * 100` = `98%`)

This applies to all Delta Lab endpoints: `top-apy`, `apy-sources`, `delta-neutral`, and `timeseries`.

## MCP Resources Added

### 1. Top APY (All Symbols)
**URI:** `wayfinder://delta-lab/top-apy/{lookback_days}/{limit}`

**Purpose:** Get top APY opportunities across ALL basis symbols (not symbol-specific). Returns LONG opportunities covering all protocols: perps, Pendle PTs, Boros IRS, yield-bearing tokens, and lending.

**Path Parameters:**
- `{lookback_days}` - Days to average over (default: "7", min: "1")
- `{limit}` - Max opportunities to return (default: "50", max: "500")

**Examples:**
```python
# Default: 7-day lookback, top 50 across all symbols
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/top-apy/7/50"
)

# Custom: 14-day lookback, top 100
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/top-apy/14/100"
)
```

### 2. APY Sources (Symbol-Specific)
**URI:** `wayfinder://delta-lab/{basis_symbol}/apy-sources/{lookback_days}/{limit}`

**Path Parameters:**
- `{basis_symbol}` - Uppercase symbol (e.g., "BTC", "ETH", "HYPE")
- `{lookback_days}` - Days to average over (default: "7", min: "1")
- `{limit}` - Max opportunities to return (default: "10", max: "1000")

**Examples:**
```python
# Default: 7-day lookback, top 10
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/BTC/apy-sources/7/10"
)

# Custom: 30-day lookback, top 100
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/BTC/apy-sources/30/100"
)
```

### 3. Delta-Neutral Pairs
**URI:** `wayfinder://delta-lab/{basis_symbol}/delta-neutral/{lookback_days}/{limit}`

**Path Parameters:**
- `{basis_symbol}` - Uppercase symbol
- `{lookback_days}` - Days to average over (default: "7", min: "1")
- `{limit}` - Max pairs to return (default: "5", max: "100")

**Examples:**
```python
# Default: 7-day lookback, top 5
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/ETH/delta-neutral/7/5"
)

# Custom: 14-day lookback, top 20
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/ETH/delta-neutral/14/20"
)
```

### 4. Asset Lookup
**URI:** `wayfinder://delta-lab/assets/{asset_id}`

**Example:**
```python
# Via MCP resource (interactive)
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/assets/1"
)
```

### 5. Assets by Address
**URIs:**
- `wayfinder://delta-lab/assets/by-address/{address}`
- `wayfinder://delta-lab/assets/by-address/{address}/{chain_id}`

**Path Parameters:**
- `address` - Contract address to search for
- `chain_id` - Optional chain ID filter (e.g. `8453`) or `"all"`

**Example:**
```python
# Returns all chains
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/assets/by-address/0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
)

# Base only
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/assets/by-address/0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913/8453"
)
```

### 6. Asset Search
**URIs:**
- `wayfinder://delta-lab/assets/search/{query}`
- `wayfinder://delta-lab/assets/search/{chain}/{query}`

**Purpose:** Quickly find Delta Lab asset IDs when you only know an approximate symbol/name (e.g. `sUSDai`, `wsteth`, `usdc`). Searches across `symbol`, `name`, `coingecko_id`, and `address`.

**Path Parameters:**
- `{query}` - Search term (symbol/name/address/asset_id)
- `{chain}` - Optional chain filter (chain code like `base` or chain ID like `8453`). Use `"all"` for no filter.

**Examples:**
```python
# Search across all chains
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/assets/search/sUSDai")

# Base only (chain code)
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/assets/search/base/usdc")

# Base only (chain ID)
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/assets/search/8453/usdc")
```

### 7. Asset Basis Info
**URI:** `wayfinder://delta-lab/{symbol}/basis`

**Example:**
```python
# Via MCP resource (interactive)
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/ETH/basis"
)
```

### 8. Asset Timeseries (Quick Snapshots)
**URI:** `wayfinder://delta-lab/{symbol}/timeseries/{series}/{lookback_days}/{limit}`

**MCP Philosophy:** SHORT, interpretable results only. For serious analysis, use the client.

**Path Parameters:**
- `{symbol}` - Asset symbol (e.g., "ETH", "BTC")
- `{series}` - Data series: "price" (default), "yield", "lending", "funding", "pendle", "boros", "rates" (all rates), or empty for all
- `{lookback_days}` - Number of days to look back (default: "7" for quick snapshot)
- `{limit}` - Maximum data points per series (default: "100", max: "10000")

**Available Series:** `price`, `yield`, `lending`, `funding`, `pendle`, `boros`, `rates` (all rates), or empty string (all series)

**⚠️ Lending Data Limitation:** The `limit` parameter is **global across all venues/markets**, not per-market. For multi-venue assets like BTC (52 lending markets), requesting lending data with a 1000-point limit will return ~20 timestamps (1000 ÷ 52) instead of 1000 timestamps. This can result in large responses (300KB+) with limited time coverage.

**Workarounds:**
- Use `price` or `funding` series for longer time ranges (single record per timestamp)
- For lending data, use the client with post-filtering by venue:
  ```python
  data = await DELTA_LAB_CLIENT.get_asset_timeseries("BTC", series="lending", limit=10000)
  moonwell = data["lending"][data["lending"]["venue"] == "moonwell"]
  ```

**Note:** MCP resource returns JSON arrays. For DataFrame formatting, use the client (see below).

**MCP Examples (Quick Snapshots):**
```python
# Quick snapshot: price, 7 days, 100 points (all defaults)
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/ETH/timeseries/price/7/100"
)

# Recent funding rates
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/BTC/timeseries/funding/7/100"
)
```

**Client Examples (Serious Analysis):**
```python
# ✅ Plot price history (30 days, DataFrame)
data = await DELTA_LAB_CLIENT.get_asset_timeseries(
    symbol="ETH",
    series="price",
    lookback_days=30,
    limit=1000
)
data["price"]["price_usd"].plot(title="ETH 30-day Price")

# ✅ Analyze lending rates (filter by venue)
data = await DELTA_LAB_CLIENT.get_asset_timeseries(
    symbol="BTC",
    series="lending",
    lookback_days=30,
    limit=10000
)
lending_df = data["lending"]
moonwell = lending_df[lending_df["venue"] == "moonwell"]
moonwell["supply_apr"].plot(title="BTC Supply APR (Moonwell)")

# ✅ Compare funding across venues
data = await DELTA_LAB_CLIENT.get_asset_timeseries("BTC", series="funding", lookback_days=14)
funding_df = data["funding"]
for venue in funding_df["venue"].unique():
    venue_data = funding_df[funding_df["venue"] == venue]
    venue_data["funding_rate"].plot(label=venue)
```

**When to use MCP vs Client:**
- **MCP:** "Show recent price", "What's the funding rate?", quick sanity checks
- **Client:** Plotting, filtering, aggregating, multi-day analysis, lending data

### 9. Screen Price
**URI:** `wayfinder://delta-lab/screen/price/{sort}/{limit}/{basis}`

**Purpose:** Screen assets by price features — returns, volatility, drawdowns. Useful for quickly finding top movers or most volatile assets.

**Path Parameters:**
- `{sort}` - Column to sort by. Options: `price_usd`, `ret_1d`, `ret_7d`, `ret_30d`, `ret_90d`, `vol_7d`, `vol_30d`, `vol_90d`, `mdd_30d`, `mdd_90d`
- `{limit}` - Max rows to return (default: "100", max: "1000")
- `{basis}` - Basis symbol filter (e.g. "ETH", "BTC") or `"all"` for no filter

**Examples:**
```python
# Top 10 daily movers (all assets)
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/price/ret_1d/10/all")

# Most volatile ETH-basis assets (30d)
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/price/vol_30d/20/ETH")
```

### 10. Screen Lending
**URI:** `wayfinder://delta-lab/screen/lending/{sort}/{limit}/{basis}`

**Purpose:** Screen lending markets by surface features — supply/borrow APRs, TVL, utilization, z-scores. Frozen/paused markets are excluded by default in MCP.

**Path Parameters:**
- `{sort}` - Column to sort by. Options: `net_supply_apr_now`, `net_supply_mean_7d`, `net_supply_mean_30d`, `combined_net_supply_apr_now`, `combined_supply_mean_7d`, `net_borrow_apr_now`, `supply_tvl_usd`, `liquidity_usd`, `util_now`, `util_mean_30d`, `borrow_spike_score`, `net_supply_z_30d`
- `{limit}` - Max rows to return (default: "100", max: "1000")
- `{basis}` - Basis symbol filter (e.g. "ETH") or `"all"` for no filter

**Examples:**
```python
# Top 20 lending rates across all assets
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/lending/net_supply_apr_now/20/all")

# Best ETH lending rates
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/lending/net_supply_apr_now/20/ETH")

# Highest borrow spike scores (potential rate anomalies)
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/lending/borrow_spike_score/10/all")
```

**Client-only filters (use `DELTA_LAB_CLIENT.screen_lending()` for):**
- `venue` - Filter by venue name (e.g. "aave", "morpho", "moonwell")
- `min_tvl` - Minimum supply TVL in USD
- `exclude_frozen` - Toggle frozen/paused market exclusion (MCP always excludes)

### 11. Screen Perp
**URI:** `wayfinder://delta-lab/screen/perp/{sort}/{limit}/{basis}`

**Purpose:** Screen perpetual markets by surface features — funding rates, basis, OI, volume. Useful for finding high-funding or anomalous perp markets.

**Path Parameters:**
- `{sort}` - Column to sort by. Options: `funding_now`, `funding_mean_7d`, `funding_std_7d`, `funding_mean_30d`, `funding_std_30d`, `funding_z_30d`, `funding_z_90d`, `funding_pos_pct_30d`, `basis_now`, `basis_mean_7d`, `basis_mean_30d`, `basis_z_30d`, `oi_now`, `oi_mean_7d`, `oi_change_vs_7d_mean`, `volume_24h`, `mark_price`
- `{limit}` - Max rows to return (default: "100", max: "1000")
- `{basis}` - Basis symbol filter (e.g. "BTC") or `"all"` for no filter

**Examples:**
```python
# Top 20 highest funding rates right now
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/perp/funding_now/20/all")

# ETH perps sorted by 30-day mean funding
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/perp/funding_mean_30d/20/ETH")

# Biggest OI changes vs 7d mean
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/perp/oi_change_vs_7d_mean/10/all")
```

**Client-only filters (use `DELTA_LAB_CLIENT.screen_perp()` for):**
- `venue` - Filter by venue name (e.g. "hyperliquid", "binance")
- `order` - Switch to ascending sort (MCP defaults to descending)
- `asset_ids` - Filter by specific asset IDs

### 12. Screen Borrow Routes
**URIs:**
- `wayfinder://delta-lab/screen/borrow-routes/{sort}/{limit}/{basis}/{borrow_basis}`
- `wayfinder://delta-lab/screen/borrow-routes/{sort}/{limit}/{basis}/{borrow_basis}/{chain_id}`

**Purpose:** Screen lending borrow routes (collateral → borrow) by route configuration (LTV, liquidation thresholds, debt ceilings, topology/mode).

**Path Parameters:**
- `{sort}` - Column to sort by. Options include: `ltv_max`, `liq_threshold`, `liquidation_penalty`, `debt_ceiling_usd`, `venue_name`, `market_label`, `created_at`
- `{limit}` - Max rows to return (default: "100", max: "1000")
- `{basis}` - Collateral basis symbol filter (e.g. "ETH") or `"all"` for no filter
- `{borrow_basis}` - Borrow basis symbol filter (e.g. "USD") or `"all"` for no filter
- `{chain_id}` - Optional chain filter (e.g. `8453`) or `"all"` for no filter

**Examples:**
```python
# ETH collateral -> USD borrow routes by max LTV
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/borrow-routes/ltv_max/50/ETH/USD")

# Screen across all collateral/borrow pairs
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/borrow-routes/ltv_max/100/all/all")

# Base chain only
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/borrow-routes/ltv_max/50/ETH/USD/8453")
```

**Client-only filters (use `DELTA_LAB_CLIENT.screen_borrow_routes()` for):**
- `venue` - Filter by venue name
- `market_id` - Filter by market ID
- `topology` - Filter by route topology (e.g. "POOLED", "ISOLATED_PAIR")
- `mode_type` - Filter by route mode type (e.g. "BASE", "EMODE")
- `asset_ids` / `borrow_asset_ids` - Filter by exact asset IDs

## Implementation Details

**File:** `wayfinder_paths/mcp/resources/delta_lab.py`

Async functions that wrap `DELTA_LAB_CLIENT` methods. All functions:
- Return dicts (JSON-serializable)
- Handle errors gracefully (return `{"error": "..."}`)
- Auto-uppercase basis symbols for consistency

**Server registration:** `wayfinder_paths/mcp/server.py`

## When to Use MCP Resources vs Direct Client

### Use MCP Resources (interactive):
- Quick one-off queries in Claude conversation
- No script needed, immediate results
- Screening with sort + basis filter

### Use Direct Client (scripting):
- Extra filters: `venue`, `min_tvl`, `exclude_frozen`, `asset_ids`, `order`
- Complex filtering/processing logic
- Multiple API calls with transformations
- **Timeseries data as DataFrames** for plotting/analysis
