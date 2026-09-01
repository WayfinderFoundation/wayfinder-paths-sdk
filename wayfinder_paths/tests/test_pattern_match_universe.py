"""Pure scorer and causal lane-feedback tests."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.quant.pattern_match_universe import (
    MarketCalibration,
    PatternMatcherConfig,
    evaluate_lane_feedback,
    score_latest_pattern,
)


def _frame(count: int = 900) -> pd.DataFrame:
    index = np.arange(count)
    close = 100 * np.exp(0.0004 * index + 0.015 * np.sin(2 * np.pi * index / 120))
    open_ = close * np.exp(-0.0002)
    timestamps = pd.date_range("2026-01-01", periods=count, freq="15min", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": open_,
            "high": np.maximum(open_, close) * 1.002,
            "low": np.minimum(open_, close) * 0.998,
            "close": close,
            "funding_rate": 0.0,
            "premium": 0.0,
            "funding_observed_at": timestamps.floor("h"),
        }
    )


def _config() -> PatternMatcherConfig:
    return PatternMatcherConfig(
        pattern_windows=(96,),
        history_limit=800,
        rerank_pool=8,
        top_matches=3,
        minimum_history_bars=300,
    )


def test_latest_scorer_is_deterministic_and_emits_range_bracket() -> None:
    calibration = MarketCalibration(
        timing={96: 0.0}, vote=0.5, steepness=-10.0, development_signals=100
    )
    first = score_latest_pattern("TEST", _frame(), calibration, config=_config())
    second = score_latest_pattern("TEST", _frame(), calibration, config=_config())

    assert first == second
    assert first.actionable
    assert first.direction == -1
    assert first.directional_vote == pytest.approx(0.8)
    assert first.stop_distance == pytest.approx(first.take_distance)
    assert first.forecast is not None
    assert first.forecast.match_count == 3
    assert first.forecast.predicted_range > 0


def test_scorer_abstains_instead_of_changing_validated_feature_contract() -> None:
    calibration = MarketCalibration(
        timing={96: 0.0}, vote=0.5, steepness=-10.0, development_signals=100
    )
    missing_funding = _frame().drop(columns="premium")
    result = score_latest_pattern(
        "TEST", missing_funding, calibration, config=_config()
    )
    assert not result.actionable
    assert result.reason == "missing_columns:premium"

    uncalibrated = score_latest_pattern("NEW", _frame(), None, config=_config())
    assert not uncalibrated.actionable
    assert uncalibrated.reason == "uncalibrated"

    duplicate = pd.concat([_frame(), _frame().tail(1)], ignore_index=True)
    result = score_latest_pattern("TEST", duplicate, calibration, config=_config())
    assert not result.actionable
    assert result.reason == "duplicate_timestamps"

    stale = _frame()
    stale["funding_observed_at"] = stale["timestamp"] - pd.Timedelta(hours=2)
    result = score_latest_pattern("TEST", stale, calibration, config=_config())
    assert not result.actionable
    assert result.reason == "stale_funding"


def test_lane_feedback_uses_only_resolved_same_direction_outcomes() -> None:
    resolved = [
        {
            "symbol": "BTC",
            "direction": -1,
            "exit_time": f"2026-01-{day:02d}T00:00:00Z",
            "net_return": 0.001,
        }
        for day in range(1, 11)
    ]
    resolved.extend(
        [
            {
                "symbol": "BTC",
                "direction": -1,
                "exit_time": "2026-02-02T00:00:00Z",  # future: cannot leak
                "net_return": -1.0,
            },
            {
                "symbol": "BTC",
                "direction": 1,
                "exit_time": "2026-01-05T00:00:00Z",  # wrong lane
                "net_return": -1.0,
            },
        ]
    )
    gate = evaluate_lane_feedback(
        resolved,
        symbol="BTC",
        direction=-1,
        as_of="2026-01-31T00:00:00Z",
        minimum_resolved=50,
        recent_window=10,
        seed_resolved_count=40,
        seed_recent_returns=[0.001] * 10,
    )
    assert gate.allowed
    assert gate.resolved_count == 50
    assert gate.recent_count == 10
    assert gate.recent_mean_bps == pytest.approx(10.0)


def test_lane_feedback_fails_closed_at_zero_recent_expectancy() -> None:
    gate = evaluate_lane_feedback(
        [],
        symbol="BTC",
        direction=-1,
        as_of="2026-01-31T00:00:00Z",
        seed_resolved_count=50,
        seed_recent_returns=[0.001, -0.001] * 5,
    )
    assert not gate.allowed
    assert gate.reason == "recent_lane_expectancy_not_positive"


def test_lane_feedback_ignores_malformed_seed_values() -> None:
    gate = evaluate_lane_feedback(
        [],
        symbol="BTC",
        direction=1,
        as_of="2026-01-31T00:00:00Z",
        seed_resolved_count=50,
        seed_recent_returns=[0.001, None, "bad"],
    )
    assert not gate.allowed
    assert gate.reason == "incomplete_recent_lane_window"
    assert gate.recent_count == 1
