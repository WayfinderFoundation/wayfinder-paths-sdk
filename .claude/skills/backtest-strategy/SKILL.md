---
name: backtest-strategy
description: Guide agents through backtesting strategy ideas with automatic data fetching and performance analysis
metadata:
  tags: backtesting, strategy, performance, sharpe, drawdown, funding, simulation
---

## When to use

Use this skill when you are:
- Validating a trading strategy idea before production deployment
- Analyzing historical performance (Sharpe, drawdown, CAGR, funding PnL)
- Testing different leverage levels or parameter combinations
- Building a carry/momentum/delta-neutral strategy backtest

## How to use

- [rules/backtesting.md](rules/backtesting.md) - Quick start, data availability, strategy function spec, BacktestConfig, gotchas, and key metrics

## Examples

- [examples/basic_momentum.py](examples/basic_momentum.py) - Cross-sectional momentum strategy using `quick_backtest`
