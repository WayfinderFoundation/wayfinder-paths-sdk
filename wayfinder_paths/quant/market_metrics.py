"""Small market metrics helpers for bounded Wayfinder scripts."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def _array(values: Sequence[float]) -> np.ndarray:
    return np.asarray(values, dtype=float)


def max_drawdown(equity: Sequence[float]) -> float:
    values = _array(equity)
    if values.size == 0:
        return 0.0
    peaks = np.maximum.accumulate(values)
    drawdowns = values / peaks - 1.0
    return float(np.nanmin(drawdowns))


def sharpe(returns: Sequence[float], periods_per_year: int = 252) -> float:
    values = _array(returns)
    if values.size == 0:
        return 0.0
    volatility = np.nanstd(values, ddof=0)
    if volatility == 0:
        return 0.0
    return float(np.sqrt(periods_per_year) * np.nanmean(values) / volatility)


def sortino(returns: Sequence[float], periods_per_year: int = 252) -> float:
    values = _array(returns)
    downside = values[values < 0]
    if values.size == 0 or downside.size == 0:
        return 0.0
    downside_volatility = np.nanstd(downside, ddof=0)
    if downside_volatility == 0:
        return 0.0
    return float(np.sqrt(periods_per_year) * np.nanmean(values) / downside_volatility)


def beta(asset_returns: Sequence[float], benchmark_returns: Sequence[float]) -> float:
    asset = _array(asset_returns)
    benchmark = _array(benchmark_returns)
    length = min(asset.size, benchmark.size)
    if length < 2:
        return 0.0
    asset = asset[:length]
    benchmark = benchmark[:length]
    benchmark_variance = np.nanvar(benchmark, ddof=0)
    if benchmark_variance == 0:
        return 0.0
    covariance = np.nanmean(
        (asset - np.nanmean(asset)) * (benchmark - np.nanmean(benchmark))
    )
    return float(covariance / benchmark_variance)


def funding_adjusted_returns(
    price_returns: Sequence[float],
    funding_rates: Sequence[float],
    side: str = "long",
) -> list[float]:
    """Return perp returns after funding. Positive funding means longs pay shorts."""
    length = min(len(price_returns), len(funding_rates))
    normalized_side = side.lower()
    if normalized_side == "long":
        return [
            float(price_returns[index]) - float(funding_rates[index])
            for index in range(length)
        ]
    if normalized_side == "short":
        return [
            -float(price_returns[index]) + float(funding_rates[index])
            for index in range(length)
        ]
    raise ValueError("side must be 'long' or 'short'")


def turnover_cost(
    turnover: Sequence[float],
    fee_bps: float,
    slippage_bps: float = 0.0,
) -> list[float]:
    cost_rate = (float(fee_bps) + float(slippage_bps)) / 10_000.0
    return [float(value) * cost_rate for value in turnover]
