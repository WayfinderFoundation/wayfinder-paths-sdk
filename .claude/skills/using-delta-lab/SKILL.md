---
name: using-delta-lab
description: How to use the Delta Lab client (DELTA_LAB_CLIENT) for basis APY discovery, delta-neutral pair finding, lending/perp/price screening, time-series, and opportunity analysis across protocols. TRIGGER when the user asks about yields, APYs, basis, funding, delta-neutral, top-apy, lending rates, perp funding, borrow routes, or opportunity screening; OR when writing/editing a script that imports `DELTA_LAB_CLIENT` or uses `wayfinder://delta-lab/...` MCP URIs; OR before any yield/basis/funding research. The MCP surface is intentionally narrow (snapshots only) — anything time-series, by-asset-id, plotting, or bulk requires the Python client documented here.
metadata:
  tags: wayfinder, delta-lab, basis, delta-neutral, apy, opportunities, yield, funding, lending, perp, screening, timeseries
---

## What you need to know (TL;DR)

**Delta Lab = Multi-protocol APY discovery tool**

```python
from wayfinder_paths.core.clients.DeltaLabClient import DELTA_LAB_CLIENT

# Core discovery
await DELTA_LAB_CLIENT.get_basis_apy_sources(basis_symbol="BTC", lookback_days=7)   # analytic opps
await DELTA_LAB_CLIENT.get_best_delta_neutral_pairs(basis_symbol="ETH", limit=20)

# v2 surface (also on DELTA_LAB_CLIENT — see rules/v2-surface.md for the full list)
await DELTA_LAB_CLIENT.search_opportunities(basis_root="ETH", side="LONG", limit=25)  # discovery shape
await DELTA_LAB_CLIENT.get_asset_price_latest(asset_id=2)          # typed: PriceLatest | None
await DELTA_LAB_CLIENT.get_market_lending_latest(market_id=912, asset_id=2)  # full screen record
await DELTA_LAB_CLIENT.explore(symbol="ETH", relations_depth=1)    # one-shot bundle

# Typed error envelope
from wayfinder_paths.core.clients.delta_lab_types import DeltaLabAPIError
```

**Critical gotchas:**
- Use uppercase symbols: `"BTC"` not `"bitcoin"` or `"btc"`
- APY can be `null` - always filter: `[o for o in opps if o["apy"]["value"] is not None]`
- Delta Lab is **read-only** (no execution, just discovery)
- Two "opportunity" shapes: `search_opportunities` = trimmed discovery (~14 fields, scan); `get_basis_apy_sources` = enriched analytic (apy/risk/summary, decide). Don't mix them up.
- `*_latest(...)` returns `None` on sparse-data 404 (not an error). Use `DeltaLabAPIError` for real failures.
- Default `limit=25` on search/list/opportunity calls keeps agent context under 10 KB. `get_basis_apy_sources(limit=500)` is 1.3 MB — cap low.

## When to use

Use this skill when you are:
- Discovering basis opportunities for a given asset (BTC, ETH, etc.)
- Finding best delta-neutral pair candidates
- Analyzing APY sources across different protocols and venues
- Understanding risk metrics and carry/hedge leg compositions
- Comparing rates across Hyperliquid, Moonwell, Boros, Pendle, etc.

## How to use

- [rules/what-is-delta-lab.md](rules/what-is-delta-lab.md) - Mental model: what Delta Lab is, basis symbols, and data sources
- [rules/high-value-reads.md](rules/high-value-reads.md) - Core queries: APY sources, delta-neutral pairs, asset lookups
- [rules/response-structures.md](rules/response-structures.md) - Understanding opportunities, APY components, and risk metrics
- [rules/gotchas.md](rules/gotchas.md) - Common mistakes, symbol resolution, and filtering
- [rules/v2-surface.md](rules/v2-surface.md) - Expanded client: entity / catalog / graph / search / TS+latest / bulk / `explore` + `fetch_backtest_bundle`. Typed records, error class, context-size guardrails.
