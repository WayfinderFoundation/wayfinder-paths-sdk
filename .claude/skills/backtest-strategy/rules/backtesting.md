# Backtesting

## CRITICAL: Data Availability

**Oldest available: ~August 2025** (Delta Lab + Hyperliquid retain ~7 months).

---

## Existing strategy vs. new idea

**If backtesting a strategy that exists in `wayfinder_paths/strategies/`**, load `existing-strategies.md` — it has the full workflow for reading strategy source code, extracting real parameters, and faithfully reproducing signal logic. The generic helpers below are for quick prototyping of *new* ideas; existing strategies need their actual logic reproduced.

---

## Strategy type → helper (for new ideas / quick prototyping)

| Strategy | Helper | Load additional rule? |
|---|---|---|
| Perp/spot momentum, trend-following | `quick_backtest` | — |
| Perp/spot with explicit funding signal | `backtest_with_rates` | — |
| Delta-neutral basis carry | `backtest_delta_neutral` | — |
| Lending yield rotation | `backtest_yield_rotation` | `yield-strategies.md` |
| Carry trade (borrow/supply spread) | `backtest_carry_trade` | `yield-strategies.md` |
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
| `total_funding` | negative | Total funding paid — negative = income received (profit) |
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
    force_rebalance_if_overleveraged=False,
    periods_per_year=8760,     # CRITICAL: must match data interval
)
```

`periods_per_year` by interval:
- 1h → 8760 | 4h → 2190 | 1d → 365

All end-to-end helpers set this automatically.

`force_rebalance_if_overleveraged=False` means the backtester will **not** automatically bypass `rebalance_threshold` just because adverse price moves pushed current gross exposure above the configured leverage. Turn it on only when your intended simulation should proactively reduce gross exposure after an overleverage event. Leave it off when you want the strategy's normal rebalance cadence to remain the only driver of de-risking.

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

### Venue selection for `fetch_funding_rates`
`fetch_funding_rates` defaults to `venue="hyperliquid"`. The funding timeseries can contain
multiple venues per timestamp (e.g. `hyperliquid` and `hyperliquid-hyna`) with materially
different rates — mixing them would corrupt the series. Always think about which venue
you're targeting before calling this function:
```python
# Default — primary Hyperliquid perp market (correct for most strategies)
funding = await fetch_funding_rates(["BTC", "ETH"], start, end)

# Explicit — if backtesting a strategy on a specific alt venue
funding = await fetch_funding_rates(["XPL"], start, end, venue="hyperliquid-hyna")
```
To see which venues are available for a symbol, inspect the raw timeseries:
```python
data = await DELTA_LAB_CLIENT.get_asset_timeseries(symbol="XPL", series="funding", lookback_days=7, limit=1000)
print(data["funding"]["venue"].unique())
```

### Multi-venue funding backtests
`BacktestConfig.funding_rates` takes a single `[ts × symbol]` DataFrame — no venue dimension.
For a strategy comparing or routing across venues, fetch each venue separately and merge before passing:
```python
# Fetch both venues
hl_funding = await fetch_funding_rates(["XPL"], start, end, venue="hyperliquid")
hyna_funding = await fetch_funding_rates(["XPL"], start, end, venue="hyperliquid-hyna")

# Option A: always-best-venue — pick highest funding per timestamp
best_funding = pd.concat([hl_funding, hyna_funding]).groupby(level=0).max()

# Option B: use both in signal via closure — signal decides routing
def my_signal(prices, ctx, _hl=hl_funding, _hyna=hyna_funding):
    # compare _hl and _hyna, route accordingly, return weights
    ...

# Pass whichever merged series reflects the positions you'll actually hold
config = BacktestConfig(funding_rates=best_funding, ...)
```
`fetch_lending_rates` already returns `{ts × venue}` natively — multi-venue rotation is built in for lending.

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

## Not supported

### Known limitations of supported strategies

| Strategy | Limitation |
|---|---|
| **Carry trade** | Models `best_supply_rate - cheapest_borrow_rate` as a free spread. In reality, borrowing requires collateral whose price risk is unmodeled. Treat results as an upper bound on attainable carry. |
| **Delta-neutral** | The spot leg is modeled as idle capital with no return. In practice it could earn lending yield (e.g. supply ETH to Aave). Stated net return understates the real opportunity by the spot supply APR. |
| **Any strategy with `interval != "1h"` via Delta Lab** | Delta Lab is hourly-only. Non-hourly intervals are resampled down (`4h`, `1d`) or rejected (`1m`, `5m`, `15m` — use `source="hyperliquid"` instead). |

---

### Could be implemented but accuracy is low — do not attempt

| Strategy type | Why it's unreliable |
|---|---|
| LP / AMM (V2 full-range) | IL formula is exact, but `fee_income_rate` must be externally estimated — no historical fee/volume data. Result is sensitivity analysis, not a real simulation. |
| V3 concentrated liquidity | IL profile differs completely in/out of range; recentering events, range exits, and tick math are not modeled. |
| On-chain volume-dependent strategies | Any strategy whose signal depends on DEX volume, pool utilization, or TVL — data not available in Delta Lab. |

### Not possible — data doesn't exist

| Strategy type | Blocker |
|---|---|
| Tokens not in Delta Lab | `fetch_prices` will return no data. Check Delta Lab coverage before designing a strategy around a specific token. |
| CEX order book / microstructure | No order book history. Slippage and fill assumptions are rough estimates at best. |
| Options / structured products | No options pricing history. |
| Cross-chain bridge arbitrage | No bridge quote history or latency data. |
| Strategies requiring sub-hourly data | All data is hourly. 1-minute or tick-level signals cannot be backtested. |

---

## Production

After validation: `just create-strategy "Name"` → implement `deposit/update/status/withdraw/exit` → smoke tests → deploy small capital first.
