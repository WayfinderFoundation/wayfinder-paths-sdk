"""
Clean, standalone backtesting module for portfolio strategies.

This module provides simple backtesting functionality with realistic transaction costs,
proper position tracking, and comprehensive performance metrics.

Basic usage:
    >>> from wayfinder_paths.core.backtesting import run_backtest, BacktestConfig
    >>> config = BacktestConfig(leverage=2.0, fee_rate=0.0004)
    >>> result = run_backtest(prices_df, target_positions_df, config)
    >>> print(result.stats)
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.core.backtesting.stats import calculate_stats
from wayfinder_paths.core.backtesting.types import (
    BacktestConfig,
    BacktestResult,
)
from wayfinder_paths.core.backtesting.utils import (
    get_maintenance_margin_rate,
    validate_target_positions,
)


def run_backtest(
    prices: pd.DataFrame,
    target_positions: pd.DataFrame,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """
    Run a backtest simulation with realistic execution and costs.

    Args:
        prices: DataFrame with index=timestamps, columns=symbols, values=prices
        target_positions: DataFrame with index=timestamps, columns=symbols, values=weights
                         Weights should be in [-1, 1] range (can be leveraged via config)
        config: BacktestConfig object (uses defaults if None)

    Returns:
        BacktestResult object containing equity curve, metrics, trades, etc.

    Example:
        >>> prices = pd.DataFrame({
        ...     'SYMBOL_A': [100, 101, 102, 103],
        ...     'SYMBOL_B': [50, 51, 50, 52]
        ... }, index=pd.date_range('2024-01-01', periods=4, freq='1H'))
        >>>
        >>> target_positions = pd.DataFrame({
        ...     'SYMBOL_A': [0.5, 0.5, 0.5, 0.5],
        ...     'SYMBOL_B': [0.5, 0.5, 0.5, 0.5]
        ... }, index=prices.index)
        >>>
        >>> result = run_backtest(prices, target_positions)
        >>> print(f"Sharpe: {result.stats['sharpe']:.2f}")
    """
    if config is None:
        config = BacktestConfig()

    if prices.empty or target_positions.empty:
        raise ValueError("Prices and target_positions DataFrames cannot be empty")

    if not prices.index.equals(target_positions.index):
        raise ValueError("Prices and target_positions must have the same index")

    symbols = list(prices.columns)
    if not all(sym in target_positions.columns for sym in symbols):
        raise ValueError("target_positions must have all symbols from prices")

    # Validate target_positions and warn about common issues
    validation_warnings = validate_target_positions(target_positions, prices)
    for warning in validation_warnings:
        print(warning)  # Print to stderr/stdout so user sees them immediately

    timestamps = prices.index

    # Auto-detect periods_per_year if not provided
    if config.periods_per_year is None:
        if len(timestamps) < 2:
            raise ValueError(
                "Cannot auto-detect periods_per_year with less than 2 data points. "
                "Please specify periods_per_year in config."
            )
        # Calculate average time difference between bars
        time_diffs = pd.Series(timestamps).diff().dropna()
        avg_bar_interval = time_diffs.median()  # Use median to handle irregular data
        seconds_per_bar = avg_bar_interval.total_seconds()

        if seconds_per_bar <= 0:
            raise ValueError(
                f"Invalid bar interval detected: {seconds_per_bar} seconds. "
                "Please specify periods_per_year in config."
            )

        # Calculate periods per year (365.25 days for leap years)
        seconds_per_year = 365.25 * 24 * 60 * 60
        config.periods_per_year = int(seconds_per_year / seconds_per_bar)

    prices = prices[symbols].ffill()
    target_positions = target_positions[symbols].ffill().fillna(0.0).clip(-1.0, 1.0)

    # Align funding rates with prices safely (no lookahead bias)
    if config.funding_rates is not None:
        # Join funding rates with prices, forward fill, then slice out just funding
        combined = prices.join(config.funding_rates, rsuffix="_funding")
        funding_cols = [col for col in combined.columns if col.endswith("_funding")]
        funding_aligned = combined[funding_cols].ffill()
        # Remove the '_funding' suffix to restore original column names
        funding_aligned.columns = [
            col.replace("_funding", "") for col in funding_aligned.columns
        ]
        config.funding_rates = funding_aligned

    cash_balance = config.initial_capital
    position_units = pd.Series(0.0, index=symbols, dtype=float)

    portfolio_values: list[float] = []
    position_snapshots: list[dict[str, float]] = []
    trades: list[dict[str, Any]] = []
    turnover_series: list[float] = []
    cost_series: list[float] = []
    exposure_series: list[float] = []
    fee_series: list[float] = []
    funding_series: list[float] = []

    liquidated = False
    liquidation_timestamp: pd.Timestamp | None = None

    for idx, ts in enumerate(timestamps):
        current_prices = prices.loc[ts]
        target_weights = target_positions.loc[ts]

        inventory_value = float((position_units * current_prices).sum())
        portfolio_value = cash_balance + inventory_value
        nav_before_trade = portfolio_value

        total_turnover = 0.0
        total_cost = 0.0
        period_fees = 0.0
        period_funding = 0.0

        for sym in symbols:
            price = float(current_prices[sym])
            if not price or price <= 0 or portfolio_value <= 0:
                continue

            target_weight = float(target_weights[sym])
            target_notional = target_weight * config.leverage * nav_before_trade
            target_units = target_notional / price

            current_units = float(position_units[sym])
            trade_units = target_units - current_units
            trade_notional = abs(trade_units * price)

            if trade_notional < config.min_trade_notional:
                continue

            current_weight = (
                (current_units * price) / nav_before_trade
                if nav_before_trade > 0
                else 0
            )
            weight_change = abs(target_weight * config.leverage - current_weight)
            if weight_change < config.rebalance_threshold:
                continue

            transaction_cost = trade_notional * (config.fee_rate + config.slippage_rate)

            if transaction_cost + trade_units * price > cash_balance:
                continue

            cash_balance -= trade_units * price
            cash_balance -= transaction_cost
            position_units[sym] = target_units

            total_turnover += trade_notional
            total_cost += transaction_cost
            period_fees += transaction_cost

            trades.append(
                {
                    "timestamp": ts,
                    "symbol": sym,
                    "price": price,
                    "units": trade_units,
                    "notional": trade_units * price,
                    "target_weight": target_weight,
                    "cost": transaction_cost,
                    "leverage": config.leverage,
                }
            )

        # Apply funding rates (now guaranteed to have matching timestamps)
        if config.funding_rates is not None:
            funding_row = config.funding_rates.loc[ts]
            funding_charge = 0.0
            for sym in symbols:
                if sym in funding_row:
                    funding_charge += (
                        float(position_units[sym])
                        * float(current_prices[sym])
                        * float(funding_row[sym])
                    )
            cash_balance -= funding_charge
            total_cost += funding_charge
            period_funding += funding_charge

        gross_notional = sum(
            abs(float(position_units[sym]) * float(current_prices[sym]))
            for sym in symbols
        )

        if config.enable_liquidation and portfolio_value > 0:
            maintenance_requirement = 0.0
            for sym in symbols:
                price = float(current_prices[sym])
                if price <= 0:
                    continue
                position_notional = abs(float(position_units[sym]) * price)
                margin_rate = get_maintenance_margin_rate(sym, config)
                maintenance_requirement += position_notional * margin_rate

            if (
                maintenance_requirement > 0
                and portfolio_value
                < maintenance_requirement * (1 + config.liquidation_buffer)
            ):
                liquidated = True
                liquidation_timestamp = ts
                cash_balance = 0.0
                position_units[:] = 0.0
                portfolio_value = 0.0

                remaining = len(timestamps) - idx - 1
                portfolio_values.append(0.0)
                turnover_series.append(0.0)
                cost_series.append(0.0)
                exposure_series.append(0.0)
                fee_series.append(0.0)
                funding_series.append(0.0)
                position_snapshots.append(dict.fromkeys(symbols, 0.0))
                if remaining > 0:
                    portfolio_values.extend([0.0] * remaining)
                    turnover_series.extend([0.0] * remaining)
                    cost_series.extend([0.0] * remaining)
                    exposure_series.extend([0.0] * remaining)
                    fee_series.extend([0.0] * remaining)
                    funding_series.extend([0.0] * remaining)
                    position_snapshots.extend([dict.fromkeys(symbols, 0.0)] * remaining)
                break

        portfolio_values.append(portfolio_value)
        turnover_series.append(
            total_turnover / nav_before_trade if nav_before_trade > 0 else 0.0
        )
        cost_series.append(
            total_cost / nav_before_trade if nav_before_trade > 0 else 0.0
        )
        exposure_series.append(
            gross_notional / portfolio_value if portfolio_value > 0 else 0.0
        )
        fee_series.append(period_fees)
        funding_series.append(period_funding)
        position_snapshots.append({sym: float(position_units[sym]) for sym in symbols})

    equity_curve = pd.Series(portfolio_values[: len(timestamps)], index=timestamps)
    returns = equity_curve.pct_change().replace([np.inf, -np.inf], 0.0).fillna(0.0)

    metrics_by_period = pd.DataFrame(
        {
            "equity": portfolio_values[: len(timestamps)],
            "turnover": turnover_series[: len(timestamps)],
            "cost": cost_series[: len(timestamps)],
            "gross_exposure": exposure_series[: len(timestamps)],
        },
        index=timestamps,
    )

    positions_over_time = pd.DataFrame(
        position_snapshots[: len(timestamps)], index=timestamps
    )

    stats = calculate_stats(
        returns=returns,
        equity_curve=equity_curve,
        trades=trades,
        turnover_series=turnover_series,
        cost_series=cost_series,
        fee_series=fee_series,
        funding_series=funding_series,
        periods_per_year=config.periods_per_year,
        prices=prices,
    )

    return BacktestResult(
        equity_curve=equity_curve,
        returns=returns,
        stats=stats,
        trades=trades,
        metrics_by_period=metrics_by_period,
        positions_over_time=positions_over_time,
        liquidated=liquidated,
        liquidation_timestamp=liquidation_timestamp,
    )
