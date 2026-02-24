"""
Backtesting framework for strategy development and validation.

Provides simple interfaces for running backtests with realistic costs,
automatic data fetching, and comprehensive performance metrics.

Quick start:
    >>> from wayfinder_paths.core.backtesting import quick_backtest
    >>>
    >>> def my_strategy(prices, ctx):
    ...     returns = prices.pct_change()
    ...     signals = (returns > 0).astype(float)
    ...     return signals / signals.sum(axis=1).values[:, None]
    >>>
    >>> result = await quick_backtest(
    ...     strategy_fn=my_strategy,
    ...     symbols=["BTC", "ETH"],
    ...     start_date="2025-01-01",
    ...     end_date="2025-02-01",
    ...     leverage=2.0
    ... )
    >>> print(result.stats)

Manual workflow:
    >>> from wayfinder_paths.core.backtesting import (
    ...     fetch_prices,
    ...     fetch_funding_rates,
    ...     run_backtest,
    ...     BacktestConfig,
    ... )
    >>>
    >>> prices = await fetch_prices(["BTC", "ETH"], "2025-01-01", "2025-02-01")
    >>> funding = await fetch_funding_rates(["BTC", "ETH"], "2025-01-01", "2025-02-01")
    >>>
    >>> # Your signal logic
    >>> target_positions = ...
    >>>
    >>> config = BacktestConfig(leverage=2.0, funding_rates=funding)
    >>> result = run_backtest(prices, target_positions, config)
"""

from wayfinder_paths.core.backtesting.backtester import (
    BacktestConfig,
    BacktestResult,
    run_backtest,
    run_multi_leverage_backtest,
)
from wayfinder_paths.core.backtesting.data import (
    align_dataframes,
    convert_to_spot,
    fetch_borrow_rates,
    fetch_funding_rates,
    fetch_prices,
)
from wayfinder_paths.core.backtesting.helpers import (
    backtest_with_rates,
    quick_backtest,
)


__all__ = [
    # Core functions
    "run_backtest",
    "BacktestConfig",
    "BacktestResult",
    # Data fetching
    "fetch_prices",
    "fetch_funding_rates",
    "fetch_borrow_rates",
    # Delta neutral helpers  ← NEW SECTION
    "convert_to_spot",  # For spot/perp hedging strategies
    # Utilities
    "align_dataframes",
    "quick_backtest",
    "backtest_with_rates",
]
