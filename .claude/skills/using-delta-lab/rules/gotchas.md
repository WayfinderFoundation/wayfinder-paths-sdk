# Gotchas

Common mistakes and important considerations when using Delta Lab.

## Quick Cheat Sheet

| ❌ Wrong | ✅ Right | Why |
|---------|---------|-----|
| `screen_lending(basis="USDC")` | `screen_lending(basis="USD")` | USDC is an asset, not a basis symbol — use `USD` (MCP auto-resolves, client does not) |
| `get_basis_apy_sources(limit=20)` for lending | Use `screen_lending` or `timeseries(series="lending")` | apy-sources is cross-instrument ranked — low-APY lending gets cut off |
| `get_lending_timeseries("USDC")` | `get_asset_timeseries(symbol="USDC", series="lending")` | One method for all series, not per-series methods |
| `ok, data = await DELTA_LAB_CLIENT...` | `data = await DELTA_LAB_CLIENT...` | Clients return data directly, not tuples |
| `data["opportunities"]` | `data["directions"]["LONG"]` | Lending opps are in LONG direction |
| `candidate["net_apy"]["value"]` | `candidate["net_apy"]` | net_apy is a float, not a dict |
| `basis_symbol="bitcoin"` | `basis_symbol="BTC"` | Use root symbol, not coingecko ID |
| **Negative funding = good for shorts** | **Negative funding = shorts PAY longs** | **CRITICAL: Sign is backwards from intuition** |
| `max(opps, key=lambda x: x["apy"]["value"])` | `max([o for o in opps if o["apy"]["value"]], ...)` | APY can be null |
| Using `candidates[0]` for lowest risk | Use `pareto_frontier` | Candidates sorted by APY, not risk |
| Ignoring `warnings` field | Always check `result["warnings"]` | Data quality issues affect decisions |
| `get_asset_timeseries(symbol="USDC", series="lending", limit=1000)` (no venue) | Add `venue="moonwell"` when you want one venue | Without venue filter, limit is shared across all venues — data gets cut off |
| `get_asset_timeseries(..., basis=True)` when you want one asset | Omit `basis` (defaults to `False`) | Default is exact asset; pass `basis=True` only to expand to basis group |

## 0. Basis Symbol vs Asset Symbol

**Screening, apy-sources, and delta-neutral endpoints use basis symbols, not asset symbols.**

Stablecoins like USDC, USDT, DAI are NOT basis symbols — they belong to the `USD` basis group. Similarly, wstETH → `ETH`, cbBTC → `BTC`.

The MCP layer auto-resolves asset symbols to their root basis (e.g. `USDC` → `USD`), so MCP queries like `screen/lending/.../USDC` will work. But the Python client requires the correct basis symbol directly.

```python
# WRONG — USDC is not a basis symbol (client will 400)
await DELTA_LAB_CLIENT.screen_lending(basis="USDC")

# RIGHT — use the root basis symbol
await DELTA_LAB_CLIENT.screen_lending(basis="USD")

# To find any symbol's root basis:
info = await DELTA_LAB_CLIENT.get_asset_basis(symbol="USDC")
# -> {"basis": {"root_symbol": "USD", "role": "STABLE", ...}}
```

Common mappings: USDC/USDT/DAI → `USD`, wstETH/cbETH → `ETH`, cbBTC/WBTC → `BTC`.

## 0b. apy-sources Is Cross-Instrument, Not Venue-Specific

`get_basis_apy_sources()` returns a **ranked cross-instrument list** (perps, LPs, lending, Pendle, Boros) sorted by APY. A small `limit` (e.g. 20) will only show the highest-APY instruments — standard lending markets at 2-5% get crowded out by perps at 20%+.

**If you want lending rates specifically:**
- Use `screen_lending(basis="USD")` for a cross-venue snapshot
- Use `get_asset_timeseries(symbol="USDC", series="lending", venue="moonwell")` for historical data from one venue

**If you need all instrument types:** use a large limit (e.g. 500) with `get_basis_apy_sources()`.

## 0c. One Method for All Timeseries

There is no `get_lending_timeseries()` or `get_funding_timeseries()`. There's one method:

```python
data = await DELTA_LAB_CLIENT.get_asset_timeseries(
    symbol="USDC",
    series="lending",  # or "funding", "price", "pendle", "boros", "rates"
    lookback_days=30,
    limit=800,
    venue="moonwell",  # optional venue filter
    basis=False,        # optional: exact symbol only (no basis expansion)
)
lending_df = data["lending"]  # DataFrame keyed by series name
```

The `series` parameter selects which data to fetch. The return dict is keyed by series name.

## 1. Client Return Pattern

**CRITICAL: Delta Lab CLIENT returns data directly (not tuples).**

```python
# WRONG - Clients don't return tuples
ok, data = await DELTA_LAB_CLIENT.get_basis_apy_sources(...)  # ❌

# RIGHT - Clients return data directly
data = await DELTA_LAB_CLIENT.get_basis_apy_sources(...)  # ✅
```

See CLAUDE.md "Scripting gotchas #0" for Client vs Adapter explanation.

**Response structure quick reference:**
```python
# APY sources
data["directions"]["LONG"]  # ← Lending opportunities (NOT data["opportunities"])

# Delta-neutral pairs
candidate["net_apy"]  # ← Float (NOT candidate["net_apy"]["value"])
```

## 1. Symbol Resolution

**WRONG:**
```python
# Don't use coingecko IDs
await DELTA_LAB_CLIENT.get_basis_apy_sources(basis_symbol="bitcoin")

# Don't use lowercase (works, but inconsistent)
await DELTA_LAB_CLIENT.get_basis_apy_sources(basis_symbol="btc")

# Don't use token IDs
await DELTA_LAB_CLIENT.get_basis_apy_sources(basis_symbol="usd-coin-base")
```

**RIGHT:**
```python
# Use uppercase root symbols
await DELTA_LAB_CLIENT.get_basis_apy_sources(basis_symbol="BTC")
await DELTA_LAB_CLIENT.get_basis_apy_sources(basis_symbol="ETH")
await DELTA_LAB_CLIENT.get_basis_apy_sources(basis_symbol="HYPE")
```

The API accepts lowercase but prefers uppercase root symbols.

## 2. APY Can Be Null

**WRONG:**
```python
opportunities = result["opportunities"]
highest = max(opportunities, key=lambda x: x["apy"]["value"])  # Crashes if value is null!
```

**RIGHT:**
```python
opportunities = result["opportunities"]
# Filter out null APYs first
valid_opps = [o for o in opportunities if o["apy"]["value"] is not None]
if valid_opps:
    highest = max(valid_opps, key=lambda x: x["apy"]["value"])
else:
    print("No opportunities with valid APY")

# Or use a default
highest = max(opportunities, key=lambda x: x["apy"]["value"] or 0)
```

APY can be `null` for several reasons:
- Insufficient historical data
- Market just launched
- Data source temporarily unavailable

## 3. Funding Rate Sign (CRITICAL MISCONCEPTION)

**CRITICAL: Negative funding means shorts PAY longs.**

```python
# Funding rate = +15% annually (0.15)
# ✅ GOOD for shorts: Longs pay shorts 15%/year
# ❌ BAD for longs: Longs pay 15%/year

# Funding rate = -8% annually (-0.08)
# ❌ BAD for shorts: Shorts pay longs 8%/year
# ✅ GOOD for longs: Longs receive 8%/year
```

**Common mistake:**
```python
# WRONG interpretation
funding = -0.08  # -8% annually
print("Negative funding = good for shorts!")  # ❌ BACKWARDS!

# RIGHT interpretation
funding = -0.08  # -8% annually
if funding < 0:
    print("Shorts PAY longs (bad for shorts)")  # ✅ Correct
else:
    print("Longs PAY shorts (good for shorts)")  # ✅ Correct
```

**In Delta Lab data:**
- Positive funding → `side="SHORT"` `instrument_type="PERP"` receives funding (good for shorts)
- Negative funding → `side="SHORT"` `instrument_type="PERP"` pays funding (cost for shorts)
- Delta-neutral candidates typically use `instrument_type="PERP"` and `side="SHORT"` as the hedge leg
- The APY value is already signed correctly for the direction shown

## 4. Side vs Sign

**Don't confuse direction with sign:**

```python
# This is a short perp position receiving funding
{
    "side": "SHORT",
    "instrument_type": "PERP",
    "apy": {"value": 0.12}  # Positive funding = shorts receive
}

# This is a short perp position paying funding
{
    "side": "SHORT",
    "instrument_type": "PERP",
    "apy": {"value": -0.08}  # Negative funding = shorts pay
}
```

- `side` indicates the position direction (LONG or SHORT)
- `apy.value` sign indicates whether you receive (positive) or pay (negative)
- For delta-neutral pairs, the hedge_leg APY is already signed correctly in net_apy

## 4. Pareto Frontier vs All Candidates

**WRONG:**
```python
# Don't assume pareto_frontier is the same as candidates
result = await DELTA_LAB_CLIENT.get_best_delta_neutral_pairs(basis_symbol="BTC")
best_by_apy = result["pareto_frontier"][0]  # May not be highest APY!
```

**RIGHT:**
```python
result = await DELTA_LAB_CLIENT.get_best_delta_neutral_pairs(basis_symbol="BTC")

# For highest APY (ignoring risk)
best_by_apy = result["candidates"][0]  # Already sorted by net_apy

# For risk-adjusted selection
pareto = result["pareto_frontier"]
# Pareto frontier contains optimal risk/return combinations
# May not include the absolute highest APY if it's too risky
```

Key differences:
- `candidates` - All pairs sorted by net_apy descending
- `pareto_frontier` - Subset of candidates on the Pareto frontier (risk-adjusted optimal)

## 5. Error Handling

**WRONG:**
```python
try:
    result = await DELTA_LAB_CLIENT.get_basis_apy_sources(basis_symbol="UNKNOWN")
    print(result)
except Exception:
    pass  # Silent failure
```

**RIGHT:**
```python
import httpx

try:
    result = await DELTA_LAB_CLIENT.get_basis_apy_sources(basis_symbol="BTC")
    print(result)
except httpx.HTTPStatusError as e:
    if e.response.status_code == 400:
        # Invalid params or unknown symbol
        error_data = e.response.json()
        print(f"Bad request: {error_data.get('error')}")
        if "suggestions" in error_data:
            print(f"Suggestions: {error_data['suggestions']}")
    elif e.response.status_code == 404:
        # Asset not found (get_asset only)
        print(f"Asset not found: {e.response.json()}")
    elif e.response.status_code == 500:
        # Server error
        print("Server error - try again later")
    else:
        raise
```

Status codes:
- 400 - Invalid parameters or unknown symbol
- 404 - Asset not found (get_asset only)
- 500 - Internal server error

## 6. Lookback Period

MCP uses 7 days (fixed). For custom lookback, use Python client. Short lookback (1-3 days) = recent but volatile. Long lookback (7-30 days) = smoothed averages.

## 7. Limit & Warnings

**MCP uses fixed limits:** 10 for apy-sources, 5 for delta-neutral. For more results, adjust limit in URI or use Python client.

**Always check warnings:**
```python
if result["warnings"]:
    print(f"Data quality issues: {result['warnings']}")
```
