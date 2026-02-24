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

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

DEFAULT_MAINTENANCE_MARGINS = {
    "HYPE/USDC:USDC": 1 / 20.0,
    "ASTER/USDC:USDC": 1 / 16.0,
    "DYDX/USDC:USDC": 1 / 20.0,
    "GMX/USDC:USDC": 1 / 20.0,
    "APEX/USDC:USDC": 1 / 20.0,
    "AVNT/USDC:USDC": 1 / 10.0,
    "BTC/USDC:USDC": 1 / 100.0,
}


@dataclass
class BacktestConfig:
    """
    Configuration for backtest simulation parameters.

    Args:
        fee_rate: Trading fee rate per trade (e.g., 0.0004 = 4 bps)
        slippage_rate: Slippage rate per trade (e.g., 0.0002 = 2 bps)
        holding_cost_rate: Holding cost rate per period
        min_trade_notional: Minimum trade size threshold
        rebalance_threshold: Minimum position change to trigger rebalance
        leverage: Position leverage multiplier (e.g., 2.0 = 2x)
        enable_liquidation: Enable liquidation simulation
        maintenance_margin_rate: Default maintenance margin requirement
        maintenance_margin_by_symbol: Per-symbol maintenance margin overrides
        liquidation_buffer: Extra buffer before liquidation triggers
        initial_capital: Starting capital (default 1.0)
        periods_per_year: Number of periods in one year - CRITICAL for Sharpe/volatility
            - 1-minute bars: 525600 (365 * 24 * 60)
            - 5-minute bars: 105120 (365 * 24 * 12)
            - 15-minute bars: 35040 (365 * 24 * 4)
            - 1-hour bars: 8760 (365 * 24)
            - 4-hour bars: 2190 (365 * 6)
            - Daily bars: 365
        funding_rates: DataFrame of funding rates (index=timestamps, cols=symbols)
    """

    fee_rate: float = 0.0004
    slippage_rate: float = 0.0002
    holding_cost_rate: float = 0.00001
    min_trade_notional: float = 1e-6
    rebalance_threshold: float = 0.0
    leverage: float = 1.0
    enable_liquidation: bool = True
    maintenance_margin_rate: float = 0.05
    maintenance_margin_by_symbol: dict[str, float] | None = None
    liquidation_buffer: float = 0.001
    initial_capital: float = 1.0
    periods_per_year: int = 525600
    funding_rates: pd.DataFrame | None = None


@dataclass
class BacktestResult:
    """
    Results from a backtest simulation.

    Attributes:
        equity_curve: Portfolio value over time (pd.Series, index=timestamps)
        returns: Period-over-period returns (pd.Series, index=timestamps)
        stats: Performance statistics dict (see schema below)
        trades: List of trade events with timestamps, symbols, costs
        metrics_by_period: DataFrame with equity, turnover, cost, exposure per period
        positions_over_time: DataFrame of position sizes per symbol over time
        liquidated: Whether the strategy was liquidated
        liquidation_timestamp: Timestamp of liquidation (if occurred)

    Stats Schema (all values in decimal format, 0-1 scale unless noted):
        sharpe: float - Sharpe ratio (>1.0 good, >2.0 excellent)
        sortino: float - Sortino ratio (like Sharpe, downside-only)
        cagr: float - Annualized return (0.12 = 12%)
        total_return: float - Cumulative return (0.45 = 45%)
        max_drawdown: float - Peak-to-trough decline (-0.25 = -25%)
        win_rate: float - Fraction of winning periods (0.55 = 55%)
        profit_factor: float | None - Gross profit / gross loss (>1.5 good)
        volatility: float - Annualized volatility (0.15 = 15%)
        avg_turnover: float - Average portfolio turnover per period
        avg_cost: float - Average transaction cost per period
        trade_count: int - Number of rebalance events
        final_equity: float - Ending portfolio value

    Example:
        >>> result = run_backtest(prices, positions, config)
        >>> print(f"Return: {result.stats['total_return']:.2%}")  # "45.20%"
        >>> print(f"Sharpe: {result.stats['sharpe']:.2f}")  # "3.31"
        >>> print(f"Max DD: {result.stats['max_drawdown']:.2%}")  # "-25.30%"
    """

    equity_curve: pd.Series
    returns: pd.Series
    stats: dict[str, float]
    trades: list[dict[str, Any]]
    metrics_by_period: pd.DataFrame
    positions_over_time: pd.DataFrame
    liquidated: bool = False
    liquidation_timestamp: pd.Timestamp | None = None


def _get_maintenance_margin_rate(symbol: str, config: BacktestConfig) -> float:
    """Get maintenance margin rate for a symbol."""
    if config.maintenance_margin_by_symbol is None:
        return DEFAULT_MAINTENANCE_MARGINS.get(symbol, config.maintenance_margin_rate)
    return config.maintenance_margin_by_symbol.get(
        symbol, config.maintenance_margin_rate
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

    timestamps = prices.index

    prices = prices[symbols].ffill()
    target_positions = target_positions[symbols].ffill().fillna(0.0).clip(-1.0, 1.0)

    # Align funding rates with prices safely (no lookahead bias)
    if config.funding_rates is not None:
        # Join funding rates with prices, forward fill, then slice out just funding
        combined = prices.join(config.funding_rates, rsuffix='_funding')
        funding_cols = [col for col in combined.columns if col.endswith('_funding')]
        funding_aligned = combined[funding_cols].ffill()
        # Remove the '_funding' suffix to restore original column names
        funding_aligned.columns = [col.replace('_funding', '') for col in funding_aligned.columns]
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
                margin_rate = _get_maintenance_margin_rate(sym, config)
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
                if remaining > 0:
                    portfolio_values.extend([0.0] * remaining)
                    turnover_series.extend([0.0] * remaining)
                    cost_series.extend([0.0] * remaining)
                    exposure_series.extend([0.0] * remaining)
                    fee_series.extend([0.0] * remaining)
                    funding_series.extend([0.0] * remaining)
                    position_snapshots.extend(
                        [dict.fromkeys(symbols, 0.0)] * (remaining + 1)
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

    stats = _calculate_stats(
        returns=returns,
        equity_curve=equity_curve,
        trades=trades,
        turnover_series=turnover_series,
        cost_series=cost_series,
        fee_series=fee_series,
        funding_series=funding_series,
        periods_per_year=config.periods_per_year,
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


def _calculate_stats(
    returns: pd.Series,
    equity_curve: pd.Series,
    trades: list[dict[str, Any]],
    turnover_series: list[float],
    cost_series: list[float],
    fee_series: list[float],
    funding_series: list[float],
    periods_per_year: int,
) -> dict[str, float | None]:
    """
    Calculate comprehensive performance statistics.

    Returns:
        Dict with performance metrics. All rates/returns in decimal format (0-1 scale):
        - total_return: 0.45 = 45% return
        - cagr: 0.12 = 12% annualized return
        - max_drawdown: -0.25 = -25% peak-to-trough decline
        - win_rate: 0.55 = 55% of periods had positive returns
        - volatility: 0.15 = 15% annualized volatility
        - sharpe: Raw ratio (not scaled)
        - sortino: Raw ratio (not scaled)
        - profit_factor: Gross profit / gross loss
        - avg_turnover: Average portfolio turnover per period
        - avg_cost: Average transaction cost per period
        - trade_count: Number of rebalance events
        - final_equity: Ending portfolio value
        - total_fees: Total transaction fees paid (absolute value)
        - total_funding: Total funding costs/income (negative = income received)
    """
    if len(returns) == 0 or len(equity_curve) == 0:
        return _empty_stats()

    mean_return = returns.mean()
    volatility = returns.std(ddof=0)

    sharpe = (
        float(np.sqrt(periods_per_year) * mean_return / volatility)
        if volatility > 0
        else 0.0
    )

    downside_returns = returns[returns < 0]
    downside_vol = downside_returns.std(ddof=0)
    sortino = (
        float(np.sqrt(periods_per_year) * mean_return / downside_vol)
        if downside_vol > 0
        else 0.0
    )

    years = len(returns) / periods_per_year if periods_per_year > 0 else 0
    cagr = (
        float(equity_curve.iloc[-1] ** (1 / years) - 1)
        if years > 0 and len(equity_curve) > 0
        else 0.0
    )

    cummax = equity_curve.cummax()
    drawdowns = equity_curve / cummax - 1
    max_drawdown = float(drawdowns.min()) if not drawdowns.empty else 0.0

    win_rate = float((returns > 0).mean()) if len(returns) > 0 else 0.0

    positive_returns = returns[returns > 0].sum()
    negative_returns = returns[returns < 0].sum()
    profit_factor = (
        float(positive_returns / abs(negative_returns))
        if negative_returns < 0
        else None
    )

    avg_turnover = float(np.mean(turnover_series)) if turnover_series else 0.0
    avg_cost = float(np.mean(cost_series)) if cost_series else 0.0

    # Calculate total fees and funding (absolute values, not averaged)
    total_fees = float(sum(fee_series)) if fee_series else 0.0
    total_funding = float(sum(funding_series)) if funding_series else 0.0

    trade_count = len(trades)

    return {
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "cagr": round(cagr, 4),
        "max_drawdown": round(max_drawdown, 4),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "volatility": round(volatility * np.sqrt(periods_per_year), 4),
        "avg_turnover": round(avg_turnover, 4),
        "avg_cost": round(avg_cost, 6),
        "total_return": round(equity_curve.iloc[-1] - 1.0, 4),
        "trade_count": trade_count,
        "final_equity": round(float(equity_curve.iloc[-1]), 4),
        "total_fees": round(total_fees, 4),
        "total_funding": round(total_funding, 4),
    }


def _empty_stats() -> dict[str, float | None]:
    """
    Return empty statistics dict for edge cases.

    All rate/return values are in decimal format (0-1 scale):
    - 0.10 = 10% return
    - -0.05 = -5% drawdown
    - 0.55 = 55% win rate
    """
    return {
        "sharpe": 0.0,
        "sortino": 0.0,
        "cagr": 0.0,
        "max_drawdown": 0.0,
        "win_rate": 0.0,
        "profit_factor": None,
        "volatility": 0.0,
        "avg_turnover": 0.0,
        "avg_cost": 0.0,
        "total_return": 0.0,
        "trade_count": 0,
        "final_equity": 1.0,
        "total_fees": 0.0,
        "total_funding": 0.0,
    }


def run_multi_leverage_backtest(
    prices: pd.DataFrame,
    target_positions: pd.DataFrame,
    leverage_tiers: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0),
    base_config: BacktestConfig | None = None,
) -> dict[str, BacktestResult]:
    """
    Run backtest across multiple leverage levels for comparison.

    Args:
        prices: Price DataFrame
        target_positions: Target position weights DataFrame
        leverage_tiers: Tuple of leverage levels to test
        base_config: Base configuration (leverage will be overridden)

    Returns:
        Dict mapping leverage labels (e.g., "2x") to BacktestResult objects
    """
    if base_config is None:
        base_config = BacktestConfig()

    results = {}
    for lev in leverage_tiers:
        config = BacktestConfig(
            fee_rate=base_config.fee_rate,
            slippage_rate=base_config.slippage_rate,
            holding_cost_rate=base_config.holding_cost_rate,
            min_trade_notional=base_config.min_trade_notional,
            rebalance_threshold=base_config.rebalance_threshold,
            leverage=lev,
            enable_liquidation=base_config.enable_liquidation,
            maintenance_margin_rate=base_config.maintenance_margin_rate,
            maintenance_margin_by_symbol=base_config.maintenance_margin_by_symbol,
            liquidation_buffer=base_config.liquidation_buffer,
            initial_capital=base_config.initial_capital,
            periods_per_year=base_config.periods_per_year,
            funding_rates=base_config.funding_rates,
        )
        result = run_backtest(prices, target_positions, config)
        label = f"{int(lev)}x" if float(lev).is_integer() else f"{lev:g}x"
        results[label] = result

    return results
