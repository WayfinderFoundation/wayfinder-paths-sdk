"""Causal 15-minute analogue scoring for a liquid perp universe.

This is the production form of the Pattern Match universe benchmark.  It
scores one market at a time from completed OHLC bars and aligned funding
observations.  Universe discovery, data access, scheduling, persistence, and
order routing stay outside this pure module.

The scorer intentionally abstains.  Missing calibration, incomplete funding,
insufficient history, ambiguous forecasts, and unhealthy market/direction
lanes are observable non-signals rather than reasons to relax a gate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.quant.pattern_match import _normalized_shape, _path_features

PATTERN_WINDOWS = (12, 24, 48, 96)
FORECAST_HORIZONS = (2, 4, 8, 24, 48)
HISTORY_LIMIT = 10_000
RERANK_POOL = 256
TOP_MATCHES = 63
RANGE_HORIZON_BARS = 12
OUTCOME_HORIZON_BARS = 96
INTERVAL = pd.Timedelta(minutes=15)
MAX_FUNDING_AGE = pd.Timedelta(minutes=65)
FUNDING_SCALE = 0.00005
PREMIUM_SCALE = 0.0005


@dataclass(frozen=True)
class MarketCalibration:
    """Immutable development-fold thresholds for one canonical HL symbol."""

    timing: dict[int, float]
    vote: float
    steepness: float
    development_signals: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> MarketCalibration:
        return cls(
            timing={int(key): float(item) for key, item in value["timing"].items()},
            vote=float(value["vote"]),
            steepness=float(value["steepness"]),
            development_signals=int(value.get("development_signals") or 0),
        )


@dataclass(frozen=True)
class PatternMatcherConfig:
    pattern_windows: tuple[int, ...] = PATTERN_WINDOWS
    forecast_horizons: tuple[int, ...] = FORECAST_HORIZONS
    history_limit: int = HISTORY_LIMIT
    rerank_pool: int = RERANK_POOL
    top_matches: int = TOP_MATCHES
    range_horizon_bars: int = RANGE_HORIZON_BARS
    clarity_margin: float = 0.03
    minimum_horizon_agreement: float = 0.80
    funding_weight: float = 0.10
    minimum_history_bars: int = HISTORY_LIMIT


@dataclass(frozen=True)
class WindowForecast:
    pattern_bars: int
    probability_up: float
    horizon_agreement: float
    mean_similarity: float
    similarity_margin_product: float
    match_count: int
    predicted_range: float
    endpoint_q50: float


@dataclass(frozen=True)
class PatternDecision:
    symbol: str
    query_time: str | None
    actionable: bool
    reason: str
    direction: int | None = None
    stop_distance: float | None = None
    take_distance: float | None = None
    directional_vote: float | None = None
    normalized_steepness: float | None = None
    forecast: WindowForecast | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.forecast is not None:
            payload["forecast"] = asdict(self.forecast)
        return payload


@dataclass(frozen=True)
class LaneGate:
    allowed: bool
    reason: str
    resolved_count: int
    recent_count: int
    recent_mean_bps: float | None


def score_latest_pattern(
    symbol: str,
    frame: pd.DataFrame,
    calibration: MarketCalibration | Mapping[str, Any] | None,
    *,
    config: PatternMatcherConfig = PatternMatcherConfig(),
) -> PatternDecision:
    """Score the latest completed bar with the frozen analogue policy.

    ``frame`` must contain timestamp/open/high/low/close plus funding_rate and
    premium columns aligned as-of each completed bar, along with the source
    observation timestamp. Funding is required: silently replacing the
    validated funding rerank with a price-only or stale model would create a
    different strategy.
    """

    if calibration is None:
        return PatternDecision(symbol, _last_timestamp(frame), False, "uncalibrated")
    market = (
        calibration
        if isinstance(calibration, MarketCalibration)
        else MarketCalibration.from_mapping(calibration)
    )
    try:
        data = _validated_frame(frame)
    except ValueError as exc:
        return PatternDecision(symbol, _last_timestamp(frame), False, str(exc))
    query_time = pd.Timestamp(data["timestamp"].iloc[-1]).isoformat()
    if len(data) < config.minimum_history_bars:
        return PatternDecision(symbol, query_time, False, "insufficient_history")
    if 96 not in market.timing:
        return PatternDecision(symbol, query_time, False, "no_96_bar_calibration")

    forecasts: list[WindowForecast] = []
    for window in config.pattern_windows:
        threshold = market.timing.get(window)
        if threshold is None:
            continue
        forecast = _score_window(data, window, config)
        if forecast is None:
            continue
        margin = abs(forecast.probability_up - 0.5)
        if (
            margin >= config.clarity_margin
            and forecast.horizon_agreement >= config.minimum_horizon_agreement
            and forecast.similarity_margin_product >= threshold
        ):
            forecasts.append(forecast)
    if not forecasts:
        return PatternDecision(symbol, query_time, False, "no_clear_timing_signal")

    winner = min(
        forecasts,
        key=lambda item: (-item.similarity_margin_product, item.pattern_bars),
    )
    if winner.pattern_bars != 96:
        return PatternDecision(
            symbol, query_time, False, "winning_lookback_not_96", forecast=winner
        )
    direction = 1 if winner.probability_up > 0.5 else -1
    directional_vote = (
        winner.probability_up if direction == 1 else 1.0 - winner.probability_up
    )
    normalized_steepness = direction * winner.endpoint_q50 / winner.predicted_range
    if directional_vote < market.vote:
        return PatternDecision(
            symbol,
            query_time,
            False,
            "below_vote_threshold",
            direction=direction,
            directional_vote=directional_vote,
            normalized_steepness=normalized_steepness,
            forecast=winner,
        )
    if normalized_steepness < market.steepness:
        return PatternDecision(
            symbol,
            query_time,
            False,
            "below_steepness_threshold",
            direction=direction,
            directional_vote=directional_vote,
            normalized_steepness=normalized_steepness,
            forecast=winner,
        )
    bracket_distance = 0.5 * winner.predicted_range
    return PatternDecision(
        symbol,
        query_time,
        True,
        "high_confidence_candidate",
        direction=direction,
        stop_distance=bracket_distance,
        take_distance=bracket_distance,
        directional_vote=directional_vote,
        normalized_steepness=normalized_steepness,
        forecast=winner,
    )


def evaluate_lane_feedback(
    resolved: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    direction: int,
    as_of: str | pd.Timestamp,
    minimum_resolved: int = 50,
    recent_window: int = 10,
    minimum_recent_mean_bps: float = 0.0,
    seed_resolved_count: int = 0,
    seed_recent_returns: Sequence[float] = (),
) -> LaneGate:
    """Evaluate the frozen causal market x direction feedback gate."""

    cutoff = _utc_timestamp(as_of)
    eligible: list[tuple[pd.Timestamp, float]] = []
    for item in resolved:
        if str(item.get("symbol")) != symbol:
            continue
        try:
            item_direction = int(item.get("direction"))
            exit_time = _utc_timestamp(item.get("exit_time"))
            net_return = float(item.get("net_return"))
        except (TypeError, ValueError):
            continue
        if (
            item_direction == direction
            and exit_time <= cutoff
            and np.isfinite(net_return)
        ):
            eligible.append((exit_time, net_return))
    eligible.sort(key=lambda item: item[0])
    seed_values: list[float] = []
    for value in seed_recent_returns:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(parsed):
            seed_values.append(parsed)
    recent_values = [*seed_values, *(item[1] for item in eligible)][-recent_window:]
    resolved_count = max(int(seed_resolved_count), len(seed_values)) + len(eligible)
    recent_mean = float(np.mean(recent_values) * 10_000) if recent_values else None
    if resolved_count < minimum_resolved:
        return LaneGate(
            False,
            "insufficient_resolved_lane_history",
            resolved_count,
            len(recent_values),
            recent_mean,
        )
    if len(recent_values) < recent_window:
        return LaneGate(
            False,
            "incomplete_recent_lane_window",
            resolved_count,
            len(recent_values),
            recent_mean,
        )
    if recent_mean is None or recent_mean <= minimum_recent_mean_bps:
        return LaneGate(
            False,
            "recent_lane_expectancy_not_positive",
            resolved_count,
            len(recent_values),
            recent_mean,
        )
    return LaneGate(
        True,
        "lane_feedback_passed",
        resolved_count,
        len(recent_values),
        recent_mean,
    )


def _score_window(
    frame: pd.DataFrame, window: int, config: PatternMatcherConfig
) -> WindowForecast | None:
    closes = frame["close"].to_numpy(dtype=np.float64)
    opens = frame["open"].to_numpy(dtype=np.float64)
    highs = frame["high"].to_numpy(dtype=np.float64)
    lows = frame["low"].to_numpy(dtype=np.float64)
    funding = frame["funding_rate"].to_numpy(dtype=np.float64)
    premium = frame["premium"].to_numpy(dtype=np.float64)
    timestamps = pd.DatetimeIndex(frame["timestamp"])
    interval_ns = int(INTERVAL.value)
    timestamps_ns = timestamps.to_numpy(dtype="datetime64[ns]").astype(np.int64)

    query_end = len(frame) - 1
    query_start = query_end - window + 1
    if query_start < 0 or not _consecutive(
        timestamps_ns, query_start, query_end, interval_ns
    ):
        return None
    query_shape = _normalized_shape(closes[query_start : query_end + 1])
    if query_shape is None:
        return None
    query_range, query_volatility = _path_features(closes[query_start : query_end + 1])

    minimum_start = max(0, query_end - config.history_limit + 1)
    latest_for_outcomes = query_end - max(config.forecast_horizons)
    latest_before_query = query_start - 1
    maximum_end = min(latest_for_outcomes, latest_before_query)
    maximum_start = maximum_end - window + 1
    if maximum_start < minimum_start:
        return None
    starts = np.arange(minimum_start, maximum_start + 1, dtype=np.int64)
    ends = starts + window - 1
    bad_intervals = (np.diff(timestamps_ns) != interval_ns).astype(np.int8)
    bad_prefix = np.concatenate(([0], np.cumsum(bad_intervals, dtype=np.int64)))
    outcome_ends = ends + max(config.forecast_horizons)
    consecutive = (bad_prefix[outcome_ends] - bad_prefix[starts]) == 0
    starts, ends = starts[consecutive], ends[consecutive]
    if not len(starts):
        return None

    log_closes = np.log(closes)
    all_windows = np.lib.stride_tricks.sliding_window_view(log_closes, window)
    values = all_windows[starts]
    means = values.mean(axis=1)
    deviations = values - means[:, None]
    stds = values.std(axis=1)
    valid = np.isfinite(stds) & (stds > 1e-12)
    starts, ends, deviations, stds = (
        starts[valid],
        ends[valid],
        deviations[valid],
        stds[valid],
    )
    if not len(starts):
        return None
    normalized = deviations / stds[:, None]
    correlation = normalized @ query_shape / window
    distances = np.sqrt(np.maximum(0.0, 2.0 - 2.0 * correlation))
    shape_similarity = np.exp(-distances)
    path_ranges = values[valid].max(axis=1) - values[valid].min(axis=1)
    log_returns = np.diff(values[valid], axis=1)
    volatilities = np.sqrt(np.square(log_returns).mean(axis=1)) * np.sqrt(window - 1)
    magnitude_similarity = _ratio_similarities(path_ranges, query_range)
    volatility_similarity = _ratio_similarities(volatilities, query_volatility)
    price_scores = (
        0.65 * shape_similarity
        + 0.20 * magnitude_similarity
        + 0.15 * volatility_similarity
    )
    pool_positions = _select_non_overlapping(
        price_scores,
        distances,
        starts,
        separation=window,
        top=config.rerank_pool,
    )
    if len(pool_positions) < config.top_matches:
        return None
    pool_starts = starts[pool_positions]
    pool_ends = ends[pool_positions]
    pool_price = price_scores[pool_positions]
    query_signature = _funding_signature(funding, premium, query_start, query_end)
    if query_signature is None:
        return None
    candidate_signatures = np.asarray(
        [
            _funding_signature(funding, premium, int(start), int(end))
            for start, end in zip(pool_starts, pool_ends, strict=True)
        ],
        dtype=np.float64,
    )
    if candidate_signatures.ndim != 2:
        return None
    funding_similarity = np.exp(
        -np.sqrt(np.square(candidate_signatures - query_signature).mean(axis=1))
    )
    aggregate = (
        1.0 - config.funding_weight
    ) * pool_price + config.funding_weight * funding_similarity
    ranking = np.lexsort((pool_starts, distances[pool_positions], -aggregate))[
        : config.top_matches
    ]
    selected_ends = pool_ends[ranking]
    selected_similarity = aggregate[ranking]

    outcomes = np.column_stack(
        [
            closes[selected_ends + horizon] / closes[selected_ends] - 1.0
            for horizon in config.forecast_horizons
        ]
    )
    probabilities = (outcomes > 0).mean(axis=0)
    probability_up = float(probabilities.mean())
    agreement = float(max((probabilities > 0.5).mean(), (probabilities < 0.5).mean()))
    entry_indices = selected_ends + 1
    offsets = np.arange(config.range_horizon_bars)
    paths = entry_indices[:, None] + offsets[None, :]
    entries = opens[entry_indices]
    total_ranges = highs[paths].max(axis=1) / lows[paths].min(axis=1) - 1.0
    endpoints = closes[paths[:, -1]] / entries - 1.0
    mean_similarity = float(selected_similarity.mean())
    return WindowForecast(
        pattern_bars=window,
        probability_up=probability_up,
        horizon_agreement=agreement,
        mean_similarity=mean_similarity,
        similarity_margin_product=mean_similarity * abs(probability_up - 0.5),
        match_count=len(ranking),
        predicted_range=float(np.quantile(total_ranges, 0.50)),
        endpoint_q50=float(np.quantile(endpoints, 0.50)),
    )


def _validated_frame(frame: pd.DataFrame) -> pd.DataFrame:
    required = [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "funding_rate",
        "premium",
        "funding_observed_at",
    ]
    missing = set(required) - set(frame.columns)
    if missing:
        raise ValueError(f"missing_columns:{','.join(sorted(missing))}")
    data = frame[required].copy()
    for column in ("timestamp", "funding_observed_at"):
        data[column] = pd.to_datetime(data[column], utc=True, errors="coerce")
    if data[["timestamp", "funding_observed_at"]].isna().any().any():
        raise ValueError("invalid_timestamp")
    if data["timestamp"].duplicated().any():
        raise ValueError("duplicate_timestamps")
    if not data["timestamp"].is_monotonic_increasing:
        raise ValueError("timestamps_not_increasing")
    for column in required[1:-1]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    prices = data[["open", "high", "low", "close"]].to_numpy(dtype=float)
    if np.any(~np.isfinite(prices)) or np.any(prices <= 0):
        raise ValueError("invalid_ohlc")
    if np.any(data["high"] < data[["open", "close"]].max(axis=1)) or np.any(
        data["low"] > data[["open", "close"]].min(axis=1)
    ):
        raise ValueError("invalid_ohlc_bounds")
    funding = data[["funding_rate", "premium"]].to_numpy(dtype=float)
    if np.any(~np.isfinite(funding)):
        raise ValueError("incomplete_funding")
    funding_age = data["timestamp"] - data["funding_observed_at"]
    if (funding_age < pd.Timedelta(0)).any() or (funding_age > MAX_FUNDING_AGE).any():
        raise ValueError("stale_funding")
    return data.reset_index(drop=True)


def _funding_signature(
    rates: np.ndarray, premiums: np.ndarray, start: int, end: int
) -> np.ndarray | None:
    rate = rates[start : end + 1]
    premium = premiums[start : end + 1]
    if not len(rate) or np.any(~np.isfinite(rate)) or np.any(~np.isfinite(premium)):
        return None
    return np.asarray(
        [
            np.tanh(rate[-1] / FUNDING_SCALE),
            np.tanh(rate.mean() / FUNDING_SCALE),
            np.tanh(premium[-1] / PREMIUM_SCALE),
            np.tanh(premium.mean() / PREMIUM_SCALE),
        ],
        dtype=np.float64,
    )


def _ratio_similarities(candidate: np.ndarray, query: float) -> np.ndarray:
    output = np.zeros(len(candidate), dtype=np.float64)
    if query <= 1e-12:
        output[candidate <= 1e-12] = 1.0
        return output
    mask = candidate > 1e-12
    output[mask] = np.exp(-np.abs(np.log(candidate[mask] / query)))
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


def _consecutive(
    timestamps_ns: np.ndarray, start: int, end: int, interval_ns: int
) -> bool:
    if start < 0 or end >= len(timestamps_ns) or end < start:
        return False
    return bool(np.all(np.diff(timestamps_ns[start : end + 1]) == interval_ns))


def _last_timestamp(frame: pd.DataFrame) -> str | None:
    if frame.empty or "timestamp" not in frame:
        return None
    try:
        return _utc_timestamp(frame["timestamp"].iloc[-1]).isoformat()
    except (TypeError, ValueError):
        return None


def _utc_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


__all__ = [
    "INTERVAL",
    "LaneGate",
    "MAX_FUNDING_AGE",
    "MarketCalibration",
    "PatternDecision",
    "PatternMatcherConfig",
    "WindowForecast",
    "evaluate_lane_feedback",
    "score_latest_pattern",
]
