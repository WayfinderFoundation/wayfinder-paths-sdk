# Backtesting

## CRITICAL: Data Availability

**Oldest available: ~August 2025** (Delta Lab + Hyperliquid retain ~7 months).

---

## Strategy type → helper

| Strategy | Helper | Load additional rule? |
|---|---|---|
| Perp/spot momentum, trend-following | `quick_backtest` | — |
| Perp/spot with explicit funding signal | `backtest_with_rates` | — |
| Delta-neutral basis carry | `backtest_delta_neutral` | — |
| Lending yield rotation | `backtest_yield_rotation` | `yield-strategies.md` |
| Carry trade (borrow/supply spread) | `backtest_carry_trade` | `yield-strategies.md` |
| LP / AMM yield | `backtest_lp_position` | `lp-strategies.md` |
| Full control | `run_backtest` directly | — |

All helpers are in `wayfinder_paths.core.backtesting`.

---

## Quick examples

### Momentum
```python
from wayfinder_paths.core.backtesting import quick_backtest

def momentum(prices, ctx):
    returns = prices.pct_change(24)
    ranks = returns.rank(axis=1, pct=True)
    target = (ranks > 0.5).astype(float) - (ranks < 0.5).astype(float)
    return target / target.abs().sum(axis=1).fillna(1)

result = await quick_backtest(momentum, ["BTC", "ETH"], "2025-08-01", "2026-01-01", leverage=2.0)
```

### Delta-neutral
```python
from wayfinder_paths.core.backtesting import backtest_delta_neutral

result = await backtest_delta_neutral(
    ["BTC", "ETH"], "2025-08-01", "2026-01-01",
    funding_threshold=0.0001,  # 0.01% per hour — enter when funding is positive
    leverage=1.5,
)
# total_funding should be negative (income received)
```

### Yield rotation / carry → see `yield-strategies.md`
### LP → see `lp-strategies.md`

---

## Stats format

**All decimals (0-1 scale)** — format with `:.2%`:
- `total_return=0.45` → 45%
- `max_drawdown=-0.25` → -25%

### Key metrics

| Metric | Good | Notes |
|---|---|---|
| `sharpe` | >1.0; >2.0 excellent | Yield strategies often >3.0 |
| `max_drawdown` | near 0 | Yield: near-zero; perp: depends on vol |
| `trade_count` | low for yield | Each switch = gas cost |
| `total_funding` | negative | Income received in delta-neutral |
| `exposure_time_pct` | ~1.0 for carry | Fraction of time spread was positive |

### Red flags
- High `trade_count` in yield → gas dominates; increase `lookback_signal_days`
- `total_funding` positive in delta-neutral → paying funding (check sign convention)
- `liquidated=True` → reduce leverage
- High `volatility_ann` in delta-neutral → hedge is off

---

## Funding sign convention (CRITICAL)

```
Positive funding (+) → longs PAY shorts → SHORT perp RECEIVES  ✓
Negative funding (-) → shorts PAY longs → SHORT perp PAYS      ✗
```

---

## BacktestConfig (manual backtest)

```python
config = BacktestConfig(
    leverage=2.0,
    fee_rate=0.0004,           # 4bps per trade
    slippage_rate=0.0002,      # 2bps (use 0.0 for stablecoin deposits)
    funding_rates=funding_df,  # Optional DataFrame[timestamp × symbol]
    enable_liquidation=True,   # False for supply-only / LP strategies
    maintenance_margin_rate=0.05,
    periods_per_year=8760,     # CRITICAL: must match data interval
)
```

`periods_per_year` by interval:
- 1h → 8760 | 4h → 2190 | 1d → 365

All end-to-end helpers set this automatically.

---

## Gotchas

- **Look-ahead bias**: never use future data in signals
- **Wrong `periods_per_year`**: Sharpe/volatility will be meaningless; `quick_backtest` sets it automatically
- **Leveraged yield**: bake leverage into synthetic price, don't use `config.leverage`
- **`fetch_lending_rates`** returns per-venue data; `fetch_supply_rates`/`fetch_borrow_rates` return symbol-level averages

### Silent zero return (CRITICAL)
With `target_weight=1.0` and any `fee_rate > 0`, no trades will ever execute:
the cash check requires `initial_capital ≥ notional + fees`, but `1.0 + fees > 1.0`.
The backtester now warns when this happens. **For yield/lending strategies, set `fee_rate=0.0`
and `slippage_rate=0.0`** (encode switching costs as discrete events, or skip them for the
synthetic-price approach).

### Venue names for `fetch_lending_rates`
Venue keys include the chain suffix: `"moonwell-base"`, `"aave-v3-base"`, not just `"moonwell"`.
An unknown venue name now raises a `ValueError` listing available options. To discover:
```python
rates = await fetch_lending_rates("USDC", start, end)  # no venues filter
print(rates["supply"].columns.tolist())  # e.g. ['aave-v3-base', 'moonwell-base', ...]
```

### `align_dataframes` changes `periods_per_year`
`fetch_lending_rates`, `fetch_prices`, and `fetch_funding_rates` all return **hourly** data (8760/yr),
so aligning them is safe with no frequency mismatch. The warning fires if you mix custom
daily/weekly data with hourly series — update `periods_per_year` to match the resulting frequency.
Safer: resample each series to a common frequency before calling `align_dataframes`.

### Borrow rate anomalies
Lending rates include utilisation spikes (e.g. 100%+ APR during high demand).
Always inspect the distribution before using raw rates in a synthetic price:
```python
print(borrow_df.describe())          # check mean vs median
print(borrow_df.median())            # median is more robust than mean for spiky data
```

---

## Production

After validation: `just create-strategy "Name"` → implement `deposit/update/status/withdraw/exit` → smoke tests → deploy small capital first.
