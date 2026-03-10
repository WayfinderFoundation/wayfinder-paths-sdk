# Backtesting LP / AMM Strategies

## What the model does and doesn't do

**IL formula**: `2*sqrt(r)/(1+r) - 1` where `r` = current/initial price ratio.
This is the exact standard formula for V2 full-range 50/50 constant-product pools.
IL is path-independent for CPMMs — only start and end price ratio matter.

**Limitations — be explicit with users:**
- V2 full-range 50/50 only. V3 concentrated liquidity is not modeled (different IL profile in/out of range).
- `fee_income_rate` is an external estimate — no historical fee data available. This is "what if fees were X%" sensitivity analysis, not a real simulation.
- No gas costs for entering/exiting positions.

## Quick start

```python
from wayfinder_paths.core.backtesting import backtest_lp_position, simulate_il, fetch_prices

# Check IL drag first
prices = await fetch_prices(["ETH", "USDC"], "2025-08-01", "2026-01-01")
il = simulate_il(prices, ("ETH", "USDC"))
print(f"IL at end: {il.iloc[-1]:.2%}")   # e.g. -0.08 = 8% drag vs holding

# Then test break-even
result = await backtest_lp_position(
    pool_assets=("ETH", "USDC"),
    start_date="2025-08-01",
    end_date="2026-01-01",
    fee_income_rate=0.25,  # 25% APY — must be estimated from pool analytics
)
print(f"LP:   {result.stats['total_return']:.2%}")
print(f"Hold: {result.stats['buy_hold_return']:.2%}")
```

## Estimating fee_income_rate

No data source exists for this — estimate from pool analytics dashboards:

```
fee_income_rate ≈ (24h_volume × fee_tier) / TVL × 365
```

Typical ranges: stable/stable 2–10%, ETH/stable 10–30%, volatile pairs 20–100%+.

## Break-even analysis

```python
il = simulate_il(prices, ("ETH", "USDC"))
years = len(prices) / 8760
breakeven_fee = (-float(il.iloc[-1])) / years
print(f"Need >{breakeven_fee:.2%} fee APY to break even vs holding")
```

## Manual build

```python
from wayfinder_paths.core.backtesting import build_lp_price_index, run_backtest, BacktestConfig

lp_prices = build_lp_price_index(prices, ("ETH", "USDC"), fee_income_rate=0.25, periods_per_year=8760)
target = pd.DataFrame({"LP_ETH_USDC": 1.0}, index=lp_prices.index)
config = BacktestConfig(enable_liquidation=False, periods_per_year=8760)
result = run_backtest(lp_prices, target, config)
```
