"""
Clean, standalone backtesting module for portfolio strategies.

This module provides simple backtesting functionality with realistic transaction costs,
proper position tracking, and comprehensive performance metrics.

Basic usage:
    >>> from wayfinder_paths.core.backtesting.backtester import run_backtest
    >>> from wayfinder_paths.core.backtesting.types import BacktestConfig
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


def get_atomic_trade_scale(
    *,
    current_prices: np.ndarray,
    position_units: np.ndarray,
    weights: np.ndarray,
    nav_before_trade: float,
    free_cash: float,
    force_rebalance: bool,
    config: BacktestConfig,
) -> float:
    # Margin-based cost: both longs and shorts require margin, not just longs.
    # Margin per trade = notional / leverage.
    margin_cost_at1 = fee_cost_at1 = 0.0
    for j in range(len(current_prices)):
        price = float(current_prices[j])
        if not price or price <= 0 or nav_before_trade <= 0:
            continue
        target_weight = float(weights[j])
        target_units = target_weight * config.leverage * nav_before_trade / price
        current_units = float(position_units[j])
        trade_units = target_units - current_units
        trade_notional = abs(trade_units * price)
        if trade_notional < config.min_trade_notional:
            continue
        current_weight = (current_units * price) / nav_before_trade
        weight_change = abs(target_weight * config.leverage - current_weight)
        reducing_gross = abs(target_units * price) < abs(current_units * price) - 1e-12
        if weight_change < config.rebalance_threshold and not (
            force_rebalance and reducing_gross
        ):
            continue
        # Only count margin for the net increase in gross exposure (not the full trade).
        # A flip from +1 to -1 has zero net gross change and should cost zero margin.
        new_gross = abs(target_units * price)
        old_gross = abs(current_units * price)
        gross_increase = max(0.0, new_gross - old_gross)
        if gross_increase > 0:
            margin_cost_at1 += (
                gross_increase / config.leverage
                if config.leverage > 0
                else gross_increase
            )
        fee_cost_at1 += trade_notional * (config.fee_rate + config.slippage_rate)
    total_required = margin_cost_at1 + fee_cost_at1
    return max(
        0.0, min(1.0, free_cash / total_required) if total_required > 1e-12 else 1.0
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
    if config.validate_positions:
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

    # "replay" skips the shift — reconciliation only (see BacktestConfig).
    if config.fill_model == "next_bar_open":
        target_positions = target_positions.shift(1).fillna(0.0)
    elif config.fill_model != "replay":
        raise ValueError(
            f"Unknown fill_model={config.fill_model!r}; expected 'next_bar_open' or 'replay'"
        )

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
    n_bars = len(timestamps)
    symbol_count = len(symbols)
    position_units = np.zeros(symbol_count, dtype=float)

    # Operate on numpy arrays in the hot loop — pandas .loc[ts] row lookups and
    # Series[label] scalar access dominate per-bar cost (10-200x slower than
    # integer-indexed numpy). Columns are already ordered as `symbols`.
    price_mat = prices.values
    weight_mat = target_positions.values
    funding_mat = (
        config.funding_rates.reindex(columns=symbols).fillna(0.0).values
        if config.funding_rates is not None
        else None
    )
    maint_rates = np.array(
        [get_maintenance_margin_rate(sym, config) for sym in symbols], dtype=float
    )
    fee_plus_slip = config.fee_rate + config.slippage_rate
    leverage = config.leverage
    track_positions = config.track_positions

    portfolio_values: list[float] = []
    position_snapshots: list[np.ndarray] = []
    trades: list[dict[str, Any]] = []
    turnover_series: list[float] = []
    cost_series: list[float] = []
    exposure_series: list[float] = []
    fee_series: list[float] = []
    funding_series: list[float] = []

    liquidated = False
    liquidation_timestamp: pd.Timestamp | None = None

    for idx in range(n_bars):
        ts = timestamps[idx]
        current_prices = price_mat[idx]
        target_weights = weight_mat[idx]

        inventory_value = float(np.nansum(position_units * current_prices))
        portfolio_value = cash_balance + inventory_value
        nav_before_trade = portfolio_value

        total_turnover = 0.0
        total_cost = 0.0
        period_fees = 0.0
        period_funding = 0.0

        # Normalize weights if gross exposure > 1 to avoid unintended over-leverage
        gross_weight = float(np.nansum(np.abs(target_weights)))
        weights = (
            target_weights / gross_weight if gross_weight > 1.0 else target_weights
        )

        # Current gross notional drives both the force-rebalance check and the
        # margin-in-use calc (these were two identical computations before).
        current_gross_notional = float(np.nansum(np.abs(position_units * current_prices)))

        # Force-rebalance: allow reducing-gross trades to bypass rebalance_threshold
        # when current leverage exceeds config.leverage due to adverse price moves
        force_rebalance = (
            config.force_rebalance_if_overleveraged
            and portfolio_value > 0
            and current_gross_notional / leverage > nav_before_trade + 1e-12
        )

        # Margin-based free cash: for perp strategies, shorts don't consume
        # cash as loans — they require margin.  Available cash for new trades
        # is portfolio value minus margin reserved for current positions.
        margin_in_use = (
            current_gross_notional / leverage
            if leverage > 0
            else current_gross_notional
        )
        free_cash = max(0.0, portfolio_value - margin_in_use)
        scale = get_atomic_trade_scale(
            current_prices=current_prices,
            position_units=position_units,
            weights=weights,
            nav_before_trade=nav_before_trade,
            free_cash=free_cash,
            force_rebalance=force_rebalance,
            config=config,
        )

        for j in range(symbol_count):
            price = float(current_prices[j])
            if not price or price <= 0 or portfolio_value <= 0:
                continue

            target_weight = float(weights[j])
            target_notional = target_weight * leverage * nav_before_trade
            target_units = target_notional / price

            current_units = float(position_units[j])
            scaled_target = current_units + scale * (target_units - current_units)
            trade_units = scaled_target - current_units
            trade_notional = abs(trade_units * price)

            if trade_notional < config.min_trade_notional:
                continue

            current_weight = (
                (current_units * price) / nav_before_trade
                if nav_before_trade > 0
                else 0.0
            )
            weight_change = abs(target_weight * leverage - current_weight)
            reducing_gross = abs(target_notional) < abs(current_units * price) - 1e-12
            if weight_change < config.rebalance_threshold and not (
                force_rebalance and reducing_gross
            ):
                continue

            transaction_cost = trade_notional * fee_plus_slip

            cash_balance -= trade_units * price
            cash_balance -= transaction_cost
            position_units[j] = scaled_target

            total_turnover += trade_notional
            total_cost += transaction_cost
            period_fees += transaction_cost

            trades.append(
                {
                    "timestamp": ts,
                    "symbol": symbols[j],
                    "price": price,
                    "units": trade_units,
                    "notional": trade_units * price,
                    "target_weight": target_weight,
                    "cost": transaction_cost,
                    "leverage": leverage,
                }
            )

        # Apply funding rates (funding_mat is column-aligned to `symbols`, missing
        # symbols filled with 0 — equivalent to the old per-symbol membership check)
        if funding_mat is not None:
            funding_charge = float(
                np.sum(position_units * current_prices * funding_mat[idx])
            )
            cash_balance -= funding_charge
            total_cost += funding_charge
            period_funding += funding_charge

        gross_notional = float(np.sum(np.abs(position_units * current_prices)))
        portfolio_value = cash_balance + float(np.nansum(position_units * current_prices))

        if config.enable_liquidation and portfolio_value > 0:
            price_ok = current_prices > 0
            position_notional = np.abs(position_units * current_prices)
            maintenance_requirement = float(
                np.sum(np.where(price_ok, position_notional * maint_rates, 0.0))
            )

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

                remaining = n_bars - idx - 1
                portfolio_values.append(0.0)
                turnover_series.append(0.0)
                cost_series.append(0.0)
                exposure_series.append(0.0)
                fee_series.append(0.0)
                funding_series.append(0.0)
                if track_positions:
                    position_snapshots.append(np.zeros(symbol_count))
                if remaining > 0:
                    portfolio_values.extend([0.0] * remaining)
                    turnover_series.extend([0.0] * remaining)
                    cost_series.extend([0.0] * remaining)
                    exposure_series.extend([0.0] * remaining)
                    fee_series.extend([0.0] * remaining)
                    funding_series.extend([0.0] * remaining)
                    if track_positions:
                        position_snapshots.extend(
                            [np.zeros(symbol_count)] * remaining
                        )
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
        if track_positions:
            position_snapshots.append(position_units.copy())

    equity_curve = pd.Series(portfolio_values[:n_bars], index=timestamps)
    returns = equity_curve.pct_change().replace([np.inf, -np.inf], 0.0).fillna(0.0)

    metrics_by_period = pd.DataFrame(
        {
            "equity": portfolio_values[:n_bars],
            "turnover": turnover_series[:n_bars],
            "cost": cost_series[:n_bars],
            "gross_exposure": exposure_series[:n_bars],
        },
        index=timestamps,
    )

    if track_positions:
        positions_over_time = pd.DataFrame(
            np.array(position_snapshots[:n_bars]),
            index=timestamps,
            columns=symbols,
        )
    else:
        positions_over_time = pd.DataFrame(columns=symbols)

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
