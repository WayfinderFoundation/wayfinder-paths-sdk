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
- `chain_id` - Optional chain filter (chain ID like `8453` or chain code like `base`). Use `"all"` for no filter.

**Example:**
```python
# All chains
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
- `wayfinder://delta-lab/assets/search/{chain}/{query}/{limit}`

**Purpose:** Find Delta Lab asset IDs when you only know an approximate symbol/name (e.g. `sUSDai`, `wsteth`, `usdc`).

**Path Parameters:**
- `{query}` - Search term (symbol/name/address/coingecko_id/asset_id)
- `{chain}` - Optional chain filter (chain ID like `8453` or chain code like `base`). Use `"all"` for no filter.
- `{limit}` - Max results (default `25`, max `200`)

**Examples:**
```python
# Search across all chains
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/assets/search/sUSDai")

# Base only (chain code)
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/assets/search/base/usdc")

# Base only + smaller limit
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/assets/search/base/usdc/10")
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

**Asset timeseries (exact symbol, default):**
- `wayfinder://delta-lab/{symbol}/timeseries/{series}/{lookback_days}/{limit}`
- `wayfinder://delta-lab/{symbol}/timeseries/{series}/{lookback_days}/{limit}/{venue}`

**Basis timeseries (expands to basis group members):**
- `wayfinder://delta-lab/basis/{symbol}/timeseries/{series}/{lookback_days}/{limit}`
- `wayfinder://delta-lab/basis/{symbol}/timeseries/{series}/{lookback_days}/{limit}/{venue}`

**MCP Philosophy:** SHORT, interpretable results only. For serious analysis, use the client.

**Path Parameters:**
- `{symbol}` - Asset symbol (e.g., "USDC", "ETH")
- `{series}` - Data series: "price" (default), "yield", "lending", "funding", "pendle", "boros", "rates" (all rates), or empty for all
- `{lookback_days}` - Number of days to look back (default: "7" for quick snapshot)
- `{limit}` - Maximum data points per series (default: "100", max: "10000")
- `{venue}` - Venue name prefix to filter on (e.g. "moonwell", "hyperliquid"). Use `_` for no filter.

**Available Series:** `price`, `yield`, `lending`, `funding`, `pendle`, `boros`, `rates` (all rates), or empty string (all series)

**Asset vs Basis mode:**
- **Asset (default):** `{symbol}/timeseries/...` — returns data for the exact symbol only. "USDC" returns only USDC pools.
- **Basis:** `basis/{symbol}/timeseries/...` — expands the symbol to all basis group members. "USDC" returns USDC + sUSDC + aUSDC etc.

Use asset mode (the default) when you know what you want. Use basis mode when exploring all related assets.

**Venue filter:** Solves the old limit-vs-lookback conflict. Previously, a limit of 1000 across 50 venues meant ~20 rows per venue. With `venue`, you get the full data window for a single venue.

**Note:** MCP resource returns JSON arrays. For DataFrame formatting, use the client (see below).

**MCP Examples (Quick Snapshots):**
```python
# Quick snapshot: price, 7 days, 100 points (all defaults)
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/ETH/timeseries/price/7/100"
)

# ✅ Moonwell USDC lending rates (exact asset + venue filter)
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/USDC/timeseries/lending/30/800/moonwell"
)

# ✅ Hyperliquid BTC funding only
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/BTC/timeseries/funding/14/500/hyperliquid"
)

# ✅ All USD-basis lending (USDC + sUSDC + aUSDC etc.)
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/basis/USDC/timeseries/lending/7/500"
)

# ✅ All USD-basis lending on Moonwell
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/basis/USDC/timeseries/lending/7/500/moonwell"
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

# ✅ Moonwell USDC lending (exact asset is the default)
data = await DELTA_LAB_CLIENT.get_asset_timeseries(
    symbol="USDC",
    series="lending",
    lookback_days=30,
    limit=800,
    venue="moonwell",
)
lending_df = data["lending"]  # All rows are Moonwell USDC

# ✅ Expand to basis group (USDC + sUSDC + aUSDC etc.)
data = await DELTA_LAB_CLIENT.get_asset_timeseries(
    symbol="USDC",
    series="lending",
    lookback_days=7,
    limit=500,
    basis=True,
)

# ✅ Hyperliquid BTC funding only
data = await DELTA_LAB_CLIENT.get_asset_timeseries(
    symbol="BTC",
    series="funding",
    lookback_days=14,
    venue="hyperliquid",
)
```

**When to use MCP vs Client:**
- **MCP:** "Show recent price", "What's the funding rate?", quick sanity checks, venue-filtered snapshots
- **Client:** Plotting, filtering, aggregating, multi-day analysis, DataFrame operations

### 9. Screen Price
**URIs:**
- `wayfinder://delta-lab/screen/price/{sort}/{limit}/{basis}`
- `wayfinder://delta-lab/screen/price/by-asset-ids/{sort}/{limit}/{asset_ids}`

**Purpose:** Screen assets by price features — returns, volatility, drawdowns. Useful for quickly finding top movers or most volatile assets.

**Path Parameters:**
- `{sort}` - Column to sort by. Options: `price_usd`, `ret_1d`, `ret_7d`, `ret_30d`, `ret_90d`, `vol_7d`, `vol_30d`, `vol_90d`, `mdd_30d`, `mdd_90d`
- `{limit}` - Max rows to return (default: "100", max: "1000")
- `{basis}` - Basis symbol filter (e.g. "ETH", "BTC") or `"all"` for no filter
- `{asset_ids}` - Comma-separated asset IDs (e.g. `"1,2,3"`) or `"all"`

**Examples:**
```python
# Top 10 daily movers (all assets)
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/price/ret_1d/10/all")

# Most volatile ETH-basis assets (30d)
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/price/vol_30d/20/ETH")

# Exact asset IDs (comma-separated)
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/price/by-asset-ids/price_usd/10/1275,498")
```

### 10. Screen Lending
**URIs:**
- `wayfinder://delta-lab/screen/lending/{sort}/{limit}/{basis}`
- `wayfinder://delta-lab/screen/lending/by-asset-ids/{sort}/{limit}/{asset_ids}`

**Purpose:** Screen lending markets by surface features — supply/borrow APRs, TVL, utilization, z-scores. Frozen/paused markets are excluded by default in MCP.

**Path Parameters:**
- `{sort}` - Column to sort by. Options: `net_supply_apr_now`, `net_supply_mean_7d`, `net_supply_mean_30d`, `combined_net_supply_apr_now`, `combined_supply_mean_7d`, `net_borrow_apr_now`, `supply_tvl_usd`, `liquidity_usd`, `util_now`, `util_mean_30d`, `borrow_spike_score`, `net_supply_z_30d`
- `{limit}` - Max rows to return (default: "100", max: "1000")
- `{basis}` - Basis symbol filter (e.g. "ETH") or `"all"` for no filter
- `{asset_ids}` - Comma-separated asset IDs (e.g. `"1,2,3"`) or `"all"`

**Examples:**
```python
# Top 20 lending rates across all assets
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/lending/net_supply_apr_now/20/all")

# Best ETH lending rates
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/lending/net_supply_apr_now/20/ETH")

# Highest borrow spike scores (potential rate anomalies)
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/lending/borrow_spike_score/10/all")

# Exact asset IDs (comma-separated)
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/lending/by-asset-ids/net_supply_apr_now/20/1,1275")
```

**Client-only filters (use `DELTA_LAB_CLIENT.screen_lending()` for):**
- `venue` - Filter by venue name (e.g. "aave", "morpho", "moonwell")
- `min_tvl` - Minimum supply TVL in USD
- `exclude_frozen` - Toggle frozen/paused market exclusion (MCP always excludes)

### 11. Screen Perp
**URIs:**
- `wayfinder://delta-lab/screen/perp/{sort}/{limit}/{basis}`
- `wayfinder://delta-lab/screen/perp/by-asset-ids/{sort}/{limit}/{asset_ids}`

**Purpose:** Screen perpetual markets by surface features — funding rates, basis, OI, volume. Useful for finding high-funding or anomalous perp markets.

**Path Parameters:**
- `{sort}` - Column to sort by. Options: `funding_now`, `funding_mean_7d`, `funding_std_7d`, `funding_mean_30d`, `funding_std_30d`, `funding_z_30d`, `funding_z_90d`, `funding_pos_pct_30d`, `basis_now`, `basis_mean_7d`, `basis_mean_30d`, `basis_z_30d`, `oi_now`, `oi_mean_7d`, `oi_change_vs_7d_mean`, `volume_24h`, `mark_price`
- `{limit}` - Max rows to return (default: "100", max: "1000")
- `{basis}` - Basis symbol filter (e.g. "BTC") or `"all"` for no filter
- `{asset_ids}` - Comma-separated base asset IDs (e.g. `"1,2,3"`) or `"all"`

**Examples:**
```python
# Top 20 highest funding rates right now
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/perp/funding_now/20/all")

# ETH perps sorted by 30-day mean funding
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/perp/funding_mean_30d/20/ETH")

# Biggest OI changes vs 7d mean
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/perp/oi_change_vs_7d_mean/10/all")

# Exact base asset IDs (comma-separated)
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/screen/perp/by-asset-ids/funding_now/20/1,2")
```

**Client-only filters (use `DELTA_LAB_CLIENT.screen_perp()` for):**
- `venue` - Filter by venue name (e.g. "hyperliquid", "binance")
- `order` - Switch to ascending sort (MCP defaults to descending)

### 12. Screen Borrow Routes
**URIs:**
- `wayfinder://delta-lab/screen/borrow-routes/{sort}/{limit}/{basis}/{borrow_basis}`
- `wayfinder://delta-lab/screen/borrow-routes/{sort}/{limit}/{basis}/{borrow_basis}/{chain_id}`
- `wayfinder://delta-lab/screen/borrow-routes/by-asset-ids/{sort}/{limit}/{asset_ids}/{borrow_asset_ids}`
- `wayfinder://delta-lab/screen/borrow-routes/by-asset-ids/{sort}/{limit}/{asset_ids}/{borrow_asset_ids}/{chain_id}`

**Purpose:** Screen lending borrow routes (collateral → borrow) by route configuration (LTV, liquidation thresholds, debt ceilings, topology/mode).

**Path Parameters:**
- `{sort}` - Column to sort by. Options include: `ltv_max`, `liq_threshold`, `liquidation_penalty`, `debt_ceiling_usd`, `venue_name`, `market_label`, `created_at`
- `{limit}` - Max rows to return (default: "100", max: "1000")
- `{basis}` - Collateral basis symbol filter (e.g. "ETH") or `"all"` for no filter
- `{borrow_basis}` - Borrow basis symbol filter (e.g. "USD") or `"all"` for no filter
- `{chain_id}` - Optional chain filter (chain ID like `8453` or chain code like `base`). Use `"all"` for no filter.

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
