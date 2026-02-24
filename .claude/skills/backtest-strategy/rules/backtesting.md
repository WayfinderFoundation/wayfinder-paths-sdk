# Backtesting Strategies

Guide for using the backtesting framework to validate strategy ideas before production deployment.

## Quick Start

To backtest a strategy idea:

1. **Define your strategy logic** as a function that takes prices and returns target positions
2. **Use `quick_backtest()`** to run it with automatic data fetching
3. **Analyze results** using the returned stats and metrics

## Workflow

```
Strategy Idea → Define Signal Logic → Fetch Data → Run Backtest → Analyze → Iterate
```

Once validated via backtesting, promote to a full Strategy class in `wayfinder_paths/strategies/`.

## Data Retention Limits (CRITICAL)

**Both Delta Lab and Hyperliquid have approximately 7 months (~211 days) of historical data retention.**

When selecting date ranges for backtests:
- ✓ Use dates within the last 7 months from today
- ✗ Avoid dates older than ~7 months - they will return empty data
- As of Feb 2026, oldest available date: ~July 2025

Example of **correct** date selection:
```python
from datetime import datetime, timedelta
end_date = datetime.now()
start_date = end_date - timedelta(days=180)  # 6 months - safe
```

Example of **incorrect** date selection (will fail):
```python
start_date = "2025-01-01"  # Too old, outside retention window
end_date = "2025-02-01"
```

If you get "No price data found" errors, verify your date range is within the retention window.

## Data Fetching

Available helpers in `wayfinder_paths.core.backtesting`:

- `fetch_prices(symbols, start, end, interval)` - Price data (OHLC close prices)
- `fetch_funding_rates(symbols, start, end)` - Perpetual futures funding rates
- `fetch_borrow_rates(symbols, start, end, protocol)` - Lending protocol borrow rates
- `align_dataframes(*dfs, method)` - Align multiple DataFrames to common timestamps

All return DataFrames in backtest-ready format:
- **Index**: DatetimeIndex (timestamps)
- **Columns**: Symbols
- **Values**: Prices, rates, etc.

## Signal Generation

Your strategy function must return a **target positions DataFrame**:

- **Index**: timestamps (matching input prices)
- **Columns**: symbols (matching input prices)
- **Values**: weights in `[-1, 1]` range
  - `1.0` = 100% long
  - `-1.0` = 100% short
  - `0.0` = flat/no position

Weights are scaled by the `leverage` parameter in BacktestConfig.

**Important**: Weights should sum to ≤1.0 per row (unless you want to use cash reserves).

## Common Strategy Patterns

### 1. Basis Trading (Spot + Short Perp)

```python
def basis_strategy(prices, ctx):
    """Trade basis spread between spot and perp."""
    spot = prices["BTC-SPOT"]
    perp = prices["BTC-PERP"]
    spread = (perp - spot) / spot

    # Long basis when spread > threshold (long spot, short perp)
    threshold = 0.02  # 2%

    target = pd.DataFrame(index=prices.index)
    target["BTC-SPOT"] = (spread > threshold).astype(float)
    target["BTC-PERP"] = -(spread > threshold).astype(float)

    return target
```

### 2. Momentum (Cross-Sectional)

```python
def momentum_strategy(prices, ctx):
    """Buy winners, sell losers."""
    lookback = 24  # 24 periods
    returns = prices.pct_change(lookback)

    # Rank assets by returns
    ranks = returns.rank(axis=1, pct=True)

    # Long top 50%, short bottom 50%
    target = (ranks > 0.5).astype(float) - (ranks < 0.5).astype(float)

    # Equal weight within longs and shorts
    target = target.div(target.abs().sum(axis=1), axis=0).fillna(0)

    return target
```

### 3. Mean Reversion

```python
def mean_reversion_strategy(prices, ctx):
    """Fade short-term moves, revert to mean."""
    lookback = 48  # 48 hour lookback

    # Z-score of recent returns
    returns = prices.pct_change()
    rolling_mean = returns.rolling(lookback).mean()
    rolling_std = returns.rolling(lookback).std()
    zscore = (returns - rolling_mean) / rolling_std

    # Short overbought, long oversold
    target = -zscore.clip(-2, 2) / 2  # Normalize to [-1, 1]
    target = target.fillna(0)

    return target
```

### 4. Carry (Funding Rate Harvesting)

```python
async def carry_strategy_with_funding(prices, funding, ctx):
    """Collect funding from high-rate shorts."""
    # Short assets with positive funding (longs pay shorts)
    target = -(funding > 0.01).astype(float)

    # Equal weight across shorts
    target = target.div(target.abs().sum(axis=1), axis=0).fillna(0)

    return target

# Use backtest_with_rates for strategies that need funding data
from wayfinder_paths.core.backtesting import backtest_with_rates

result = await backtest_with_rates(
    strategy_fn=carry_strategy_with_funding,
    symbols=["BTC", "ETH"],
    start_date="2025-01-01",
    end_date="2025-02-01"
)
```

## Running Backtests

### Quick Method (Recommended for Agents)

```python
from wayfinder_paths.core.backtesting import quick_backtest

result = await quick_backtest(
    strategy_fn=my_strategy,
    symbols=["BTC", "ETH", "SOL"],
    start_date="2025-01-01",
    end_date="2025-02-01",
    interval="1h",  # 1m, 5m, 15m, 1h, 4h, 1d
    leverage=2.0,
    include_funding=True  # Auto-fetch and apply funding rates
)

# Stats are in decimal format (0-1 scale)
print(f"Return: {result.stats['total_return']:.2%}")  # 0.45 = 45%
print(f"Sharpe: {result.stats['sharpe']:.2f}")
print(f"Max DD: {result.stats['max_drawdown']:.2%}")  # -0.25 = -25%

result.equity_curve.plot()
```

**Important**: `quick_backtest` automatically:
- Sets `periods_per_year` based on interval (8760 for 1h, 365 for 1d, etc.)
- Builds context dict: `{"symbols": [...], "interval": "1h", ...}`
- Your strategy function receives this context as second parameter

### Manual Method (Full Control)

```python
from wayfinder_paths.core.backtesting import (
    fetch_prices,
    fetch_funding_rates,
    run_backtest,
    BacktestConfig,
)

# Fetch data
prices = await fetch_prices(["BTC", "ETH"], "2025-01-01", "2025-02-01", interval="1h")
funding = await fetch_funding_rates(["BTC", "ETH"], "2025-01-01", "2025-02-01")

# Generate signals
target_positions = my_strategy(prices, {"symbols": ["BTC", "ETH"]})

# Configure and run
config = BacktestConfig(
    leverage=2.0,
    fee_rate=0.0004,  # 0.04% per trade
    slippage_rate=0.0002,  # 0.02% slippage
    funding_rates=funding,
    enable_liquidation=True,
    maintenance_margin_rate=0.05,  # 5% maintenance margin
    periods_per_year=8760  # CRITICAL: 365*24 for hourly data
)

result = run_backtest(prices, target_positions, config)
```

**CRITICAL**: Always set `periods_per_year` correctly for your interval:
- 1-minute bars: 525600 (365 × 24 × 60)
- 5-minute bars: 105120 (365 × 24 × 12)
- 15-minute bars: 35040 (365 × 24 × 4)
- 1-hour bars: 8760 (365 × 24)
- 4-hour bars: 2190 (365 × 6)
- Daily bars: 365

Wrong `periods_per_year` will make Sharpe ratio and volatility meaningless.

## Interpreting Results

### Stats Schema

**IMPORTANT**: All rate/return stats are in **decimal format (0-1 scale)**:
- 0.45 = 45% return
- -0.25 = -25% drawdown
- 0.55 = 55% win rate

Use Python's `:.2%` format specifier to display as percentages.

Complete stats dictionary keys:

| Key | Type | Description | Good Value | Format |
|-----|------|-------------|------------|--------|
| `sharpe` | float | Risk-adjusted returns | >1.0 good, >2.0 excellent | `.2f` |
| `sortino` | float | Downside-only Sharpe | >1.0 good, >2.0 excellent | `.2f` |
| `cagr` | float | Annualized return | Varies by strategy | `:.2%` |
| `total_return` | float | Cumulative return | Positive | `:.2%` |
| `max_drawdown` | float | Peak-to-trough decline | <-0.20 (less than -20%) | `:.2%` |
| `win_rate` | float | Fraction of winning periods | >0.50 (50%+) | `:.2%` |
| `profit_factor` | float\|None | Gross profit / gross loss | >1.5 | `.2f` |
| `volatility` | float | Annualized volatility | Depends on strategy | `:.2%` |
| `avg_turnover` | float | Average portfolio turnover | Lower is better | `.4f` |
| `avg_cost` | float | Average transaction cost | Lower is better | `.6f` |
| `trade_count` | int | Number of rebalances | Sufficient activity | `d` |
| `final_equity` | float | Ending portfolio value | >initial_capital | `.4f` |

**Example Usage:**
```python
stats = result.stats

# Correct formatting
print(f"Return: {stats['total_return']:.2%}")      # "45.20%"
print(f"Sharpe: {stats['sharpe']:.2f}")            # "3.31"
print(f"Max DD: {stats['max_drawdown']:.2%}")      # "-36.78%"
print(f"Win Rate: {stats['win_rate']:.2%}")        # "42.55%"
print(f"Volatility: {stats['volatility']:.2%}")    # "109.49%"
print(f"Trades: {stats['trade_count']}")           # "1624"
```

### Red Flags

❌ **High turnover** → Excessive trading, high transaction costs eating returns
❌ **Liquidations** → Strategy too aggressive, reduce leverage
❌ **Low trade count** → Insufficient signal frequency, strategy may not be active enough
❌ **High max drawdown** → Too risky, consider position sizing or stop-loss
❌ **Negative funding PnL** → Funding costs exceeding trading PnL (for perp strategies)

### Accessing Detailed Results

```python
# Equity curve
result.equity_curve.plot(title="Strategy Performance")

# Returns distribution
result.returns.hist(bins=50)

# Positions over time
result.positions_over_time.plot()

# Metrics by period
result.metrics_by_period["turnover"].plot(title="Turnover")
result.metrics_by_period["cost"].plot(title="Costs")
result.metrics_by_period["gross_exposure"].plot(title="Leverage")

# Trade log
trades_df = pd.DataFrame(result.trades)
print(trades_df.groupby("symbol")["cost"].sum())
```

## Configuration Options

`BacktestConfig` parameters:

```python
config = BacktestConfig(
    # Cost parameters
    fee_rate=0.0004,           # Transaction fee (0.04%)
    slippage_rate=0.0002,      # Slippage (0.02%)
    holding_cost_rate=0.00001, # Holding cost per period

    # Trading thresholds
    min_trade_notional=1e-6,   # Minimum trade size
    rebalance_threshold=0.0,   # Min weight change to rebalance

    # Leverage and risk
    leverage=1.0,              # Position leverage multiplier
    enable_liquidation=True,   # Check for liquidation
    maintenance_margin_rate=0.05,  # 5% maintenance margin
    liquidation_buffer=0.001,  # Extra buffer (0.1%)

    # Simulation settings
    initial_capital=1.0,       # Starting portfolio value
    periods_per_year=8760,     # CRITICAL: Must match your interval!
                               # 1min=525600, 5min=105120, 15min=35040
                               # 1h=8760, 4h=2190, 1d=365

    # Optional funding rates
    funding_rates=None         # DataFrame of funding rates
)
```

**CRITICAL**: `periods_per_year` must match your data interval or Sharpe/volatility will be wrong!

### Symbol-Specific Margins

```python
config = BacktestConfig(
    maintenance_margin_by_symbol={
        "BTC": 1 / 100.0,  # 1% margin (100x max leverage)
        "ETH": 1 / 50.0,   # 2% margin (50x max leverage)
        "HYPE": 1 / 20.0,  # 5% margin (20x max leverage)
    }
)
```

## Multi-Leverage Testing

Test strategy across multiple leverage levels:

```python
from wayfinder_paths.core.backtesting import run_multi_leverage_backtest

results = run_multi_leverage_backtest(
    prices=prices,
    target_positions=target_positions,
    leverage_tiers=(1.0, 2.0, 3.0, 5.0),
    base_config=config
)

# Compare results
for label, result in results.items():
    sharpe = result.stats['sharpe']
    max_dd = result.stats['max_drawdown']
    print(f"{label}: Sharpe={sharpe:.2f}, Max DD={max_dd:.2%}")
```

## Best Practices

1. **Start simple** - Test basic signal logic before adding complexity
2. **Check data quality** - Verify price data looks reasonable (no gaps, spikes)
3. **Use realistic costs** - Don't underestimate fees and slippage
4. **Test multiple periods** - Don't overfit to one time period
5. **Consider funding** - For perp strategies, funding can dominate P&L
6. **Watch liquidations** - If strategy liquidates, reduce leverage
7. **Validate assumptions** - Does the signal make economic sense?
8. **Compare to baseline** - How does it perform vs buy-and-hold?

## From Backtest to Production

Once a strategy shows promising backtest results:

1. **Create strategy class** using `just create-strategy "Strategy Name"`
2. **Implement Strategy interface** (deposit, update, withdraw, exit)
3. **Add proper adapters** for live execution
4. **Write smoke tests** in `test_strategy.py`
5. **Create manifest.yaml** with permissions and dependencies
6. **Document in examples.json** with expected performance
7. **Run on testnet** or with small capital first
8. **Monitor live performance** vs backtest expectations

## Common Gotchas

❌ **Look-ahead bias** - Using future information in signals
❌ **Survivorship bias** - Only testing on assets that still exist
❌ **Overfitting** - Too many parameters tuned to one dataset
❌ **Unrealistic costs** - Ignoring fees, slippage, funding
❌ **Position sizing** - Not accounting for leverage constraints
❌ **Data quality** - Missing bars, incorrect prices, timezone issues

## Examples

See `examples/` directory for full working examples:
- `basic_momentum.py` - Simple momentum strategy
- `basis_trading.py` - Spot-perp basis trading
- `mean_reversion.py` - Mean reversion strategy
- `carry_harvesting.py` - Funding rate carry strategy
