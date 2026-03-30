# Polymarket Backtesting Data — Function Reference

All functions are async. They live in `wayfinder_paths.core.backtesting.polymarket_data`.

```python
from wayfinder_paths.core.backtesting.polymarket_data import (
    fetch_wallet_trades,
    fetch_market_prices,
    fetch_market_metadata,
)
```

---

## 1. `fetch_wallet_trades` — Historical trade feed for a wallet

```python
trades_df = await fetch_wallet_trades(
    wallet_address: str,     # EVM address (case-insensitive)
    start_date: str,         # ISO 8601: "2025-10-01"
    end_date: str,           # ISO 8601: "2025-12-01"
    adapter: PolymarketAdapter | None = None,  # Reuse an existing adapter
) -> pd.DataFrame
```

**Data sources:** Polymarket Data API (`/activity`) for recent trades, Goldsky subgraph for historical trades on resolved markets. Goldsky fills gaps when the Data API doesn't cover the full date range.

### Returns: `pd.DataFrame` indexed by `timestamp` (UTC `DatetimeIndex`)

| Column | dtype | Description |
|--------|-------|-------------|
| `woi_address` | str | The wallet address (lowercased) |
| `condition_id` | str | Polymarket condition ID (hex, e.g. `"0x1a2b..."`) |
| `token_id` | str | CLOB asset ID — numeric string (e.g. `"71321045..."`) |
| `outcome` | str | `"Yes"` or `"No"` (or custom label for multi-outcome markets) |
| `side` | str | `"BUY"` or `"SELL"` |
| `usdc_amount` | float | USDC spent (BUY) or received before fees (SELL) |
| `share_count` | float | Number of shares |
| `avg_price` | float | USDC per share, range `[0, 1]` |
| `market_slug` | str | Human-readable slug (e.g. `"trump-wins-2024"`) |
| `tx_hash` | str | On-chain transaction hash — deduplication key |

**Empty result:** Returns an empty DataFrame with all columns present (never raises when no trades found).

### Example

```python
adapter = PolymarketAdapter(config={})
trades_df = await fetch_wallet_trades(
    "0xabc123...", "2025-10-01", "2025-12-01", adapter=adapter,
)
buys = trades_df[trades_df["side"] == "BUY"]
print(f"Avg entry price: {buys['avg_price'].mean():.3f}")
await adapter.close()
```

---

## 2. `fetch_market_prices` — Hourly CLOB price history

```python
prices_df = await fetch_market_prices(
    token_ids: list[str],    # CLOB asset IDs (from trades_df["token_id"].unique())
    start_date: str,         # ISO 8601
    end_date: str,           # ISO 8601
    fidelity: int = 60,      # CLOB candle interval in minutes
    max_gap_hours: int | None = None,  # Gaps longer than this → NaN
    adapter: PolymarketAdapter | None = None,
) -> pd.DataFrame
```

### Returns: `pd.DataFrame` indexed by `timestamp` (UTC `DatetimeIndex`), columns = token_ids

**Convention: row at time `t` = last price observation strictly before `t`.** No lookahead — at time `t` you only see prices that were known before `t`.

**NaN values:** The first grid point has no prior observation → NaN. If `max_gap_hours` is set, periods with no observation within that window are also NaN.

**Resolution:** When a market resolves, the CLOB price converges to `1.0` (YES) or `0.0` (NO).

### Example

```python
token_ids = trades_df["token_id"].unique().tolist()
prices_df = await fetch_market_prices(
    token_ids, "2025-10-01", "2025-12-01", adapter=adapter,
)

from wayfinder_paths.core.backtesting.polymarket_data import detect_resolutions
resolutions = detect_resolutions(prices_df)  # {token_id: 0.0 or 1.0}
```

---

## 3. `fetch_market_metadata` — Condition-id → market info

```python
markets = await fetch_market_metadata(
    condition_ids: list[str],  # Polymarket condition IDs
    adapter: PolymarketAdapter | None = None,
) -> dict[str, dict]           # {condition_id: MarketMeta}
```

**Data source:** Polymarket Gamma API.

### Returns: `dict[str, dict]` keyed by `condition_id`

| Field | type | Description |
|-------|------|-------------|
| `condition_id` | str | Same as the key |
| `market_slug` | str | Human-readable slug |
| `question` | str | Full question text |
| `end_date_iso` | str | Scheduled resolution date (ISO 8601) |
| `resolved` | bool | Whether on-chain resolution has occurred |
| `volume_usdc` | float | All-time total volume in USDC |
| `outcomes` | list[str] | e.g. `["Yes", "No"]` |
| `tokens` | list[dict] | `[{"token_id": str, "outcome": str}, ...]` |

**Missing condition_ids:** Omitted from the returned dict.

### Example

```python
cond_ids = trades_df["condition_id"].unique().tolist()
markets = await fetch_market_metadata(cond_ids, adapter=adapter)

# Build resolution_prices for the backtester (keyed by condition_id)
from wayfinder_paths.core.backtesting.polymarket_data import detect_resolutions
resolution_prices = detect_resolutions(prices_df)  # {token_id: 0.0 or 1.0}
```

---

## Complete copy-trade research workflow

```python
from wayfinder_paths.adapters.polymarket_adapter import PolymarketAdapter
from wayfinder_paths.core.backtesting.polymarket_backtester import (
    run_polymarket_backtest, compare_sizing_strategies,
)
from wayfinder_paths.core.backtesting.polymarket_data import (
    fetch_wallet_trades, fetch_market_prices, fetch_market_metadata,
)
from wayfinder_paths.core.backtesting.polymarket_helpers import (
    flat_dollar_sizer, flat_ratio_sizer, proportional_sizer,
)

adapter = PolymarketAdapter(config={})

# 1. Fetch trade history
woi = "0xabc123..."
trades_df = await fetch_wallet_trades(woi, "2025-09-01", "2025-12-01", adapter=adapter)

# 2. Get prices for all traded markets
token_ids = trades_df["token_id"].unique().tolist()
prices_df = await fetch_market_prices(token_ids, "2025-09-01", "2025-12-01", adapter=adapter)

# 3. Get market metadata
cond_ids = trades_df["condition_id"].unique().tolist()
markets = await fetch_market_metadata(cond_ids, adapter=adapter)

# 4. Compare sizing strategies
results = await compare_sizing_strategies(
    woi_addresses=[woi],
    sizing_fns={
        "flat_$20": flat_dollar_sizer(20.0),
        "5pct_woi": flat_ratio_sizer(0.05, max_order=50.0),
        "prop_10pct": proportional_sizer(0.10),
    },
    trades_df=trades_df,
    prices_df=prices_df,
)

for name, res in results.items():
    s = res.stats
    print(f"{name:15s}: return={s['total_return']:+.1%}  trades={s['trade_count']}")

await adapter.close()
```
