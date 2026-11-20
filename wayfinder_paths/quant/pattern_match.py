"""Deterministic historical pattern matching for Pattern Match.

This module deliberately accepts already-hydrated price series. Provider access,
cache policy, candle completion, and interval selection belong to the caller so
the same data pack can be handed to an agent without a second market-data pull.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

MIN_PATTERN_BARS = 12
DEFAULT_HORIZONS = (1, 3, 6, 12)


@dataclass(frozen=True)
class PriceSeries:
    """A timestamp-aligned close series from one provider and market."""

    symbol: str
    source: str
    timestamps_ms: Sequence[int]
    closes: Sequence[float]

    def arrays(self) -> tuple[np.ndarray, np.ndarray]:
        timestamps = np.asarray(self.timestamps_ms, dtype=np.int64)
        closes = np.asarray(self.closes, dtype=np.float64)
        if timestamps.ndim != 1 or closes.ndim != 1:
            raise ValueError("timestamps and closes must be one-dimensional")
        if len(timestamps) != len(closes):
            raise ValueError("timestamps and closes must have the same length")
        if len(timestamps) and np.any(np.diff(timestamps) <= 0):
            raise ValueError("timestamps must be strictly increasing")
        if np.any(~np.isfinite(closes)) or np.any(closes <= 0):
            raise ValueError("closes must contain finite positive values")
        return timestamps, closes


def normalized_price_shape(values: np.ndarray) -> np.ndarray | None:
    log_values = np.log(values)
    std = float(log_values.std())
    if not np.isfinite(std) or std <= 1e-12:
        return None
    return (log_values - float(log_values.mean())) / std


def price_path_features(values: np.ndarray) -> tuple[float, float]:
    log_values = np.log(values)
    log_returns = np.diff(log_values)
    path_range = float(log_values.max() - log_values.min())
    realized_volatility = float(
        np.sqrt(np.mean(np.square(log_returns))) * np.sqrt(len(log_returns))
    )
    return path_range, realized_volatility


def _ratio_similarity(candidate: float, query: float) -> tuple[float, float]:
    if query <= 1e-12 or candidate <= 1e-12:
        return (1.0, 1.0) if query <= 1e-12 and candidate <= 1e-12 else (0.0, 0.0)
    ratio = candidate / query
    return float(np.exp(-abs(np.log(ratio)))), ratio


def _summary(values: Sequence[float]) -> dict[str, float | int | None]:
    outcomes = np.asarray(values, dtype=np.float64)
    if not len(outcomes):
        return {
            "samples": 0,
            "mean_bps": None,
            "median_bps": None,
            "hit_rate_up": None,
            "q25_bps": None,
            "q75_bps": None,
        }
    return {
        "samples": int(len(outcomes)),
        "mean_bps": round(float(outcomes.mean()), 1),
        "median_bps": round(float(np.median(outcomes)), 1),
        "hit_rate_up": round(float((outcomes > 0).mean()), 3),
        "q25_bps": round(float(np.quantile(outcomes, 0.25)), 1),
        "q75_bps": round(float(np.quantile(outcomes, 0.75)), 1),
    }


def summarize_forward_outcomes(
    matches: Sequence[dict[str, Any]], horizons: Sequence[int]
) -> dict[str, dict[str, float | int | None]]:
    """Summarize the matcher outcome fields for a match subset."""

    return {
        f"{horizon}_bar": _summary(
            [float(match["outcomes"][f"{horizon}_bar_bps"]) for match in matches]
        )
        for horizon in horizons
    }


def _path_bps(values: np.ndarray, base: float) -> list[float]:
    return [round(float((value / base - 1) * 10_000), 1) for value in values]


def _forward_path_distribution(
    matches: Sequence[dict[str, Any]], max_horizon: int
) -> dict[str, Any]:
    paths = [
        match["forward_path_bps"]
        for match in matches
        if len(match.get("forward_path_bps", [])) == max_horizon + 1
    ]
    if not paths:
        return {
            "samples": 0,
            "median_bps": [],
            "q25_bps": [],
            "q75_bps": [],
            "hit_rate_up": [],
        }
    values = np.asarray(paths, dtype=np.float64)
    return {
        "samples": int(len(values)),
        "median_bps": [round(float(value), 1) for value in np.median(values, axis=0)],
        "q25_bps": [
            round(float(value), 1) for value in np.quantile(values, 0.25, axis=0)
        ],
        "q75_bps": [
            round(float(value), 1) for value in np.quantile(values, 0.75, axis=0)
        ],
        "hit_rate_up": [round(float(value), 3) for value in (values > 0).mean(axis=0)],
    }


def find_price_analogs(
    pattern: PriceSeries,
    histories: Sequence[PriceSeries],
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    top: int = 15,
    min_separation_bars: int | None = None,
    shape_paths: int = 0,
    forward_paths: int = 0,
) -> dict[str, Any]:
    """Find independent shape analogs and summarize their forward returns.

    Candidates from the pattern's own market that overlap its timestamp range
    are excluded. Selected matches within one source/market cannot overlap one
    another, which prevents one historical move from inflating the sample size.
    """

    pattern_timestamps, pattern_closes = pattern.arrays()
    window = len(pattern_closes)
    if window < MIN_PATTERN_BARS:
        raise ValueError(f"pattern needs at least {MIN_PATTERN_BARS} bars")
    query_shape = normalized_price_shape(pattern_closes)
    if query_shape is None:
        raise ValueError("pattern has zero price variance")
    query_range, query_volatility = price_path_features(pattern_closes)

    normalized_horizons = tuple(
        sorted({int(value) for value in horizons if int(value) > 0})
    )
    if not normalized_horizons:
        raise ValueError("at least one positive horizon is required")
    if top < 1:
        raise ValueError("top must be positive")
    separation = max(window, int(min_separation_bars or window))
    max_horizon = normalized_horizons[-1]
    query_start = int(pattern_timestamps[0])
    query_end = int(pattern_timestamps[-1])

    candidates: list[
        tuple[
            float,
            float,
            float,
            float,
            float,
            float,
            float,
            int,
            PriceSeries,
            np.ndarray,
            np.ndarray,
        ]
    ] = []
    usable_histories: list[dict[str, Any]] = []
    for history in histories:
        timestamps, closes = history.arrays()
        max_start = len(closes) - window - max_horizon
        usable_histories.append(
            {
                "symbol": history.symbol,
                "source": history.source,
                "bars": int(len(closes)),
                "candidate_windows": max(0, max_start + 1),
            }
        )
        for start in range(max(0, max_start + 1)):
            end = start + window
            candidate_start = int(timestamps[start])
            candidate_end = int(timestamps[end - 1])
            same_market = (
                history.symbol == pattern.symbol and history.source == pattern.source
            )
            overlaps_query = (
                candidate_start <= query_end and candidate_end >= query_start
            )
            if same_market and overlaps_query:
                continue
            shape = normalized_price_shape(closes[start:end])
            if shape is None:
                continue
            shape_distance = float(np.sqrt(np.mean((shape - query_shape) ** 2)))
            shape_similarity = float(np.exp(-shape_distance))
            candidate_range, candidate_volatility = price_path_features(
                closes[start:end]
            )
            magnitude_similarity, magnitude_ratio = _ratio_similarity(
                candidate_range, query_range
            )
            volatility_similarity, volatility_ratio = _ratio_similarity(
                candidate_volatility, query_volatility
            )
            similarity_score = (
                0.65 * shape_similarity
                + 0.20 * magnitude_similarity
                + 0.15 * volatility_similarity
            )
            candidates.append(
                (
                    similarity_score,
                    shape_distance,
                    shape_similarity,
                    magnitude_similarity,
                    volatility_similarity,
                    magnitude_ratio,
                    volatility_ratio,
                    start,
                    history,
                    timestamps,
                    closes,
                )
            )

    candidates.sort(key=lambda item: (-item[0], item[1]))
    selected_starts: dict[tuple[str, str], list[int]] = {}
    matches: list[dict[str, Any]] = []
    for (
        similarity_score,
        shape_distance,
        shape_similarity,
        magnitude_similarity,
        volatility_similarity,
        magnitude_ratio,
        volatility_ratio,
        start,
        history,
        timestamps,
        closes,
    ) in candidates:
        key = (history.source, history.symbol)
        taken = selected_starts.setdefault(key, [])
        if any(abs(start - other) < separation for other in taken):
            continue
        taken.append(start)
        end = start + window
        base = float(closes[end - 1])
        outcomes = {
            f"{horizon}_bar_bps": round(
                float((closes[end - 1 + horizon] / base - 1) * 10_000), 1
            )
            for horizon in normalized_horizons
        }
        match: dict[str, Any] = {
            "symbol": history.symbol,
            "source": history.source,
            "start_ms": int(timestamps[start]),
            "end_ms": int(timestamps[end - 1]),
            "distance": round(shape_distance, 4),
            "shape_similarity": round(shape_similarity, 4),
            "magnitude_similarity": round(magnitude_similarity, 4),
            "volatility_similarity": round(volatility_similarity, 4),
            "magnitude_ratio": round(magnitude_ratio, 4),
            "volatility_ratio": round(volatility_ratio, 4),
            "similarity_score": round(similarity_score, 4),
            "outcomes": outcomes,
        }
        if len(matches) < shape_paths:
            match["shape_path_bps"] = _path_bps(closes[start:end], float(closes[start]))
        match["forward_path_bps"] = _path_bps(
            closes[end - 1 : end + max_horizon],
            base,
        )
        matches.append(match)
        if len(matches) >= top:
            break

    outcome_distributions = summarize_forward_outcomes(matches, normalized_horizons)
    forward_path_distribution = _forward_path_distribution(matches, max_horizon)
    for index, match in enumerate(matches):
        if index >= forward_paths:
            match.pop("forward_path_bps", None)
    return {
        "pattern": {
            "symbol": pattern.symbol,
            "source": pattern.source,
            "start_ms": query_start,
            "end_ms": query_end,
            "bars": window,
            "return_bps": round(
                float((pattern_closes[-1] / pattern_closes[0] - 1) * 10_000), 1
            ),
            "range_bps": round(float(np.expm1(query_range) * 10_000), 1),
            "realized_volatility_bps": round(query_volatility * 10_000, 1),
            "shape_path_bps": _path_bps(pattern_closes, float(pattern_closes[0])),
        },
        "histories": usable_histories,
        "matches": matches,
        "outcome_distributions": outcome_distributions,
        "forward_path_distribution": forward_path_distribution,
    }
