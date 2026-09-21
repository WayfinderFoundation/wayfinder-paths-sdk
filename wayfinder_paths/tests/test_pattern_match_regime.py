"""Causal benchmark risk guard, independent of the analogue scorer."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.quant.pattern_match_universe import (
    INTERVAL,
    evaluate_macro_regime,
)


def benchmark() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2026-09-01", periods=97, freq="15min", tz="UTC"
            ),
            "close": 100 * np.exp(np.arange(97) * 0.001),
        }
    )


def test_guard_is_symmetric_and_does_not_reverse_signal() -> None:
    frame = benchmark()
    cutoff = frame.timestamp.iloc[-1] + INTERVAL
    short = evaluate_macro_regime(frame, direction=-1, as_of=cutoff)
    assert not short.allowed
    assert short.reason == "macro_countertrend_move"
    assert short.normalized_move == pytest.approx(np.sqrt(96))
    assert short.log_return == pytest.approx(0.096)
    assert evaluate_macro_regime(frame, direction=1, as_of=cutoff).allowed

    frame.close = 10000 / frame.close
    long = evaluate_macro_regime(frame, direction=1, as_of=cutoff)
    assert not long.allowed
    assert long.normalized_move == pytest.approx(-short.normalized_move)
    assert evaluate_macro_regime(frame, direction=-1, as_of=cutoff).allowed


def test_future_and_in_progress_bars_cannot_change_gate() -> None:
    frame = benchmark()
    cutoff = frame.timestamp.iloc[-1] + INTERVAL
    expected = evaluate_macro_regime(frame, direction=-1, as_of=cutoff)
    future = pd.DataFrame(
        {"timestamp": [cutoff, cutoff + INTERVAL], "close": [1e-9, 1e9]}
    )
    assert (
        evaluate_macro_regime(
            pd.concat([frame, future], ignore_index=True), direction=-1, as_of=cutoff
        )
        == expected
    )


@pytest.mark.parametrize("defect", ["missing", "gap", "duplicate", "stale", "invalid"])
def test_incomplete_benchmark_fails_closed(defect: str) -> None:
    frame = benchmark()
    cutoff = frame.timestamp.iloc[-1] + INTERVAL
    if defect == "missing":
        frame = frame.drop(columns="close")
    elif defect == "gap":
        frame = frame.drop(index=20)
    elif defect == "duplicate":
        frame.loc[20, "timestamp"] = frame.timestamp.iloc[19]
    elif defect == "stale":
        cutoff += INTERVAL
    else:
        frame.loc[20, "close"] = np.nan
    gate = evaluate_macro_regime(frame, direction=-1, as_of=cutoff)
    assert not gate.allowed
    assert gate.normalized_move is None
    assert gate.reason != "macro_countertrend_move"


def test_flat_benchmark_and_exact_boundary_are_not_a_shock() -> None:
    frame = benchmark()
    cutoff = frame.timestamp.iloc[-1] + INTERVAL
    frame.close = 100.0
    assert evaluate_macro_regime(frame, direction=-1, as_of=cutoff).normalized_move == 0
    # One nonzero return: total return equals root-sum-square volatility.
    frame.loc[96, "close"] = 100 * np.exp(0.01)
    gate = evaluate_macro_regime(frame, direction=-1, as_of=cutoff)
    assert gate.normalized_move == pytest.approx(1.0)
    assert gate.allowed


def test_direction_must_be_explicit() -> None:
    with pytest.raises(ValueError, match="direction"):
        evaluate_macro_regime(benchmark(), direction=0, as_of="2026-09-02T00:15Z")


def test_september_benchmark_vectors_keep_calm_fade_and_reject_rally() -> None:
    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures/pattern_match_research/macro_btc.json"
        ).read_text()
    )
    for case, expected in zip(fixture["cases"], [True, False, False], strict=True):
        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range(
                    case["first_open"], periods=97, freq="15min"
                ),
                "close": case["closes"],
            }
        )
        gate = evaluate_macro_regime(frame, direction=-1, as_of=case["as_of"])
        assert gate.allowed is expected
