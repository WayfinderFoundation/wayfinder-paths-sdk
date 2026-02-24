# Backtesting

## Quick Start

```python
from wayfinder_paths.core.backtesting import quick_backtest

def my_strategy(prices, ctx):
    """Return target positions DataFrame: index=timestamps, columns=symbols, values=weights [-1,1]"""
    returns = prices.pct_change(24)
    ranks = returns.rank(axis=1, pct=True)
    target = (ranks > 0.5).astype(float) - (ranks < 0.5).astype(float)
    return target / target.abs().sum(axis=1).fillna(1)

result = await quick_backtest(
    strategy_fn=my_strategy,
    symbols=["BTC", "ETH"],
    start_date="2025-08-01",
    end_date="2025-12-01",
    leverage=2.0,
    interval="1h"
)

print(f"Sharpe: {result.stats['sharpe']:.2f}")
print(f"Return: {result.stats['total_return']:.2%}")
print(f"Max DD: {result.stats['max_drawdown']:.2%}")
```

## CRITICAL: Data Availability

**Oldest available data: ~July 2025** (Delta Lab + Hyperliquid).

```python
# ✓ Safe - within available range
start_date = "2025-08-01"

# ✗ Will fail - before July 2025
start_date = "2025-01-01"
```

## Strategy Function

Returns DataFrame (index=timestamps, columns=symbols, values=weights):
- `1.0` = 100% long, `-1.0` = 100% short, `0.0` = flat
- Weights scaled by `leverage` in config
- Should sum to ≤1.0 per row

## Example Patterns

```python
# Momentum
def momentum(prices, ctx):
    returns = prices.pct_change(24)
    ranks = returns.rank(axis=1, pct=True)
    target = (ranks > 0.5).astype(float) - (ranks < 0.5).astype(float)
    return target / target.abs().sum(axis=1).fillna(1)

# Carry (funding harvesting)
def carry(prices, funding, ctx):
    # CRITICAL: Negative funding means shorts PAY longs (bad for shorts)
    # Positive funding means longs pay shorts (good for shorts)
    target = -(funding > 0.01).astype(float)  # Short when positive funding
    return target / target.abs().sum(axis=1).fillna(1)
```

## CRITICAL: periods_per_year

`quick_backtest` sets this automatically. Manual `run_backtest` requires:
- 1h: 8760 (365×24)
- 4h: 2190 (365×6)
- 1d: 365

Wrong value = meaningless Sharpe/volatility.

## Stats Format

**CRITICAL**: All stats are **decimals (0-1 scale)**, not percentages:
- `total_return=0.45` = 45% return
- `max_drawdown=-0.25` = -25% drawdown

Use `:.2%` format to display.

### Key Metrics

- `sharpe` - >1.0 good, >2.0 excellent
- `total_return` - cumulative return (decimal)
- `max_drawdown` - peak-to-trough (decimal)
- `win_rate` - fraction of winning periods
- `profit_factor` - gross profit / gross loss (>1.5 good)

### Red Flags

- High turnover → excessive costs
- Liquidations → reduce leverage
- High max drawdown → too risky
- Negative funding PnL → funding costs exceed trading profit

## BacktestConfig (Manual Method)

```python
config = BacktestConfig(
    leverage=2.0,
    fee_rate=0.0004,  # 0.04%
    slippage_rate=0.0002,
    funding_rates=funding_df,  # Optional
    enable_liquidation=True,
    maintenance_margin_rate=0.05,  # 5%
    periods_per_year=8760  # CRITICAL: must match interval
)

# Symbol-specific margins (optional)
config = BacktestConfig(
    maintenance_margin_by_symbol={
        "BTC": 1/100.0,  # 1% (100x max)
        "ETH": 1/50.0,   # 2% (50x max)
    }
)
```

## Gotchas

- Look-ahead bias - using future data in signals
- Unrealistic costs - ignoring fees/slippage/funding
- Overfitting - too many params tuned to one period
- Data quality - verify no gaps/spikes before running

## Production

After validation: `just create-strategy "Name"` → implement Strategy interface → write tests → deploy with small capital first.
