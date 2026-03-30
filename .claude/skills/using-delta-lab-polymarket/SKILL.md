---
name: using-delta-lab-polymarket
description: How to use the Polymarket backtesting data layer — wallet trade history, hourly token prices, and market metadata — for copy-trade backtesting and strategy research.
metadata:
  tags: polymarket, copy-trade, woi, backtesting, trade-history, prices
---

## What you need to know (TL;DR)

**Polymarket backtesting data = historical trade + price data for copy-trade research.**

Data is fetched via the `PolymarketAdapter` (Data API + Goldsky + CLOB). Functions live in `core/backtesting/polymarket_data.py`.

```python
from wayfinder_paths.adapters.polymarket_adapter import PolymarketAdapter
from wayfinder_paths.core.backtesting.polymarket_data import (
    fetch_wallet_trades,
    fetch_market_prices,
    fetch_market_metadata,
)
from wayfinder_paths.core.backtesting.polymarket_backtester import run_polymarket_backtest
from wayfinder_paths.core.backtesting.polymarket_helpers import flat_dollar_sizer

adapter = PolymarketAdapter(config={})

# What did a specific trader do?
trades_df = await fetch_wallet_trades(
    wallet_address="0xabc...",
    start_date="2025-10-01",
    end_date="2025-12-01",
    adapter=adapter,
)

# Get hourly prices for the markets they traded
token_ids = trades_df["token_id"].unique().tolist()
prices_df = await fetch_market_prices(
    token_ids=token_ids,
    start_date="2025-10-01",
    end_date="2025-12-01",
    adapter=adapter,
)

# Now run the backtest
result = run_polymarket_backtest(trades_df, prices_df, sizing_fn=flat_dollar_sizer(20.0))

await adapter.close()
```

**Critical gotchas:**
- `fetch_wallet_trades` / `fetch_market_prices` / `fetch_market_metadata` are async functions in `polymarket_data.py`, not client methods
- `trades_df` index is `timestamp` (UTC-aware `DatetimeIndex`)
- `prices_df` columns are **token_ids** (CLOB numeric strings, e.g. `"71321045..."`)
- All dates are ISO 8601 strings; accepts `"2025-10-01"` or `"2025-10-01T00:00:00Z"`
- `prices_df` uses "strictly before" convention: row at time `t` = last observation before `t`

## When to use this skill

- Fetching data for `run_polymarket_backtest` / `compare_sizing_strategies`
- Getting market metadata (question text, resolution status) for a set of condition_ids
- Building strategy scoring that needs historical Polymarket trade flows

## How to use

- [rules/client-reference.md](rules/client-reference.md) — All data functions: signatures, params, return shapes
- [rules/gotchas.md](rules/gotchas.md) — ID confusion, date ranges, empty DataFrames, resolution detection, fee model
