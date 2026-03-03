# Delta Lab Skill Verification

This document verifies that Claude can clearly understand WHAT, WHEN, and HOW to use the Delta Lab client.

## ✅ WHAT is Delta Lab?

**Clear definition provided:**
- Multi-protocol APY discovery and delta-neutral strategy research tool
- Aggregates opportunities from Hyperliquid, Moonwell, Boros, Pendle, Hyperlend, etc.
- Read-only (no execution, just discovery)

**Three core capabilities clearly documented:**
1. `get_basis_apy_sources()` - Find all yield opportunities for an asset
2. `get_best_delta_neutral_pairs()` - Find optimal carry/hedge combinations
3. `get_asset()` - Look up asset metadata by ID

## ✅ WHEN to use Delta Lab?

**Trigger phrases documented in SKILL.md:**
- "What are the best APYs for BTC/ETH?" → Use Delta Lab
- "Find me delta-neutral opportunities" → Use Delta Lab
- "What lending rates are available?" → Use Delta Lab + filter
- "Compare funding rates across venues" → Use Delta Lab + filter
- "Show me highest yield with lowest risk" → Use Delta Lab pareto_frontier

**Clear boundaries:**
- ✓ Use for: Discovery, comparison, analysis
- ✗ Don't use for: Execution (Delta Lab is read-only)

## ✅ HOW to use Delta Lab?

### Immediately actionable code snippets

**SKILL.md provides instant TL;DR:**
```python
from wayfinder_paths.core.clients import DELTA_LAB_CLIENT

# Find all yield opportunities
await DELTA_LAB_CLIENT.get_basis_apy_sources(basis_symbol="BTC", lookback_days=7)

# Find best delta-neutral pairs
await DELTA_LAB_CLIENT.get_best_delta_neutral_pairs(basis_symbol="ETH", limit=20)

# Look up asset metadata
await DELTA_LAB_CLIENT.get_asset(asset_id=1)
```

### Critical gotchas highlighted upfront

**From SKILL.md:**
- Use uppercase symbols: `"BTC"` not `"bitcoin"` or `"btc"`
- APY can be `null` - always filter
- Delta Lab is read-only

**From gotchas.md cheat sheet:**
| ❌ Wrong | ✅ Right | Why |
|---------|---------|-----|
| `basis_symbol="bitcoin"` | `basis_symbol="BTC"` | Use root symbol |
| `max(opps, key=lambda x: x["apy"]["value"])` | Filter nulls first | APY can be null |
| Assuming delta-neutral = risk-free | Check `erisk_proxy` | Still has risks |

### Common patterns documented

**high-value-reads.md provides:**
- Find highest APY for an asset (with null filtering)
- Find best delta-neutral by net APY
- Find best Pareto-optimal pair (risk-adjusted)
- Compare opportunities across protocols
- Group by venue/instrument type

### Response structure fully documented

**response-structures.md covers:**
- Every field in Opportunity object
- APY components breakdown
- Risk metrics interpretation
- Delta-neutral candidate structure
- Example pair compositions

## ✅ Verification Checklist

- [x] Claude knows WHAT Delta Lab is (discovery tool, read-only)
- [x] Claude knows WHEN to use it (trigger phrases mapped to methods)
- [x] Claude knows HOW to use it (code snippets + common patterns)
- [x] Critical gotchas are front-and-center (symbols, null APYs, read-only)
- [x] Common mistakes have clear WRONG vs RIGHT examples
- [x] Response structures are fully documented
- [x] Real-world usage patterns provided
- [x] Quick reference table maps user questions to methods
- [x] Skill loads successfully
- [x] Client tested with real data (BTC/ETH verified)

## 📋 Test Scenarios

### Scenario 1: User asks "What's the best APY for BTC?"
**Claude should:**
1. Recognize this triggers Delta Lab
2. Use `get_basis_apy_sources(basis_symbol="BTC")`
3. Filter `directions.LONG` for yield-generating opportunities
4. Filter out null APYs: `[o for o in opps if o["apy"]["value"] is not None]`
5. Sort by `apy.value` descending
6. Present top results with venue and instrument type

**Documented in:** SKILL.md (trigger mapping), high-value-reads.md (code pattern), gotchas.md (null filtering)

### Scenario 2: User asks "Find me a delta-neutral BTC strategy"
**Claude should:**
1. Recognize this is a delta-neutral query
2. Use `get_best_delta_neutral_pairs(basis_symbol="BTC")`
3. Present `candidates[0]` for highest net APY
4. Explain carry leg (yield source) and hedge leg (price hedge)
5. Mention net APY is combined after hedging costs
6. Note that delta-neutral doesn't mean risk-free (check erisk_proxy)

**Documented in:** SKILL.md (trigger mapping), high-value-reads.md (usage pattern), response-structures.md (structure), gotchas.md (risk warning)

### Scenario 3: User asks "Compare Moonwell vs Hyperliquid lending rates for ETH"
**Claude should:**
1. Use `get_basis_apy_sources(basis_symbol="ETH")`
2. Filter opportunities by venue and instrument_type
3. Compare APYs across venues
4. Check warnings field for data quality

**Documented in:** high-value-reads.md (filtering pattern), response-structures.md (opportunity structure)

## 🎯 Conclusion

The Delta Lab skill provides:
- ✅ Clear WHAT (definition, capabilities, boundaries)
- ✅ Clear WHEN (trigger phrases, use cases)
- ✅ Clear HOW (code snippets, patterns, examples)
- ✅ Critical gotchas upfront (symbols, nulls, read-only)
- ✅ Complete reference (structures, components, interpretations)

**Claude has everything needed to use Delta Lab correctly and confidently.**
