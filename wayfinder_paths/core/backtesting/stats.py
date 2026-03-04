"""Statistics calculation for backtesting framework."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.core.backtesting.types import BacktestStats


def calculate_stats(
    returns: pd.Series,
    equity_curve: pd.Series,
    trades: list[dict[str, Any]],
    turnover_series: list[float],
    cost_series: list[float],
    fee_series: list[float],
    funding_series: list[float],
    periods_per_year: int,
    prices: pd.DataFrame | None = None,
) -> BacktestStats:
    """
    Calculate comprehensive performance statistics.

    Returns:
        Dict with performance metrics. All rates/returns in decimal format (0-1 scale):
        - start: Start timestamp
        - end: End timestamp
        - duration: Duration timedelta
        - exposure_time_pct: Percentage of time with non-zero exposure
        - equity_final: Final portfolio value
        - equity_peak: Peak portfolio value
        - total_return: 0.45 = 45% return
        - buy_hold_return: Equal-weight buy & hold return (if prices available)
        - return_ann: Annualized return (same as cagr)
        - volatility_ann: Annualized volatility
        - cagr: Annualized return
        - sharpe: Sharpe ratio
        - sortino: Sortino ratio
        - calmar: Calmar ratio (CAGR / abs(max_drawdown))
        - max_drawdown: Peak-to-trough decline
        - avg_drawdown: Average drawdown across all drawdown periods
        - max_drawdown_duration: Longest drawdown duration (timedelta)
        - avg_drawdown_duration: Average drawdown duration (timedelta)
        - trade_count: Number of trades
        - win_rate: Fraction of winning trades
        - best_trade: Best single trade return
        - worst_trade: Worst single trade return
        - avg_trade: Average trade return
        - max_trade_duration: Longest time between trades (timedelta)
        - avg_trade_duration: Average time between trades (timedelta)
        - profit_factor: Gross profit / gross loss
        - expectancy: Average trade return (same as avg_trade)
        - sqn: System Quality Number
        - kelly_criterion: Kelly criterion for optimal position sizing
        - avg_turnover: Average portfolio turnover per period
        - avg_cost: Average transaction cost per period
        - final_equity: Ending portfolio value
        - total_fees: Total transaction fees paid
        - total_funding: Total funding costs/income
    """
    if len(returns) == 0 or len(equity_curve) == 0:
        return empty_stats()

    # Time-based metrics
    start = equity_curve.index[0]
    end = equity_curve.index[-1]
    duration = end - start

    # Exposure time (% of time with non-zero positions)
    exposure_count = sum(1 for t in turnover_series if t > 0)
    exposure_time_pct = (
        exposure_count / len(turnover_series) if turnover_series else 0.0
    )

    # Equity metrics
    equity_final = float(equity_curve.iloc[-1])
    equity_peak = float(equity_curve.max())
    total_return = equity_final - 1.0

    # Buy & Hold return (equal-weight buy and hold of all assets)
    buy_hold_return = np.nan
    if prices is not None and not prices.empty:
        initial_prices = prices.iloc[0]
        final_prices = prices.iloc[-1]
        asset_returns = (final_prices / initial_prices) - 1
        buy_hold_return = float(asset_returns.mean())

    # Return and volatility metrics
    mean_return = returns.mean()
    volatility = returns.std(ddof=0)
    volatility_ann = volatility * np.sqrt(periods_per_year)

    years = len(returns) / periods_per_year if periods_per_year > 0 else 0
    cagr = (
        float(equity_curve.iloc[-1] ** (1 / years) - 1)
        if years > 0 and len(equity_curve) > 0
        else 0.0
    )
    return_ann = cagr

    # Risk-adjusted metrics
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

    # Drawdown metrics
    cummax = equity_curve.cummax()
    drawdowns = equity_curve / cummax - 1
    max_drawdown = float(drawdowns.min()) if not drawdowns.empty else 0.0

    calmar = abs(cagr / max_drawdown) if max_drawdown != 0 else 0.0

    # Calculate all drawdown periods
    in_drawdown = drawdowns < 0
    drawdown_periods = []
    start_dd = None
    for ts, dd_flag in in_drawdown.items():
        if dd_flag and start_dd is None:
            start_dd = ts
        elif not dd_flag and start_dd is not None:
            drawdown_periods.append((start_dd, ts))
            start_dd = None
    if start_dd is not None:
        drawdown_periods.append((start_dd, equity_curve.index[-1]))

    avg_drawdown = (
        float(drawdowns[drawdowns < 0].mean())
        if len(drawdowns[drawdowns < 0]) > 0
        else 0.0
    )

    if drawdown_periods:
        drawdown_durations = [(end - start) for start, end in drawdown_periods]
        max_drawdown_duration = max(drawdown_durations)
        avg_drawdown_duration = sum(drawdown_durations, pd.Timedelta(0)) / len(
            drawdown_durations
        )
    else:
        max_drawdown_duration = pd.Timedelta(0)
        avg_drawdown_duration = pd.Timedelta(0)

    # Trade-based metrics
    # For continuous rebalancing strategies, use period returns instead of per-trade PnL
    trade_count = len(trades)

    if len(returns) > 0:
        best_trade = float(returns.max())
        worst_trade = float(returns.min())
        avg_trade = float(returns.mean())
        expectancy = avg_trade

        # SQN (System Quality Number) = sqrt(n) * avg / std
        trade_std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
        sqn = (np.sqrt(len(returns)) * avg_trade / trade_std) if trade_std > 0 else 0.0

        # Trade durations (time between rebalance events)
        if trade_count > 1:
            trade_times = sorted({trade["timestamp"] for trade in trades})
            if len(trade_times) > 1:
                durations = [
                    trade_times[i + 1] - trade_times[i]
                    for i in range(len(trade_times) - 1)
                ]
                max_trade_duration = max(durations)
                avg_trade_duration = sum(durations, pd.Timedelta(0)) / len(durations)
            else:
                max_trade_duration = pd.Timedelta(0)
                avg_trade_duration = pd.Timedelta(0)
        else:
            max_trade_duration = pd.Timedelta(0)
            avg_trade_duration = pd.Timedelta(0)
    else:
        best_trade = 0.0
        worst_trade = 0.0
        avg_trade = 0.0
        expectancy = 0.0
        sqn = 0.0
        max_trade_duration = pd.Timedelta(0)
        avg_trade_duration = pd.Timedelta(0)

    # Win rate and profit factor
    win_rate = float((returns > 0).mean()) if len(returns) > 0 else 0.0

    positive_returns = returns[returns > 0].sum()
    negative_returns = returns[returns < 0].sum()
    profit_factor = (
        float(positive_returns / abs(negative_returns))
        if negative_returns < 0
        else np.nan
    )

    # Kelly Criterion = win_rate - (1 - win_rate) / (avg_win / abs(avg_loss))
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    if len(wins) > 0 and len(losses) > 0:
        avg_win = float(wins.mean())
        avg_loss = float(abs(losses.mean()))
        win_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0
        kelly = (
            win_rate - ((1 - win_rate) / win_loss_ratio) if win_loss_ratio > 0 else 0.0
        )
    else:
        kelly = 0.0

    # Turnover and cost metrics
    avg_turnover = float(np.mean(turnover_series)) if turnover_series else 0.0
    avg_cost = float(np.mean(cost_series)) if cost_series else 0.0
    total_fees = float(sum(fee_series)) if fee_series else 0.0
    total_funding = float(sum(funding_series)) if funding_series else 0.0

    return {
        "start": start,
        "end": end,
        "duration": duration,
        "exposure_time_pct": round(exposure_time_pct, 4),
        "equity_final": round(equity_final, 4),
        "equity_peak": round(equity_peak, 4),
        "total_return": round(total_return, 4),
        "buy_hold_return": round(buy_hold_return, 4)
        if not np.isnan(buy_hold_return)
        else np.nan,
        "return_ann": round(return_ann, 4),
        "volatility_ann": round(volatility_ann, 4),
        "cagr": round(cagr, 4),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "calmar": round(calmar, 3),
        "max_drawdown": round(max_drawdown, 4),
        "avg_drawdown": round(avg_drawdown, 4),
        "max_drawdown_duration": max_drawdown_duration,
        "avg_drawdown_duration": avg_drawdown_duration,
        "trade_count": trade_count,
        "win_rate": round(win_rate, 4),
        "best_trade": round(best_trade, 4),
        "worst_trade": round(worst_trade, 4),
        "avg_trade": round(avg_trade, 4),
        "max_trade_duration": max_trade_duration,
        "avg_trade_duration": avg_trade_duration,
        "profit_factor": round(profit_factor, 2)
        if not np.isnan(profit_factor)
        else np.nan,
        "expectancy": round(expectancy, 4),
        "sqn": round(sqn, 3),
        "kelly_criterion": round(kelly, 4),
        "avg_turnover": round(avg_turnover, 4),
        "avg_cost": round(avg_cost, 6),
        "final_equity": round(equity_final, 4),
        "total_fees": round(total_fees, 4),
        "total_funding": round(total_funding, 4),
    }


def empty_stats() -> BacktestStats:
    """
    Return empty statistics dict for edge cases.

    All rate/return values are in decimal format (0-1 scale):
    - 0.10 = 10% return
    - -0.05 = -5% drawdown
    - 0.55 = 55% win rate
    """
    return {
        "start": None,
        "end": None,
        "duration": None,
        "exposure_time_pct": 0.0,
        "equity_final": 1.0,
        "equity_peak": 1.0,
        "total_return": 0.0,
        "buy_hold_return": np.nan,
        "return_ann": 0.0,
        "volatility_ann": 0.0,
        "cagr": 0.0,
        "sharpe": 0.0,
        "sortino": 0.0,
        "calmar": 0.0,
        "max_drawdown": 0.0,
        "avg_drawdown": 0.0,
        "max_drawdown_duration": None,
        "avg_drawdown_duration": None,
        "trade_count": 0,
        "win_rate": 0.0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "avg_trade": 0.0,
        "max_trade_duration": None,
        "avg_trade_duration": None,
        "profit_factor": np.nan,
        "expectancy": 0.0,
        "sqn": 0.0,
        "kelly_criterion": 0.0,
        "avg_turnover": 0.0,
        "avg_cost": 0.0,
        "final_equity": 1.0,
        "total_fees": 0.0,
        "total_funding": 0.0,
    }
