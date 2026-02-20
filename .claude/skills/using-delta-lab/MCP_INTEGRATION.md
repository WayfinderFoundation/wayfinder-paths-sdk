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
**URI:** `wayfinder://delta-lab/assets/by-address/{address}`

**Path Parameters:**
- `address` - Contract address to search for

**Note:** Returns assets from all chains. To filter by chain_id, use the client directly.

**Example:**
```python
# Via MCP resource (interactive) - returns all chains
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/assets/by-address/0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
)

# To filter by chain, use client:
result = await DELTA_LAB_CLIENT.get_assets_by_address(
    address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    chain_id=1
)
```

### 6. Asset Basis Info
**URI:** `wayfinder://delta-lab/{symbol}/basis`

**Example:**
```python
# Via MCP resource (interactive)
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/ETH/basis"
)
```

### 7. Asset Timeseries (Quick Snapshots)
**URI:** `wayfinder://delta-lab/{symbol}/timeseries/{series}/{lookback_days}/{limit}`

**MCP Philosophy:** SHORT, interpretable results only. For serious analysis, use the client.

**Path Parameters:**
- `{symbol}` - Asset symbol (e.g., "ETH", "BTC")
- `{series}` - Data series: "price" (default), "funding", "lending", "rates", or empty for all
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

## Implementation Details

**File:** `wayfinder_paths/mcp/resources/delta_lab.py`

Three async functions that wrap `DELTA_LAB_CLIENT` methods:
- `get_basis_apy_sources(basis_symbol, lookback_days, limit)`
- `get_best_delta_neutral_pairs(basis_symbol, lookback_days, limit)`
- `get_delta_lab_asset(asset_id)`

All functions:
- Return JSON strings
- Handle errors gracefully (return `{"error": "..."}`)
- Auto-uppercase basis symbols for consistency

**Server registration:** `wayfinder_paths/mcp/server.py`
- Imported delta_lab resource functions
- Registered three MCP resources with FastMCP

## When to Use MCP Resources vs Direct Client

### Use MCP Resources (interactive):
- ✅ Quick one-off queries in Claude conversation
- ✅ No script needed
- ✅ Immediate results
- ❌ Timeseries data returned as JSON arrays (not DataFrames)

### Use Direct Client (scripting):
- ✅ Complex filtering/processing logic
- ✅ Multiple API calls with transformations
- ✅ Batch operations
- ✅ Part of a larger workflow
- ✅ **Timeseries data as DataFrames** - `get_asset_timeseries()` returns `dict[str, pd.DataFrame]` for easy plotting/analysis

## Testing

After restarting the MCP server, you can test with:

```python
# In Claude conversation (no code needed)
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/BTC/apy-sources",
    params={"limit": "10"}
)
```

Or programmatically:
```python
from wayfinder_paths.core.clients import DELTA_LAB_CLIENT

result = await DELTA_LAB_CLIENT.get_basis_apy_sources(
    basis_symbol="BTC",
    lookback_days=7,
    limit=10
)
```

Both access the same backend API.
