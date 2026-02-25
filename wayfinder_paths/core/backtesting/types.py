"""Type definitions for backtesting framework."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

import pandas as pd


class BacktestStats(TypedDict, total=False):
    """
    Type-safe schema for backtest statistics.
    All rate/return values in decimal format (0-1 scale).

    Use this for IDE autocomplete and type safety:
        stats: BacktestStats = result.stats
        print(stats['sharpe'])  # IDE knows this exists

    Note: Some stats may be NaN (not None) when not applicable:
        - buy_hold_return: NaN if prices not available
        - profit_factor: NaN if no losing trades
        Use np.isnan() to check, or format will show "nan"
    """

    # Time metrics
    start: pd.Timestamp
    end: pd.Timestamp
    duration: pd.Timedelta
    exposure_time_pct: float

    # Equity metrics
    equity_final: float
    equity_peak: float
    total_return: float
    buy_hold_return: float  # NaN if prices not available

    # Return metrics
    return_ann: float  # Same as cagr
    volatility_ann: float
    cagr: float

    # Risk-adjusted metrics
    sharpe: float
    sortino: float
    calmar: float

    # Drawdown metrics
    max_drawdown: float
    avg_drawdown: float
    max_drawdown_duration: pd.Timedelta
    avg_drawdown_duration: pd.Timedelta

    # Trade metrics
    trade_count: int
    win_rate: float
    best_trade: float
    worst_trade: float
    avg_trade: float
    max_trade_duration: pd.Timedelta
    avg_trade_duration: pd.Timedelta
    profit_factor: float  # NaN if no losing trades
    expectancy: float
    sqn: float
    kelly_criterion: float

    # Cost metrics
    avg_turnover: float
    avg_cost: float
    final_equity: float
    total_fees: float
    total_funding: float


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
        periods_per_year: Number of periods in one year - CRITICAL for Sharpe/volatility.
            If None, will auto-detect from data frequency.
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
    periods_per_year: int | None = None  # If None, will auto-detect from data frequency
    funding_rates: pd.DataFrame | None = None


@dataclass
class BacktestResult:
    """
    Results from a backtest simulation.

    Attributes:
        equity_curve: Portfolio value over time (pd.Series, index=timestamps)
        returns: Period-over-period returns (pd.Series, index=timestamps)
        stats: Performance statistics (BacktestStats TypedDict for IDE autocomplete)
        trades: List of trade events with timestamps, symbols, costs
        metrics_by_period: DataFrame with equity, turnover, cost, exposure per period
        positions_over_time: DataFrame of position sizes per symbol over time
        liquidated: Whether the strategy was liquidated
        liquidation_timestamp: Timestamp of liquidation (if occurred)

    Stats Schema: See BacktestStats TypedDict for complete schema.
        All rate/return values in decimal format (0-1 scale):
        - total_return: 0.45 = 45%
        - max_drawdown: -0.25 = -25%
        - win_rate: 0.55 = 55%

    Note: Some stats may be NaN when not applicable (buy_hold_return, profit_factor).
        Format directly: f"{stats['profit_factor']:.2f}" → "nan"
        Or check: if not np.isnan(stats['profit_factor']): ...

    Example:
        >>> import numpy as np
        >>> result = run_backtest(prices, positions, config)
        >>> stats: BacktestStats = result.stats  # Type hint for IDE autocomplete
        >>> print(f"Return: {stats['total_return']:.2%}")  # "45.20%"
        >>> print(f"Sharpe: {stats['sharpe']:.2f}")  # "3.31"
        >>> print(f"Max DD: {stats['max_drawdown']:.2%}")  # "-25.30%"
        >>> # Handle NaN values
        >>> pf = stats['profit_factor']
        >>> pf_str = f"{pf:.2f}" if not np.isnan(pf) else "N/A"
        >>> print(f"PF: {pf_str}")  # "N/A" or "2.35"
    """

    equity_curve: pd.Series
    returns: pd.Series
    stats: BacktestStats
    trades: list[dict[str, Any]]
    metrics_by_period: pd.DataFrame
    positions_over_time: pd.DataFrame
    liquidated: bool = False
    liquidation_timestamp: pd.Timestamp | None = None
