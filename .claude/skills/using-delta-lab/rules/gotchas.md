# Gotchas

Common mistakes and important considerations when using Delta Lab.

## Quick Cheat Sheet

| ❌ Wrong | ✅ Right | Why |
|---------|---------|-----|
| `ok, data = await DELTA_LAB_CLIENT...` | `data = await DELTA_LAB_CLIENT...` | Clients return data directly, not tuples |
| `data["opportunities"]` | `data["directions"]["LONG"]` | Lending opps are in LONG direction |
| `candidate["net_apy"]["value"]` | `candidate["net_apy"]` | net_apy is a float, not a dict |
| `basis_symbol="bitcoin"` | `basis_symbol="BTC"` | Use root symbol, not coingecko ID |
| `max(opps, key=lambda x: x["apy"]["value"])` | `max([o for o in opps if o["apy"]["value"]], ...)` | APY can be null |
| Assuming delta-neutral = risk-free | Check `erisk_proxy` and understand risks | Still has funding, liquidation, smart contract risk |
| Using `candidates[0]` for lowest risk | Use `pareto_frontier` | Candidates sorted by APY, not risk |
| Ignoring `warnings` field | Always check `result["warnings"]` | Data quality issues affect decisions |
| `basis_symbol="usdc-base"` | `basis_symbol="USDC"` | Use symbol, not token ID |

## 0. Client Return Pattern & Response Structure

**CRITICAL: Delta Lab CLIENT returns data directly (not tuples).**

```python
# WRONG - Delta Lab CLIENT doesn't return tuples
ok, data = await DELTA_LAB_CLIENT.get_basis_apy_sources(...)  # ❌ ValueError!

# RIGHT - Clients return data directly
data = await DELTA_LAB_CLIENT.get_basis_apy_sources(...)  # ✅
```

See CLAUDE.md "Scripting gotchas #0" for full Client vs Adapter explanation.

### Response Structures

**`get_basis_apy_sources()` response:**

```python
{
  "as_of": "2026-02-13T16:00:00+00:00",
  "basis": {...},
  "summary": {...},
  "warnings": [],
  "directions": {
    "LONG": [  # ← Lending/supply opportunities
      {
        "apy": {"value": 0.048529, ...},  # ← DECIMAL form (4.853%)
        "venue": "morpho_ethereum",
        "instrument_type": "LENDING_SUPPLY",
        ...
      }
    ],
    "SHORT": [...]  # ← Borrowing opportunities
  }
}

# Access lending opportunities:
opportunities = data["directions"]["LONG"]

# NOT: data["opportunities"] ❌
```

**`get_best_delta_neutral_pairs()` response:**

```python
{
  "candidates": [
    {
      "net_apy": 1.546,  # ← Float, already % (NOT 0.01546!)
      "carry_leg": {
        "apy": {"value": 0.048529, ...},  # ← Decimal form
        "venue": "morpho_ethereum"
      },
      "hedge_leg": {
        "apy": {"value": 1.497886, ...},  # ← Already %
        "venue": "hyperliquid"
      }
    }
  ]
}

# Access net APY:
net_apy = candidate["net_apy"]  # Float, not candidate["net_apy"]["value"] ❌
```

### APY Format Inconsistency ⚠️

Different fields use different formats:

| Field | Format | Example | Display As |
|-------|--------|---------|------------|
| `carry_leg.apy.value` | Decimal | `0.048529` | `× 100` = 4.853% |
| `hedge_leg.apy.value` | Percentage | `1.497886` | As-is = 1.498% |
| `net_apy` | Percentage | `1.546` | As-is = 1.546% |

**Why?** Hedge funding rates come from Hyperliquid data which is already annualized in percentage form.

**Script pattern:**
```python
carry_apy = candidate["carry_leg"]["apy"]["value"] * 100  # ✅ Multiply
hedge_apy = candidate["hedge_leg"]["apy"]["value"]  # ✅ Don't multiply
net_apy = candidate["net_apy"]  # ✅ Don't multiply (and no ["value"]!)
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

## 3. Side vs Sign

**Don't confuse direction with sign:**

```python
# This is a LONG opportunity (you receive yield)
{
    "side": "LONG",
    "instrument_type": "lending",
    "apy": {"value": 0.08}  # Positive: you earn 8%
}

# This is also a LONG opportunity (you receive yield as short perp)
{
    "side": "LONG",
    "instrument_type": "perp",
    "apy": {"value": 0.12}  # Positive: you earn funding as short perp
}

# This is a SHORT opportunity (you pay yield)
{
    "side": "SHORT",
    "instrument_type": "lending",  # Borrowing
    "apy": {"value": -0.05}  # Negative: you pay 5%
}
```

- `side` indicates whether the opportunity is yield-generating (LONG) or yield-paying (SHORT)
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

## 6. Lookback Period Affects Results

**Short lookback (1-3 days):**
- More recent data
- May be volatile/noisy
- Better for short-term opportunities

**Long lookback (7-30 days):**
- Smoothed averages
- More stable estimates
- Better for long-term strategies
- Default is 7 days

```python
# Short-term (recent funding spike)
recent = await DELTA_LAB_CLIENT.get_basis_apy_sources(
    basis_symbol="BTC",
    lookback_days=1
)

# Long-term (smoothed average)
stable = await DELTA_LAB_CLIENT.get_basis_apy_sources(
    basis_symbol="BTC",
    lookback_days=30
)
```

## 7. Limit Parameter

**Don't assume you got everything:**

```python
# Default limit is 500
result = await DELTA_LAB_CLIENT.get_basis_apy_sources(basis_symbol="BTC")

# Check summary to see if you're missing data
total_count = sum(result["summary"]["instrument_type_counts"].values())
if total_count >= 500:
    print("Warning: Results may be truncated. Increase limit or filter by type.")
```

- `get_basis_apy_sources`: limit=500 (max 1000)
- `get_best_delta_neutral_pairs`: limit=20 (max 100)

## 8. Delta-Neutral Doesn't Mean Risk-Free

**Common misconception:**
```python
# This is delta-neutral (no price risk)
pair = result["candidates"][0]
print(f"Risk-free {pair['net_apy']:.2%} yield!")  # WRONG!
```

**Reality:**
Delta-neutral pairs eliminate **price risk** but still have:
- **Funding rate risk** - Funding can change (for floating legs)
- **Liquidation risk** - Margin requirements for perps/leverage
- **Smart contract risk** - Protocol exploits/failures
- **Liquidity risk** - Can't exit positions at expected prices
- **Execution risk** - Slippage, failed transactions
- **Correlation risk** - Imperfect hedges (different venues/chains)

The `erisk_proxy` metric attempts to capture these, but it's not comprehensive.

## 9. Cross-Chain/Venue Complexity

**Be aware of execution complexity:**

```python
pair = result["candidates"][0]

carry_venue = pair["carry_leg"]["venue"]  # "moonwell" (Base chain)
hedge_venue = pair["hedge_leg"]["venue"]  # "hyperliquid" (L1)

# This requires:
# 1. Funds on Base for Moonwell deposit
# 2. Funds on Arbitrum for Hyperliquid deposit (Bridge2)
# 3. Managing two separate positions on different chains
# 4. Cross-chain rebalancing if needed
```

Always check:
- `venue` - Different protocols
- `chain_id` - Different chains
- Asset compatibility (e.g., different USDC versions)

## 10. Maturity and Time Decay

**For fixed-term instruments:**

```python
opp = result["opportunities"][0]
maturity = opp.get("maturity_ts")

if maturity:
    # This is a fixed-term position (PT, fixed-rate market)
    # APY is for the full term, but time remaining affects:
    # - Entry attractiveness
    # - Exit liquidity
    # - Duration risk

    from datetime import datetime
    maturity_dt = datetime.fromisoformat(maturity.replace("Z", "+00:00"))
    now = datetime.now(maturity_dt.tzinfo)
    days_remaining = (maturity_dt - now).days

    if days_remaining < 7:
        print("Warning: Less than 7 days to maturity - check exit liquidity")
```

## 11. Instrument-Specific Considerations

### Perps
- Check `funding_rate_hourly_latest` vs `funding_rate_hourly_avg` for volatility
- High OI + low liquidity = liquidation risk

### Lending
- `supply_apr` can change rapidly based on utilization
- Check `tvl_usd` for protocol size/safety

### Fixed-rate (Boros)
- `implied_apy` is the locked rate - won't change after entry
- Compare to `funding_apy_est` (floating) for arbitrage
- Check `liquidity_usd` for exit ability

### PT/YT (Pendle)
- PT yield is fixed at entry based on discount to maturity
- YT yield depends on underlying rate - variable
- Check `maturity_ts` and `duration_risk`

## 12. Asset ID vs Asset Reference

**Don't mix internal IDs with API calls:**

```python
# Opportunity contains asset references
opp = result["opportunities"][0]

# WRONG - using the nested dict
deposit_asset = opp["opportunity"]["deposit_asset_id"]  # This is just an int!

# RIGHT - using the resolved asset
deposit_asset = opp["deposit_asset"]  # This is {"asset_id": 3, "symbol": "USDT"}
symbol = deposit_asset["symbol"]
asset_id = deposit_asset["asset_id"]

# If you need more details, use get_asset
details = await DELTA_LAB_CLIENT.get_asset(asset_id=asset_id)
# Now you have address, chain_id, decimals, coingecko_id
```

## 13. Warnings Field

**Always check warnings:**

```python
result = await DELTA_LAB_CLIENT.get_basis_apy_sources(basis_symbol="BTC")

if result["warnings"]:
    for warning in result["warnings"]:
        print(f"Warning: {warning}")
    # Examples:
    # - "Insufficient data for some opportunities"
    # - "Some venues temporarily unavailable"
    # - "Liquidity data may be stale"
```

Warnings indicate:
- Data quality issues
- Missing information
- Potential staleness
- Source availability problems

Don't ignore them - they affect decision quality.
