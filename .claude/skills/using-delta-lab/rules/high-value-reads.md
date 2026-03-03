# High-Value Reads

These are the core Delta Lab client methods you'll use most often.

## Quick Reference

**User asks:** → **Use this method:**

- "What symbols/assets are available?" → `get_basis_symbols()`
- "What are the absolute best APYs right now?" → `get_top_apy()` (all symbols)
- "What are the best APYs for BTC/ETH?" → `get_basis_apy_sources(basis_symbol="BTC")`
- "Find me delta-neutral opportunities" → `get_best_delta_neutral_pairs()`
- "What lending rates are available for X?" → `get_basis_apy_sources()` + filter `instrument_type=="LENDING_SUPPLY"`
- "Compare funding rates across venues" → `get_basis_apy_sources()` + filter `instrument_type=="PERP"`
- "Show me the highest yield with lowest risk" → `get_best_delta_neutral_pairs()` + use `pareto_frontier`
- "What asset is asset_id 123?" → `get_asset(asset_id=123)`
- "Find all WETH assets across chains" → `get_assets_by_address("0xC02a...")`
- "Is ETH in a basis group?" → `get_asset_basis("ETH")`
- "Get price history for plotting" → `get_asset_timeseries("ETH", series="price")`
- "Compare lending rates over time" → `get_asset_timeseries("USDC", series="lending")`

**Important:** Delta Lab is **read-only** (discovery only, no execution).

## ⚠️ APY Value Format (CRITICAL)

**APY values are returned as decimal floats, NOT percentages:**

- `0.98` means **98% APY** (not 0.98%)
- `2.40` means **240% APY** (not 2.40%)
- `0.05` means **5% APY** (not 0.05%)

To display as percentage: **multiply by 100** (e.g., `apy * 100` = `98%`)

When comparing or reporting APYs, always interpret them as decimals. A value of `0.98` is a very high yield (98%), not a negligible one.

## MCP Resources (Default Approach)

**Use MCP resources for all queries** - instant, no script needed. See CLAUDE.md for full documentation.

Quick URIs:
- `wayfinder://delta-lab/top-apy/7/20` - **Top 20 APYs across ALL symbols** (7-day lookback)
- `wayfinder://delta-lab/WSTETH/apy-sources/7/10` - Top 10 yield opportunities for WSTETH (7-day lookback)
- `wayfinder://delta-lab/WSTETH/apy-sources/30/50` - Top 50 opportunities for WSTETH (30-day lookback)
- `wayfinder://delta-lab/ETH/delta-neutral/7/5` - Top 5 delta-neutral pairs for ETH (7-day lookback)
- `wayfinder://delta-lab/ETH/delta-neutral/14/20` - Top 20 pairs for ETH (14-day lookback)

**Only use Python client below for:** Complex filtering, DataFrame formatting, or multi-venue lending data.

## 0. Get Basis Symbols (Discovery)

**Purpose:** List all available basis symbols in Delta Lab.

```python
from wayfinder_paths.core.clients import DELTA_LAB_CLIENT

# Get all available symbols
result = await DELTA_LAB_CLIENT.get_basis_symbols(get_all=True)

# Get top 50 symbols
result = await DELTA_LAB_CLIENT.get_basis_symbols(limit=50)
```

### Response Structure

```python
{
    "symbols": [
        {
            "symbol": "BTC",
            "asset_id": 1,
            "basis_group_id": 1,
            "opportunity_count": 95
        },
        {
            "symbol": "ETH",
            "asset_id": 2,
            "basis_group_id": 2,
            "opportunity_count": 87
        },
        ...
    ],
    "total_count": 50
}
```

### Key Fields

- `symbols` - List of basis symbols with metadata
- `total_count` - Number of symbols returned
- `opportunity_count` - Number of opportunities available for each symbol

### Use Cases

**Find which symbols have opportunities:**
```python
result = await DELTA_LAB_CLIENT.get_basis_symbols(get_all=True)
symbols_with_data = [s for s in result["symbols"] if s["opportunity_count"] > 0]
print(f"Found {len(symbols_with_data)} symbols with opportunities")
```

**Get top symbols by opportunity count:**
```python
result = await DELTA_LAB_CLIENT.get_basis_symbols(get_all=True)
sorted_symbols = sorted(result["symbols"], key=lambda x: x["opportunity_count"], reverse=True)
top_10 = sorted_symbols[:10]
```

## 1. Get Basis APY Sources

**Purpose:** Find all yield opportunities for a given asset across all protocols.

```python
from wayfinder_paths.core.clients import DELTA_LAB_CLIENT

# Get all BTC opportunities
result = await DELTA_LAB_CLIENT.get_basis_apy_sources(
    basis_symbol="BTC",
    lookback_days=7,  # Default: 7, min: 1
    limit=500,  # Default: 500, max: 1000
    as_of=None,  # Default: now (optional datetime)
)
```

### Response Structure

```python
{
    "basis": {
        "input_symbol": "BTC",
        "root_symbol": "BTC",
        "root_asset_id": 1,
        "basis_group_id": 42,
        "basis_asset_ids": [1, 123, 456]
    },
    "as_of": "2024-02-12T12:00:00Z",
    "lookback_days": 7,
    "summary": {
        "instrument_type_counts": {
            "perp": 15,
            "lending": 8,
            "fixed_rate": 3
        }
    },
    "directions": {
        "LONG": [...],  # Opportunities where you receive yield
        "SHORT": [...]  # Opportunities where you pay yield
    },
    "opportunities": [...],  # All opportunities combined
    "warnings": []
}
```

### Key Fields

- `directions.LONG` - Yield-generating positions (lending, short perp in positive funding, PT)
- `directions.SHORT` - Yield-paying positions (borrowing, long perp in positive funding, YT)
- `opportunities` - All opportunities regardless of direction
- `summary.instrument_type_counts` - Count by instrument type

## 2. Get Best Delta-Neutral Pairs

**Purpose:** Find the best carry/hedge combinations for delta-neutral strategies.

```python
# Get best delta-neutral pairs for BTC
result = await DELTA_LAB_CLIENT.get_best_delta_neutral_pairs(
    basis_symbol="BTC",
    lookback_days=7,  # Default: 7, min: 1
    limit=20,  # Default: 20, max: 100
    as_of=None,  # Default: now
)
```

### Response Structure

```python
{
    "basis": {
        "input_symbol": "BTC",
        "root_symbol": "BTC",
        "root_asset_id": 1,
        "basis_group_id": 42,
        "basis_asset_ids": [1, 123, 456]
    },
    "as_of": "2024-02-12T12:00:00Z",
    "lookback_days": 7,
    "candidates": [
        {
            "basis_root_symbol": "BTC",
            "exposure_asset": {"asset_id": 1, "symbol": "BTC"},
            "carry_leg": {...},  # Full opportunity object
            "hedge_leg": {...},  # Full opportunity object
            "net_apy": 0.12,  # Combined APY (12%)
            "erisk_proxy": 0.05  # Risk metric
        },
        ...
    ],
    "pareto_frontier": [...]  # Optimal risk/return pairs
}
```

### Key Fields

- `candidates` - All delta-neutral pairs sorted by net_apy descending
- `pareto_frontier` - Subset of candidates on the risk/return Pareto frontier
- `carry_leg` - The position earning yield (LONG opportunity)
- `hedge_leg` - The position hedging exposure (SHORT opportunity)
- `net_apy` - Combined APY after hedging costs
- `erisk_proxy` - Risk metric (lower is better)

## 3. Get Asset Info

**Purpose:** Look up asset metadata by internal asset_id.

```python
# Get asset info by ID
result = await DELTA_LAB_CLIENT.get_asset(asset_id=1)
```

### Response Structure

```python
{
    "asset_id": 1,
    "symbol": "BTC",
    "name": "Bitcoin",
    "decimals": 8,
    "chain_id": 1,
    "address": "0x...",
    "coingecko_id": "bitcoin"
}
```

### Use Cases

- Resolving asset_id references from opportunities
- Getting contract addresses for on-chain execution
- Looking up coingecko_id for price data

## Common Query Patterns

### Find highest APY for an asset

```python
result = await DELTA_LAB_CLIENT.get_basis_apy_sources(
    basis_symbol="ETH",
    lookback_days=7,
    limit=500
)

# Filter LONG opportunities (yield-generating)
long_opps = result["directions"]["LONG"]

# Sort by APY descending
sorted_opps = sorted(
    long_opps,
    key=lambda x: x["apy"]["value"] or 0,
    reverse=True
)

highest_apy = sorted_opps[0] if sorted_opps else None
```

### Find best delta-neutral strategy by net APY

```python
result = await DELTA_LAB_CLIENT.get_best_delta_neutral_pairs(
    basis_symbol="BTC",
    lookback_days=7,
    limit=20
)

# Candidates are already sorted by net_apy descending
best_pair = result["candidates"][0] if result["candidates"] else None

print(f"Best pair: {best_pair['net_apy']:.2%} net APY")
print(f"Carry leg: {best_pair['carry_leg']['instrument_type']} on {best_pair['carry_leg']['venue']}")
print(f"Hedge leg: {best_pair['hedge_leg']['instrument_type']} on {best_pair['hedge_leg']['venue']}")
```

### Find best Pareto-optimal delta-neutral strategy

```python
result = await DELTA_LAB_CLIENT.get_best_delta_neutral_pairs(
    basis_symbol="BTC",
    lookback_days=7,
    limit=20
)

# Use pareto_frontier for risk-adjusted selection
pareto = result["pareto_frontier"]

# Find lowest risk on frontier
safest = min(pareto, key=lambda x: x["erisk_proxy"]) if pareto else None

# Find highest yield on frontier
highest_yield = max(pareto, key=lambda x: x["net_apy"]) if pareto else None
```

### Compare opportunities across protocols

```python
result = await DELTA_LAB_CLIENT.get_basis_apy_sources(
    basis_symbol="HYPE",
    lookback_days=7,
    limit=500
)

# Group by venue
from collections import defaultdict
by_venue = defaultdict(list)
for opp in result["opportunities"]:
    venue = opp.get("venue") or "unknown"
    by_venue[venue].append(opp)

# Compare average APY by venue
for venue, opps in by_venue.items():
    avg_apy = sum(o["apy"]["value"] or 0 for o in opps) / len(opps)
    print(f"{venue}: {avg_apy:.2%} avg APY ({len(opps)} opportunities)")
```

## 4. Get Assets by Address

**Purpose:** Find all assets with a given contract address (useful for finding wrapped/bridged versions).

```python
# Find all WETH assets across chains
result = await DELTA_LAB_CLIENT.get_assets_by_address(
    address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    chain_id=None,  # Optional: filter by specific chain
)
```

### Response Structure

```python
{
    "assets": [
        {
            "asset_id": 123,
            "symbol": "WETH",
            "name": "Wrapped Ether",
            "decimals": 18,
            "chain_id": 1,
            "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            "coingecko_id": "weth"
        },
        ...
    ]
}
```

### Use Cases

**Find all versions of an asset:**
```python
# Find all USDC versions
result = await DELTA_LAB_CLIENT.get_assets_by_address(
    address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"  # USDC on Ethereum
)
for asset in result["assets"]:
    print(f"{asset['symbol']} on chain {asset['chain_id']}")
```

**Filter by chain:**
```python
# Find USDC specifically on Base
result = await DELTA_LAB_CLIENT.get_assets_by_address(
    address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    chain_id=8453  # Base
)
```

## 5. Get Asset Basis Info

**Purpose:** Check if an asset is part of a basis group and get its role.

```python
# Check if ETH is in a basis group
result = await DELTA_LAB_CLIENT.get_asset_basis(symbol="ETH")
```

### Response Structure

```python
{
    "asset_id": 1,
    "symbol": "ETH",
    "basis": {
        "basis_group_id": 1,
        "root_asset_id": 1,
        "root_symbol": "ETH",
        "role": "ROOT"  # or "WRAPPED", "YIELD_BEARING", "COLLATERAL"
    }
}
# If not in a basis group, "basis" will be None
```

### Use Cases

**Check if assets are fungible:**
```python
eth_basis = await DELTA_LAB_CLIENT.get_asset_basis(symbol="ETH")
weth_basis = await DELTA_LAB_CLIENT.get_asset_basis(symbol="WETH")

if eth_basis["basis"] and weth_basis["basis"]:
    same_group = eth_basis["basis"]["basis_group_id"] == weth_basis["basis"]["basis_group_id"]
    print(f"ETH and WETH are {'in the same' if same_group else 'in different'} basis group(s)")
```

## 6. Get Asset Timeseries

**Purpose:** Get historical timeseries data for price, rates, and market metrics.

**MCP Resource (Quick Snapshots Only):**

MCP timeseries defaults prioritize SHORT, interpretable results. For serious analysis, use the client.

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

**Client (Serious Analysis - DataFrames):**

For anything beyond quick snapshots (plotting, filtering, multi-day, multi-venue), use the client:

```python
# Plot price history (30 days, 1000 points)
data = await DELTA_LAB_CLIENT.get_asset_timeseries(
    symbol="ETH",
    series="price",
    lookback_days=30,
    limit=1000,
    as_of=None,  # Optional: query as of a specific time
)
data["price"]["price_usd"].plot(title="ETH 30-day Price")
```

### Response Structure

Returns `dict[str, pd.DataFrame]` with each series as a separate DataFrame:

```python
{
    "price": DataFrame(
        columns=["price_usd"],
        index=DatetimeIndex  # ts column converted to datetime index
    ),
    "lending": DataFrame(
        columns=["market_id", "chain_id", "venue", "supply_apr", "borrow_apr", ...],
        index=DatetimeIndex
    ),
    ...
}
```

### Available Series

- `"price"` - Price history (columns: `price_usd`)
- `"yield"` - Yield token rates (columns: `yield_token_asset_id`, `yield_token_symbol`, `apy_base`, `apy_base_7d`, `exchange_rate`, `tvl_usd`)
- `"lending"` - Lending market rates (columns: `market_id`, `venue`, `supply_apr`, `borrow_apr`, `supply_reward_apr`, `borrow_reward_apr`, `utilization`, `supply_tvl_usd`, `borrow_tvl_usd`)
- `"funding"` - Perp funding rates (columns: `instrument_id`, `venue`, `market_external_id`, `funding_rate`, `mark_price_usd`, `oi_usd`, `volume_usd`)
- `"pendle"` - Pendle PT/YT rates (columns: `market_id`, `venue`, `pt_symbol`, `maturity_ts`, `implied_apy`, `underlying_apy`, `reward_apr`, `pt_price`, `tvl_usd`)
- `"boros"` - Boros fixed rates (columns: `market_id`, `venue`, `market_external_id`, `fixed_rate_mark`, `floating_rate_oracle`, `pv`)
- `"rates"` - Alias for all rate series (yield, lending, pendle, boros, funding)
- `None` - Return all available series (default)

### Use Cases

**Plot price history:**
```python
data = await DELTA_LAB_CLIENT.get_asset_timeseries("ETH", series="price", lookback_days=7)
data["price"]["price_usd"].plot(title="ETH Price (7d)")
```

**Compare lending rates across venues:**
```python
data = await DELTA_LAB_CLIENT.get_asset_timeseries("USDC", series="lending", lookback_days=30)
lending_df = data["lending"]

# Plot supply APR by venue
import matplotlib.pyplot as plt
for venue in lending_df["venue"].unique():
    venue_data = lending_df[lending_df["venue"] == venue]
    plt.plot(venue_data.index, venue_data["supply_apr"], label=venue)
plt.legend()
plt.title("USDC Supply APR by Venue")
plt.show()
```

**Analyze funding rate trends:**
```python
data = await DELTA_LAB_CLIENT.get_asset_timeseries("BTC", series="funding", lookback_days=30)
funding_df = data["funding"]

# Calculate average funding rate by venue
avg_funding = funding_df.groupby("venue")["funding_rate"].mean()
print(avg_funding.sort_values(ascending=False))
```

**Get all rate series at once:**
```python
# Get price + all rate series
data = await DELTA_LAB_CLIENT.get_asset_timeseries("ETH", series="price,rates", lookback_days=7)

# Access each series
price_df = data["price"]
lending_df = data["lending"]
funding_df = data["funding"]
# ... etc
```
