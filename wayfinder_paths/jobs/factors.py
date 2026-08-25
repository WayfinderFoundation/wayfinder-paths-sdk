"""Causal panel transforms for jobs_v1 factor research.

These helpers build scores only. Economic PnL, fills, fees, funding, and
drawdowns remain owned by the jobs_v1 execution simulator.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd


def panel_from_frames(
    frames: Mapping[str, pd.DataFrame],
    column: str,
    *,
    symbols: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Align one column from per-symbol frames on their UTC timestamps."""
    selected = list(symbols) if symbols is not None else list(frames)
    series: dict[str, pd.Series] = {}
    for symbol in selected:
        frame = frames.get(symbol)
        if frame is None or column not in frame:
            continue
        timestamps = pd.to_datetime(frame["timestamp"], utc=True)
        values = pd.to_numeric(frame[column], errors="coerce")
        series[symbol] = pd.Series(values.to_numpy(), index=timestamps)
    if not series:
        return pd.DataFrame()
    return pd.concat(series, axis=1).sort_index()


def _numeric_panel(values: pd.DataFrame) -> pd.DataFrame:
    numeric = values.apply(pd.to_numeric, errors="coerce")
    return numeric.where(np.isfinite(numeric))


def _eligible_values(
    values: pd.DataFrame, eligible: pd.DataFrame | None
) -> pd.DataFrame:
    numeric = _numeric_panel(values)
    if eligible is None:
        return numeric
    mask = eligible.reindex(index=numeric.index, columns=numeric.columns)
    return numeric.where(mask.fillna(False).astype(bool))


def cross_sectional_rank(
    values: pd.DataFrame, eligible: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Rank each timestamp symmetrically from -1 (low) to +1 (high).

    Ties receive their average rank. A row needs at least two eligible finite
    assets; a one-asset "cross-section" is undefined and remains missing.
    """
    masked = _eligible_values(values, eligible)
    ranks = masked.rank(axis=1, method="average")
    denominator = masked.notna().sum(axis=1).sub(1).replace(0, np.nan)
    return ranks.sub(1).div(denominator, axis=0).mul(2).sub(1).where(masked.notna())


def cross_sectional_robust_zscore(
    values: pd.DataFrame,
    eligible: pd.DataFrame | None = None,
    *,
    clip: float | None = 5.0,
) -> pd.DataFrame:
    """Median/MAD normalize each timestamp without full-sample leakage."""
    if clip is not None and clip <= 0:
        raise ValueError("robust z-score clip must be positive")
    masked = _eligible_values(values, eligible)
    median = masked.median(axis=1, skipna=True)
    deviation = masked.sub(median, axis=0).abs()
    scale = deviation.median(axis=1, skipna=True).mul(1.4826).replace(0, np.nan)
    result = masked.sub(median, axis=0).div(scale, axis=0)
    return result.clip(-clip, clip) if clip is not None else result


def rolling_beta(
    returns: pd.DataFrame,
    benchmark_returns: pd.Series,
    period: int,
    *,
    min_periods: int | None = None,
    clip: tuple[float, float] | None = (-3.0, 3.0),
) -> pd.DataFrame:
    """Rolling asset beta to a benchmark using information through each row."""
    if period <= 1:
        raise ValueError("rolling beta period must be greater than one")
    minimum = period if min_periods is None else int(min_periods)
    if minimum <= 1 or minimum > period:
        raise ValueError("rolling beta min_periods must be in [2, period]")
    assets = _numeric_panel(returns)
    benchmark = pd.to_numeric(
        benchmark_returns.reindex(assets.index), errors="coerce"
    ).where(lambda series: np.isfinite(series))
    asset_mean = assets.rolling(period, min_periods=minimum).mean()
    benchmark_mean = benchmark.rolling(period, min_periods=minimum).mean()
    covariance = assets.mul(benchmark, axis=0).rolling(
        period, min_periods=minimum
    ).mean() - asset_mean.mul(benchmark_mean, axis=0)
    variance = (
        benchmark.pow(2).rolling(period, min_periods=minimum).mean()
        - benchmark_mean.pow(2)
    ).replace(0, np.nan)
    result = covariance.div(variance, axis=0)
    return result.clip(*clip) if clip is not None else result


def residual_return(
    close: pd.DataFrame,
    benchmark_close: pd.Series,
    horizon: int,
    *,
    beta_period: int,
    beta_min_periods: int | None = None,
    beta_clip: tuple[float, float] | None = (-3.0, 3.0),
) -> pd.DataFrame:
    """Benchmark-beta residual simple return over a trailing horizon."""
    if horizon <= 0:
        raise ValueError("residual return horizon must be positive")
    prices = _numeric_panel(close)
    benchmark = pd.to_numeric(benchmark_close.reindex(prices.index), errors="coerce")
    asset_bar_returns = prices.pct_change(fill_method=None)
    benchmark_bar_returns = benchmark.pct_change(fill_method=None)
    beta = rolling_beta(
        asset_bar_returns,
        benchmark_bar_returns,
        beta_period,
        min_periods=beta_min_periods,
        clip=beta_clip,
    )
    asset_horizon = prices.pct_change(horizon, fill_method=None)
    benchmark_horizon = benchmark.pct_change(horizon, fill_method=None)
    return asset_horizon - beta.mul(benchmark_horizon, axis=0)


def blend_factor_scores(
    factors: Mapping[str, pd.DataFrame],
    weights: Mapping[str, float],
    eligible: pd.DataFrame | None = None,
    *,
    rerank: bool = True,
) -> pd.DataFrame:
    """Blend aligned factor panels with absolute weights normalized to one."""
    if not factors:
        raise ValueError("factor blend requires at least one factor")
    unknown = set(weights) - set(factors)
    missing = set(factors) - set(weights)
    if unknown or missing:
        raise ValueError(
            f"factor weights must match factors; missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    scale = sum(abs(float(weight)) for weight in weights.values())
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("factor blend weights must have positive finite magnitude")
    first = next(iter(factors.values()))
    blended = pd.DataFrame(0.0, index=first.index, columns=first.columns)
    valid = pd.DataFrame(True, index=first.index, columns=first.columns)
    for name, panel in factors.items():
        aligned = _numeric_panel(panel).reindex(
            index=first.index, columns=first.columns
        )
        blended = blended + aligned.fillna(0.0) * (float(weights[name]) / scale)
        valid &= aligned.notna()
    blended = blended.where(valid)
    if eligible is not None:
        mask = eligible.reindex(index=first.index, columns=first.columns)
        blended = blended.where(mask.fillna(False).astype(bool))
    return cross_sectional_rank(blended) if rerank else blended
