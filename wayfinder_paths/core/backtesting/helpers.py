"""
Helper utilities for quick backtesting workflows.

Provides convenience wrappers that combine data fetching and backtesting.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

from wayfinder_paths.core.backtesting.backtester import run_backtest
from wayfinder_paths.core.backtesting.data import (
    align_dataframes,
    fetch_funding_rates,
    fetch_prices,
)
from wayfinder_paths.core.backtesting.types import BacktestConfig, BacktestResult


async def quick_backtest(
    strategy_fn: Callable[[pd.DataFrame, dict[str, Any]], pd.DataFrame],
    symbols: list[str],
    start_date: str,
    end_date: str,
    interval: str = "1h",
    leverage: float = 1.0,
    include_funding: bool = True,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """
    Run a backtest with automatic data fetching.

    This function automatically:
    - Fetches price data
    - Fetches funding rates (if include_funding=True)
    - Builds context dict with: symbols, interval, start_date, end_date
    - Calls your strategy_fn(prices, context)
    - Runs the backtest with proper periods_per_year for the interval

    Args:
        strategy_fn: Function that takes (prices, context) and returns target_positions.
                    The context dict is built automatically and contains:
                    {"symbols": [...], "interval": "1h", "start_date": "...", "end_date": "..."}
                    Your function cannot pass additional context keys - use closures if needed.
        symbols: List of symbols to trade (e.g., ["BTC", "ETH"])
        start_date: Start date (ISO format: "2025-01-01")
        end_date: End date (ISO format: "2025-02-01")
        interval: Time interval ("1m", "5m", "15m", "1h", "4h", "1d")
        leverage: Position leverage (e.g., 2.0 = 2x)
        include_funding: Whether to fetch and apply funding rates
        config: Optional BacktestConfig. If provided, leverage and funding_rates will be overridden.
                The periods_per_year will be set automatically based on interval.

    Returns:
        BacktestResult object with equity_curve, returns, stats, trades, etc.
        All stats are in decimal format (0-1 scale):
        - total_return of 0.45 = 45% return
        - max_drawdown of -0.25 = -25% decline

    Example:
        >>> def my_strategy(prices, ctx):
        ...     # ctx is automatically: {"symbols": ["BTC", "ETH"], "interval": "1h", ...}
        ...     # Simple momentum
        ...     returns = prices.pct_change()
        ...     signals = (returns > 0).astype(float)
        ...     return signals / signals.sum(axis=1).values[:, None]

        >>> result = await quick_backtest(
        ...     strategy_fn=my_strategy,
        ...     symbols=["BTC", "ETH"],
        ...     start_date="2025-01-01",
        ...     end_date="2025-02-01",
        ...     leverage=2.0
        ... )
        >>> print(f"Return: {result.stats['total_return']:.2%}")  # Format as percentage
    """
    prices = await fetch_prices(symbols, start_date, end_date, interval)

    funding = None
    if include_funding:
        try:
            funding = await fetch_funding_rates(symbols, start_date, end_date)
            prices, funding = await align_dataframes(prices, funding, method="ffill")
        except (ValueError, KeyError):
            pass

    context = {
        "symbols": symbols,
        "interval": interval,
        "start_date": start_date,
        "end_date": end_date,
    }
    target_positions = strategy_fn(prices, context)

    # Auto-calculate periods_per_year based on interval
    interval_to_periods = {
        "1m": 365 * 24 * 60,  # 525600
        "5m": 365 * 24 * 12,  # 105120
        "15m": 365 * 24 * 4,  # 35040
        "1h": 365 * 24,  # 8760
        "4h": 365 * 6,  # 2190
        "1d": 365,
    }
    periods_per_year = interval_to_periods.get(interval, 365 * 24 * 60)

    if config is None:
        config = BacktestConfig(
            leverage=leverage, funding_rates=funding, periods_per_year=periods_per_year
        )
    else:
        config.leverage = leverage
        config.funding_rates = funding
        config.periods_per_year = periods_per_year

    return run_backtest(prices, target_positions, config)


async def backtest_with_rates(
    strategy_fn: Callable[
        [pd.DataFrame, pd.DataFrame | None, dict[str, Any]], pd.DataFrame
    ],
    symbols: list[str],
    start_date: str,
    end_date: str,
    interval: str = "1h",
    leverage: float = 1.0,
    include_funding: bool = True,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """
    Run a backtest where strategy function receives both prices and funding rates.

    Args:
        strategy_fn: Function that takes (prices, funding_rates, context) and returns target_positions
        symbols: List of symbols to trade
        start_date: Start date (ISO format)
        end_date: End date (ISO format)
        interval: Time interval
        leverage: Position leverage
        include_funding: Whether to fetch funding rates
        config: Optional BacktestConfig

    Returns:
        BacktestResult object

    Example:
        >>> def basis_strategy(prices, funding, ctx):
        ...     # Use funding rates in signal generation
        ...     high_funding = funding > 0.01
        ...     signals = high_funding.astype(float)
        ...     return signals / signals.sum(axis=1).fillna(1)

        >>> result = await backtest_with_rates(
        ...     strategy_fn=basis_strategy,
        ...     symbols=["BTC", "ETH"],
        ...     start_date="2025-01-01",
        ...     end_date="2025-02-01"
        ... )
    """
    prices = await fetch_prices(symbols, start_date, end_date, interval)

    funding = None
    if include_funding:
        try:
            funding = await fetch_funding_rates(symbols, start_date, end_date)
            prices, funding = await align_dataframes(prices, funding, method="ffill")
        except (ValueError, KeyError):
            pass

    context = {
        "symbols": symbols,
        "interval": interval,
        "start_date": start_date,
        "end_date": end_date,
    }
    target_positions = strategy_fn(prices, funding, context)

    if config is None:
        config = BacktestConfig(leverage=leverage, funding_rates=funding)
    else:
        config.leverage = leverage
        config.funding_rates = funding

    return run_backtest(prices, target_positions, config)
