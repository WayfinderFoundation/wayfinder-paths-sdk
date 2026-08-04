"""Deterministic historical-pattern matching for Fractal Scan.

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


def _normalized_shape(values: np.ndarray) -> np.ndarray | None:
    log_values = np.log(values)
    std = float(log_values.std())
    if not np.isfinite(std) or std <= 1e-12:
        return None
    return (log_values - float(log_values.mean())) / std


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


def find_price_analogs(
    pattern: PriceSeries,
    histories: Sequence[PriceSeries],
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    top: int = 15,
    min_separation_bars: int | None = None,
    shape_paths: int = 0,
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
    query_shape = _normalized_shape(pattern_closes)
    if query_shape is None:
        raise ValueError("pattern has zero price variance")

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

    candidates: list[tuple[float, int, PriceSeries, np.ndarray, np.ndarray]] = []
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
            shape = _normalized_shape(closes[start:end])
            if shape is None:
                continue
            distance = float(np.sqrt(np.mean((shape - query_shape) ** 2)))
            candidates.append((distance, start, history, timestamps, closes))

    candidates.sort(key=lambda item: item[0])
    selected_starts: dict[tuple[str, str], list[int]] = {}
    matches: list[dict[str, Any]] = []
    for distance, start, history, timestamps, closes in candidates:
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
            "distance": round(distance, 4),
            "outcomes": outcomes,
        }
        if len(matches) < shape_paths:
            match["shape_path_bps"] = _path_bps(closes[start:end], float(closes[start]))
        matches.append(match)
        if len(matches) >= top:
            break

    outcome_distributions = summarize_forward_outcomes(matches, normalized_horizons)
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
            "shape_path_bps": _path_bps(pattern_closes, float(pattern_closes[0])),
        },
        "histories": usable_histories,
        "matches": matches,
        "outcome_distributions": outcome_distributions,
    }
