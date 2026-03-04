# Backtesting Framework

Simple, realistic backtesting framework for strategy development and validation.

## Features

- ✅ Automatic data fetching from Delta Lab and Hyperliquid
- ✅ Realistic transaction costs (fees, slippage, funding rates)
- ✅ Comprehensive performance metrics (Sharpe, Sortino, max drawdown, etc.)
- ✅ Liquidation simulation with symbol-specific maintenance margins
- ✅ Multi-leverage comparison testing
- ✅ Clean, minimal API

## Quick Start

```python
from wayfinder_paths.core.backtesting import quick_backtest

def my_strategy(prices, ctx):
    """Simple momentum strategy."""
    returns = prices.pct_change(24)
    ranks = returns.rank(axis=1, pct=True)
    target = (ranks > 0.5).astype(float) - (ranks < 0.5).astype(float)
    return target / target.abs().sum(axis=1).fillna(1)

result = await quick_backtest(
    strategy_fn=my_strategy,
    symbols=["BTC", "ETH"],
    start_date="2025-01-01",
    end_date="2025-02-01",
    leverage=2.0
)

print(result.stats)
# {'sharpe': 1.42, 'max_drawdown': -15.3, 'cagr': 45.2, ...}
```

## Manual Workflow

For full control over data fetching and configuration:

```python
from wayfinder_paths.core.backtesting import (
    fetch_prices,
    fetch_funding_rates,
    run_backtest,
    BacktestConfig,
)

# Fetch data
prices = await fetch_prices(["BTC", "ETH"], "2025-01-01", "2025-02-01")
funding = await fetch_funding_rates(["BTC", "ETH"], "2025-01-01", "2025-02-01")

# Generate signals (your strategy logic)
target_positions = my_strategy(prices, {})

# Configure
config = BacktestConfig(
    leverage=2.0,
    fee_rate=0.0004,
    funding_rates=funding,
    enable_liquidation=True
)

# Run
result = run_backtest(prices, target_positions, config)
```

## Data Fetchers

```python
# Price data
prices = await fetch_prices(
    symbols=["BTC", "ETH"],
    start_date="2025-01-01",
    end_date="2025-02-01",
    interval="1h"  # 1m, 5m, 15m, 1h, 4h, 1d
)

# Funding rates (for perps)
funding = await fetch_funding_rates(
    symbols=["BTC", "ETH"],
    start_date="2025-01-01",
    end_date="2025-02-01"
)

# Borrow rates (lending protocols)
rates = await fetch_borrow_rates(
    symbols=["USDC", "ETH"],
    start_date="2025-01-01",
    end_date="2025-02-01",
    protocol="aave"  # or "morpho", "moonwell"
)
```

## Signal Format

Your strategy function must return a **target positions DataFrame**:

- **Index**: timestamps (matching input prices)
- **Columns**: symbols (matching input prices)
- **Values**: weights in `[-1, 1]` range
  - `1.0` = 100% long
  - `-1.0` = 100% short
  - `0.0` = flat/no position

Weights are scaled by the `leverage` parameter.

## Key Metrics

```python
result.stats = {
    'sharpe': 1.42,          # Risk-adjusted returns (>1.0 good)
    'sortino': 1.68,         # Downside risk-adjusted
    'cagr': 45.2,            # Annualized return (%)
    'max_drawdown': -15.3,   # Largest decline (%)
    'win_rate': 54.2,        # % profitable periods
    'profit_factor': 1.85,   # Profit/loss ratio
    'trade_count': 142,      # Total trades
    'final_equity': 1.452    # Final portfolio value
}
```

## Configuration

```python
config = BacktestConfig(
    # Costs
    fee_rate=0.0004,           # 0.04% per trade
    slippage_rate=0.0002,      # 0.02% slippage

    # Risk
    leverage=2.0,              # Position leverage
    enable_liquidation=True,   # Check for liquidation
    maintenance_margin_rate=0.05,  # 5% default margin

    # Optional
    funding_rates=None,        # DataFrame of funding rates
    maintenance_margin_by_symbol={  # Symbol-specific margins
        "BTC": 1/100.0,  # 1% (100x max leverage)
        "ETH": 1/50.0,   # 2% (50x max leverage)
    }
)
```

## Multi-Leverage Testing

Compare performance across leverage levels:

```python
from wayfinder_paths.core.backtesting import run_multi_leverage_backtest

results = run_multi_leverage_backtest(
    prices=prices,
    target_positions=target_positions,
    leverage_tiers=(1.0, 2.0, 3.0, 5.0)
)

for label, result in results.items():
    print(f"{label}: Sharpe={result.stats['sharpe']:.2f}")
```

## Strategy Examples

See `.claude/skills/backtest-strategy/examples/` for working examples:

- `basic_momentum.py` - Cross-sectional momentum
- Basis trading, mean reversion, carry harvesting patterns in skill docs

## From Backtest to Production

1. Validate via backtesting (this module)
2. Create strategy: `just create-strategy "Strategy Name"`
3. Implement Strategy interface
4. Add adapters, tests, manifest
5. Deploy with small capital

## Claude Code Skill

Load `/backtest-strategy` skill for:
- Full documentation and patterns
- Common strategy examples
- Gotchas and best practices
- Integration with production strategies

## Implementation

Core modules:
- `backtester.py` - Main backtest engine (ported from production)
- `data.py` - Data fetchers (Delta Lab, Hyperliquid)
- `helpers.py` - Convenience wrappers (`quick_backtest`)
- `test_backtesting.py` - Tests

Design philosophy: **Simple, fast, realistic**. No complex abstractions, just clean functions that work.
