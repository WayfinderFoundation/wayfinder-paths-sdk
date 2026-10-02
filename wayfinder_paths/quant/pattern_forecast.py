"""Provider-neutral OHLC analogue forecasts.

Market-data hydration, calibration, publication policy, and persistence belong
to callers.  This module only scores a completed, timestamp-aligned OHLC series
so the same implementation can be used for spot and derivative markets.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from wayfinder_paths.quant.pattern_match import (
    normalized_price_shape,
    price_path_features,
)

DEFAULT_HORIZONS = (2, 4, 8, 24, 48)
DEFAULT_INTERVAL_MS = 15 * 60 * 1_000


@dataclass(frozen=True)
class OhlcSeries:
    """A completed OHLC series for one provider market."""

    symbol: str
    source: str
    timestamps_ms: Sequence[int]
    opens: Sequence[float]
    highs: Sequence[float]
    lows: Sequence[float]
    closes: Sequence[float]

    def arrays(self) -> tuple[np.ndarray, ...]:
        values = (
            np.asarray(self.timestamps_ms, dtype=np.int64),
            np.asarray(self.opens, dtype=np.float64),
            np.asarray(self.highs, dtype=np.float64),
            np.asarray(self.lows, dtype=np.float64),
            np.asarray(self.closes, dtype=np.float64),
        )
        if any(value.ndim != 1 for value in values):
            raise ValueError("timestamps and OHLC values must be one-dimensional")
        if len({len(value) for value in values}) != 1:
            raise ValueError("timestamps and OHLC values must have the same length")
        timestamps, opens, highs, lows, closes = values
        if len(timestamps) and np.any(np.diff(timestamps) <= 0):
            raise ValueError("timestamps must be strictly increasing")
        prices = np.column_stack((opens, highs, lows, closes))
        if np.any(~np.isfinite(prices)) or np.any(prices <= 0):
            raise ValueError("OHLC values must be finite and positive")
        if np.any(highs < np.maximum(opens, closes)):
            raise ValueError("high must be at least open and close")
        if np.any(lows > np.minimum(opens, closes)):
            raise ValueError("low must be at most open and close")
        return values


@dataclass(frozen=True)
class PatternForecastConfig:
    """Stable inputs controlling analogue selection and forecast output."""

    pattern_bars: int = 96
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    history_limit: int = 10_000
    top_matches: int = 63
    display_matches: int = 15
    range_horizon_bars: int = 12
    interval_ms: int = DEFAULT_INTERVAL_MS


@dataclass(frozen=True)
class AnalogueMatch:
    start_ms: int
    end_ms: int
    similarity: float
    shape_distance: float
    forward_path_bps: tuple[float, ...] | None = None


@dataclass(frozen=True)
class PatternForecast:
    symbol: str
    source: str
    as_of_ms: int
    pattern_bars: int
    match_count: int
    probability_up: float
    probability_up_by_horizon: dict[int, float]
    horizon_agreement: float
    mean_similarity: float
    similarity_margin_product: float
    range_q25: float
    range_q50: float
    range_q75: float
    endpoint_q25: float
    endpoint_q50: float
    endpoint_q75: float
    normalized_steepness: float
    median_path_bps: tuple[float, ...]
    q25_path_bps: tuple[float, ...]
    q75_path_bps: tuple[float, ...]
    hit_rate_up_path: tuple[float, ...]
    matches: tuple[AnalogueMatch, ...]

    @property
    def direction(self) -> int:
        return 1 if self.probability_up > 0.5 else -1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def forecast_price_analogs(
    series: OhlcSeries,
    *,
    config: PatternForecastConfig = PatternForecastConfig(),
) -> PatternForecast:
    """Forecast the latest completed bar from non-overlapping analogues."""

    timestamps, opens, highs, lows, closes = series.arrays()
    _validate_config(config)
    if len(closes) < config.pattern_bars + max(config.horizons) + 1:
        raise ValueError("insufficient history for pattern and outcomes")
    if np.any(np.diff(timestamps) != config.interval_ms):
        raise ValueError("timestamps must be consecutive at the configured interval")

    query_end = len(closes) - 1
    query_start = query_end - config.pattern_bars + 1
    query_values = closes[query_start : query_end + 1]
    query_shape = normalized_price_shape(query_values)
    if query_shape is None:
        raise ValueError("pattern has zero price variance")
    query_range, query_volatility = price_path_features(query_values)

    max_horizon = max(config.horizons)
    minimum_start = max(0, query_end - config.history_limit + 1)
    maximum_end = min(query_start - 1, query_end - max_horizon)
    maximum_start = maximum_end - config.pattern_bars + 1
    if maximum_start < minimum_start:
        raise ValueError("insufficient independent analogue history")

    starts = np.arange(minimum_start, maximum_start + 1, dtype=np.int64)
    windows = np.lib.stride_tricks.sliding_window_view(
        np.log(closes), config.pattern_bars
    )[starts]
    deviations = windows - windows.mean(axis=1)[:, None]
    stds = windows.std(axis=1)
    valid = np.isfinite(stds) & (stds > 1e-12)
    starts = starts[valid]
    windows = windows[valid]
    deviations = deviations[valid]
    stds = stds[valid]
    if not len(starts):
        raise ValueError("no variable analogue windows")

    normalized = deviations / stds[:, None]
    correlation = normalized @ query_shape / config.pattern_bars
    distances = np.sqrt(np.maximum(0.0, 2.0 - 2.0 * correlation))
    shape_similarity = np.exp(-distances)
    path_ranges = windows.max(axis=1) - windows.min(axis=1)
    log_returns = np.diff(windows, axis=1)
    volatilities = np.sqrt(np.square(log_returns).mean(axis=1)) * np.sqrt(
        config.pattern_bars - 1
    )
    similarities = (
        0.65 * shape_similarity
        + 0.20 * _ratio_similarities(path_ranges, query_range)
        + 0.15 * _ratio_similarities(volatilities, query_volatility)
    )
    selected = _select_non_overlapping(
        similarities,
        distances,
        starts,
        separation=config.pattern_bars,
        top=config.top_matches,
    )
    if len(selected) < config.top_matches:
        raise ValueError(
            f"only {len(selected)} independent analogues; {config.top_matches} required"
        )

    selected_starts = starts[selected]
    selected_ends = selected_starts + config.pattern_bars - 1
    selected_similarity = similarities[selected]
    outcomes = np.column_stack(
        [
            closes[selected_ends + horizon] / closes[selected_ends] - 1.0
            for horizon in config.horizons
        ]
    )
    probabilities = (outcomes > 0).mean(axis=0)
    probability_up = float(probabilities.mean())
    horizon_agreement = float(
        max((probabilities > 0.5).mean(), (probabilities < 0.5).mean())
    )

    range_offsets = np.arange(1, config.range_horizon_bars + 1)
    range_indices = selected_ends[:, None] + range_offsets[None, :]
    entries = opens[selected_ends + 1]
    ranges = highs[range_indices].max(axis=1) / lows[range_indices].min(axis=1) - 1.0
    endpoints = closes[selected_ends + config.range_horizon_bars] / entries - 1.0
    path_offsets = np.arange(0, max_horizon + 1)
    path_indices = selected_ends[:, None] + path_offsets[None, :]
    paths_bps = (closes[path_indices] / closes[selected_ends, None] - 1.0) * 10_000

    range_quantiles = np.quantile(ranges, (0.25, 0.5, 0.75))
    endpoint_quantiles = np.quantile(endpoints, (0.25, 0.5, 0.75))
    direction = 1 if probability_up > 0.5 else -1
    normalized_steepness = (
        direction * float(endpoint_quantiles[1]) / float(range_quantiles[1])
    )
    display_count = min(config.display_matches, len(selected))
    matches = tuple(
        AnalogueMatch(
            start_ms=int(timestamps[start]),
            end_ms=int(timestamps[end]),
            similarity=float(similarity),
            shape_distance=float(distance),
            forward_path_bps=(
                tuple(float(value) for value in path) if index < display_count else None
            ),
        )
        for index, (start, end, similarity, distance, path) in enumerate(
            zip(
                selected_starts,
                selected_ends,
                selected_similarity,
                distances[selected],
                paths_bps,
                strict=True,
            )
        )
    )
    mean_similarity = float(selected_similarity.mean())
    return PatternForecast(
        symbol=series.symbol,
        source=series.source,
        as_of_ms=int(timestamps[-1] + config.interval_ms),
        pattern_bars=config.pattern_bars,
        match_count=len(selected),
        probability_up=probability_up,
        probability_up_by_horizon={
            horizon: float(value)
            for horizon, value in zip(config.horizons, probabilities, strict=True)
        },
        horizon_agreement=horizon_agreement,
        mean_similarity=mean_similarity,
        similarity_margin_product=mean_similarity * abs(probability_up - 0.5),
        range_q25=float(range_quantiles[0]),
        range_q50=float(range_quantiles[1]),
        range_q75=float(range_quantiles[2]),
        endpoint_q25=float(endpoint_quantiles[0]),
        endpoint_q50=float(endpoint_quantiles[1]),
        endpoint_q75=float(endpoint_quantiles[2]),
        normalized_steepness=normalized_steepness,
        median_path_bps=tuple(float(value) for value in np.median(paths_bps, axis=0)),
        q25_path_bps=tuple(
            float(value) for value in np.quantile(paths_bps, 0.25, axis=0)
        ),
        q75_path_bps=tuple(
            float(value) for value in np.quantile(paths_bps, 0.75, axis=0)
        ),
        hit_rate_up_path=tuple(float(value) for value in (paths_bps > 0).mean(axis=0)),
        matches=matches,
    )


def _validate_config(config: PatternForecastConfig) -> None:
    if config.pattern_bars < 12:
        raise ValueError("pattern_bars must be at least 12")
    if not config.horizons or min(config.horizons) < 1:
        raise ValueError("horizons must contain positive bars")
    if config.range_horizon_bars > max(config.horizons):
        raise ValueError("range horizon must fit inside the maximum forecast horizon")
    if config.top_matches < 1 or config.display_matches < 0:
        raise ValueError("match counts must be non-negative and top_matches positive")
    if config.history_limit < config.pattern_bars:
        raise ValueError("history_limit must cover the pattern")
    if config.interval_ms < 1:
        raise ValueError("interval_ms must be positive")


def _ratio_similarities(candidates: np.ndarray, query: float) -> np.ndarray:
    output = np.zeros(len(candidates), dtype=np.float64)
    if query <= 1e-12:
        output[candidates <= 1e-12] = 1.0
        return output
    mask = candidates > 1e-12
    output[mask] = np.exp(-np.abs(np.log(candidates[mask] / query)))
    return output


def _select_non_overlapping(
    scores: np.ndarray,
    distances: np.ndarray,
    starts: np.ndarray,
    *,
    separation: int,
    top: int,
) -> np.ndarray:
    order = np.lexsort((starts, distances, -scores))
    chosen: list[int] = []
    chosen_starts: list[int] = []
    for index in order:
        start = int(starts[index])
        if any(abs(start - other) < separation for other in chosen_starts):
            continue
        chosen.append(int(index))
        chosen_starts.append(start)
        if len(chosen) == top:
            break
    return np.asarray(chosen, dtype=np.int64)


__all__ = [
    "AnalogueMatch",
    "DEFAULT_HORIZONS",
    "DEFAULT_INTERVAL_MS",
    "OhlcSeries",
    "PatternForecast",
    "PatternForecastConfig",
    "forecast_price_analogs",
]
