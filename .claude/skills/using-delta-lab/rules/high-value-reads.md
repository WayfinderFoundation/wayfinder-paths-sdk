# High-Value Reads

These are the core Delta Lab client methods you'll use most often.

## Quick Reference

**User asks:** → **Use this method:**

- "What symbols/assets are available?" → `get_basis_symbols()`
- "What are the best APYs for BTC/ETH?" → `get_basis_apy_sources()`
- "Find me delta-neutral opportunities" → `get_best_delta_neutral_pairs()`
- "What lending rates are available for X?" → `get_basis_apy_sources()` + filter `instrument_type=="LENDING_SUPPLY"`
- "Compare funding rates across venues" → `get_basis_apy_sources()` + filter `instrument_type=="PERP"`
- "Show me the highest yield with lowest risk" → `get_best_delta_neutral_pairs()` + use `pareto_frontier`
- "What asset is asset_id 123?" → `get_asset(asset_id=123)`

**Important:** Delta Lab is **read-only** (discovery only, no execution).

## MCP Resources (Interactive Access)

Delta Lab is also available via MCP resources for interactive queries without writing scripts:

### Available Resources

1. **`wayfinder://delta-lab/symbols`**
   - Returns: All basis symbols with opportunity counts
   - Use for: Discovery of available assets

2. **`wayfinder://delta-lab/{basis_symbol}/apy-sources/{limit}`**
   - Path parameters:
     - `{basis_symbol}` - Uppercase symbol (e.g., `WSTETH`, `BTC`)
     - `{limit}` - Max opportunities (default: `10`, max: `1000`)
   - Fixed parameter: `lookback_days=7`
   - Returns: Top N opportunities grouped by LONG/SHORT with APY, risk, venues
   - Use for: Quick APY/rate queries (adjust limit as needed)

3. **`wayfinder://delta-lab/{basis_symbol}/delta-neutral/{limit}`**
   - Path parameters:
     - `{basis_symbol}` - Uppercase symbol
     - `{limit}` - Max pairs (default: `5`, max: `100`)
   - Fixed parameter: `lookback_days=7`
   - Returns: Top N carry/hedge pairs sorted by net APY, Pareto frontier
   - Use for: Delta-neutral strategy discovery (adjust limit as needed)

4. **`wayfinder://delta-lab/assets/{asset_id}`**
   - Path parameter: `{asset_id}` - Internal asset ID (integer)
   - Returns: Asset metadata (symbol, name, chain, address, etc.)
   - Use for: Resolving asset IDs to symbols/addresses

### When to use MCP vs Python client

| User Request | Action | Why |
|--------------|--------|-----|
| "Best rates for X" | MCP with `/10` | Default limit |
| "Show me X rates" | MCP with `/10` | Default is enough |
| "Top 10 rates" | MCP with `/10` | That's the default |
| **"Get me more"** (after MCP) | **MCP with `/50`** | **Just change limit in URI** |
| **"Show all"** or **"100 results"** | **MCP with `/100`** | **Adjust limit as needed** |
| "30-day lookback" | Python client | MCP uses 7 days (only custom param) |
| "Filter by venue" | Python client | MCP doesn't filter |

**Examples:**
```
# Get top 10 wstETH rates (default)
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/WSTETH/apy-sources/10")

# User asks for more: just change the limit!
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/WSTETH/apy-sources/50")

# Get 100 opportunities
ReadMcpResourceTool(server="wayfinder", uri="wayfinder://delta-lab/WSTETH/apy-sources/100")

# Only use Python client for custom lookback_days:
data = await DELTA_LAB_CLIENT.get_basis_apy_sources(
    basis_symbol="WSTETH",
    lookback_days=30,  # MCP fixed at 7
    limit=100
)
```

**Important:**
- **Default to MCP resource first** - use `/10` for apy-sources, `/5` for delta-neutral
- **Change limit easily**: Just modify the last number in the URI (max: 1000 for apy-sources, 100 for delta-neutral)
- **Only use Python client for**: Custom `lookback_days`, complex filtering, or batch queries
- Don't write scripts for simple limit changes - just adjust the URI

**Common follow-up pattern:**
```
User: "What are the best wstETH rates?"
→ uri="wayfinder://delta-lab/WSTETH/apy-sources/10"

User: "Get me more" or "Show 50"
→ uri="wayfinder://delta-lab/WSTETH/apy-sources/50"  (just change the number!)
```

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
