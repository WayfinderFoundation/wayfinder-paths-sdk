import json
from pathlib import Path

import pandas as pd
import pytest

from wayfinder_paths.quant.pattern_match_universe import (
    evaluate_lane_feedback,
    load_calibration_bundle,
    score_latest_pattern,
)

FIXTURES = Path(__file__).parent / "fixtures/pattern_match_research"


def test_frozen_feedback_matches_all_139_trade_identities():
    rows = pd.read_csv(
        FIXTURES / "feedback.csv.gz", parse_dates=["query_time", "exit_time"]
    )
    observed = []
    for row in rows[rows.fold.isin([3, 4, 5])].itertuples():
        lane = rows[(rows.coin == row.coin) & (rows.direction == row.direction)]
        gate = evaluate_lane_feedback(
            [
                {
                    "symbol": r.coin,
                    "direction": r.direction,
                    "exit_time": r.exit_time,
                    "net_return": r.current_config_net_return,
                }
                for r in lane.itertuples()
            ],
            symbol=row.coin,
            direction=row.direction,
            as_of=row.query_time,
        )
        if gate.allowed:
            observed.append(row.signal_id)
    assert sorted(observed) == sorted(rows[rows.selected].signal_id)
    returns = rows[rows.signal_id.isin(observed)].current_config_net_return
    assert len(returns) == 139
    assert (returns > 0).mean() == pytest.approx(87 / 139)
    assert returns.mean() * 10_000 == pytest.approx(7.331791924341332)
    assert returns[returns > 0].sum() / -returns[returns < 0].sum() == pytest.approx(
        1.1859169745377034
    )


def test_raw_ohlc_and_funding_reproduce_research_forecasts():
    frame = pd.read_csv(
        FIXTURES / "btc.csv.gz", parse_dates=["timestamp", "funding_observed_at"]
    )
    frame["funding_observed_at"] = pd.to_datetime(
        frame.funding_observed_at, utc=True, format="mixed"
    )
    expected = pd.read_csv(FIXTURES / "btc_expected.csv", parse_dates=["query_time"])
    for row in expected.itertuples():
        decision = score_latest_pattern(
            "BTC",
            frame[frame.timestamp <= row.query_time].tail(10000),
            load_calibration_bundle()["markets"]["BTC"],
        )
        assert decision.actionable
        assert decision.direction == row.direction
        assert decision.directional_vote == pytest.approx(
            row.cross_horizon_directional_vote
        )
        assert decision.forecast.predicted_range == pytest.approx(row.range_q50)
        assert decision.normalized_steepness == pytest.approx(row.normalized_steepness)


def test_calibration_metadata_records_actual_training_overlap():
    bundle = load_calibration_bundle()
    assert "lane_seeds" not in bundle  # The backend must bootstrap its own cadence.
    assert bundle["calibration_window"]["folds"] == [1, 2, 3]
    assert bundle["calibration_window"]["end"] == "2026-05-02T08:45:00+00:00"
    assert (
        json.loads((FIXTURES / "manifest.json").read_text())["expected_trades"] == 139
    )
