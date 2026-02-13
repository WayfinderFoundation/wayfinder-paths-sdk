# Delta Lab MCP Integration

Delta Lab is now available in the Wayfinder MCP server as **read-only resources**.

## MCP Resources Added

### 1. APY Sources
**URI:** `wayfinder://delta-lab/{basis_symbol}/apy-sources`

**Query Parameters:**
- `lookback_days` (optional, default: "7") - Number of days to look back
- `limit` (optional, default: "500") - Max opportunities to return

**Example:**
```python
# Via MCP resource (interactive)
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/BTC/apy-sources",
    params={"lookback_days": "7", "limit": "100"}
)
```

### 2. Delta-Neutral Pairs
**URI:** `wayfinder://delta-lab/{basis_symbol}/delta-neutral`

**Query Parameters:**
- `lookback_days` (optional, default: "7") - Number of days to look back
- `limit` (optional, default: "20") - Max candidates to return

**Example:**
```python
# Via MCP resource (interactive)
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/ETH/delta-neutral",
    params={"lookback_days": "7", "limit": "20"}
)
```

### 3. Asset Lookup
**URI:** `wayfinder://delta-lab/assets/{asset_id}`

**Example:**
```python
# Via MCP resource (interactive)
ReadMcpResourceTool(
    server="wayfinder",
    uri="wayfinder://delta-lab/assets/1"
)
```

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

### Use Direct Client (scripting):
- ✅ Complex filtering/processing logic
- ✅ Multiple API calls with transformations
- ✅ Batch operations
- ✅ Part of a larger workflow

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
