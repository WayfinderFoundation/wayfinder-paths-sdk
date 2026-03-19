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
    convert_to_spot,
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

    interval_to_periods = {
        "1m": 365 * 24 * 60,
        "5m": 365 * 24 * 12,
        "15m": 365 * 24 * 4,
        "1h": 365 * 24,
        "4h": 365 * 6,
        "1d": 365,
    }
    periods_per_year = interval_to_periods.get(interval, 365 * 24)

    if config is None:
        config = BacktestConfig(
            leverage=leverage, funding_rates=funding, periods_per_year=periods_per_year
        )
    else:
        config.leverage = leverage
        config.funding_rates = funding
        config.periods_per_year = periods_per_year

    return run_backtest(prices, target_positions, config)


async def backtest_delta_neutral(
    symbols: list[str],
    start_date: str,
    end_date: str,
    funding_threshold: float = 0.0001,
    leverage: float = 1.0,
    interval: str = "1h",
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """
    Delta-neutral basis carry: long spot + short perp, enter when funding is positive.

    Strategy logic:
    - Short perp (-0.5 weight) + long spot (+0.5 weight) per symbol when funding > threshold
    - Net delta ≈ 0 (price-neutral); profits from funding payments collected on the short side
    - Exits when funding drops below threshold (flat)

    Funding sign convention (CRITICAL):
    - Positive funding (+): longs PAY shorts → short perp RECEIVES funding (good)
    - Negative funding (-): shorts PAY longs → short perp PAYS funding (bad)
    - Only enter when funding > threshold to ensure you collect, not pay

    Args:
        symbols: Perp symbols to trade (e.g. ["BTC", "ETH"])
        start_date: Start date ("YYYY-MM-DD", oldest ~Aug 2025)
        end_date: End date ("YYYY-MM-DD")
        funding_threshold: Per-period funding rate threshold to enter.
                           Raw per-period value from fetch_funding_rates()
                           (e.g. 0.0001 = 0.01% per hour ≈ 8.76% annualized for 1h data).
                           Set to 0.0 to always hold delta-neutral regardless of sign.
        leverage: Capital leverage multiplier (default 1.0)
        interval: Price/funding data interval (default "1h")
        config: Optional BacktestConfig; leverage and funding_rates are always overridden.

    Returns:
        BacktestResult. Key stats:
        - total_funding: cumulative funding paid (negative = income received / profit)
        - volatility_ann: should be very low (<5%) for a well-constructed delta-neutral
        - sharpe: often very high (10-30+) for funding harvesting strategies

    Notes:
        - Positions use "{symbol}_PERP" and "{symbol}_SPOT" column naming
        - If funding data is unavailable for a symbol, that symbol gets static delta-neutral
        - enable_liquidation defaults to False

    Example:
        >>> result = await backtest_delta_neutral(
        ...     ["BTC", "ETH"], "2025-08-01", "2026-01-01",
        ...     funding_threshold=0.0001, leverage=1.5
        ... )
        >>> print(f"Funding income: {result.stats['total_funding']:.4f}")
        >>> print(f"Sharpe: {result.stats['sharpe']:.2f}")
    """
    perp_prices = await fetch_prices(symbols, start_date, end_date, interval)

    perp_funding: pd.DataFrame | None = None
    try:
        perp_funding = await fetch_funding_rates(symbols, start_date, end_date)
        perp_prices, perp_funding = await align_dataframes(
            perp_prices, perp_funding, method="ffill"
        )
    except (ValueError, KeyError):
        pass

    spot_prices, spot_funding = convert_to_spot(perp_prices)

    all_prices = pd.concat(
        [perp_prices.add_suffix("_PERP"), spot_prices.add_suffix("_SPOT")], axis=1
    )

    if perp_funding is not None:
        perp_funding_suffixed = perp_funding.add_suffix("_PERP")
    else:
        perp_funding_suffixed = pd.DataFrame(
            0.0,
            index=perp_prices.index,
            columns=[f"{s}_PERP" for s in symbols],
        )
    all_funding = pd.concat(
        [perp_funding_suffixed, spot_funding.add_suffix("_SPOT")], axis=1
    )

    target = pd.DataFrame(0.0, index=all_prices.index, columns=all_prices.columns)
    for sym in symbols:
        perp_col = f"{sym}_PERP"
        spot_col = f"{sym}_SPOT"
        if perp_funding is not None and sym in perp_funding.columns:
            high_funding = perp_funding[sym] > funding_threshold
            target.loc[high_funding, perp_col] = -0.5
            target.loc[high_funding, spot_col] = 0.5
        else:
            # No funding data: always hold delta-neutral
            target[perp_col] = -0.5
            target[spot_col] = 0.5

    interval_to_periods = {
        "1m": 365 * 24 * 60,
        "5m": 365 * 24 * 12,
        "15m": 365 * 24 * 4,
        "1h": 365 * 24,
        "4h": 365 * 6,
        "1d": 365,
    }
    periods_per_year = interval_to_periods.get(interval, 365 * 24)

    if config is None:
        config = BacktestConfig(
            leverage=leverage,
            funding_rates=all_funding,
            periods_per_year=periods_per_year,
            enable_liquidation=False,
        )
    else:
        config.leverage = leverage
        config.funding_rates = all_funding
        config.periods_per_year = periods_per_year

    return run_backtest(all_prices, target, config)
