import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.quant.pattern_match_outcomes import resolve_pattern_outcome
from wayfinder_paths.quant.pattern_match_universe import _select_non_overlapping


def resolve(
    *,
    count=4,
    direction=1,
    stop=0.1,
    take=0.1,
    missing_bar=False,
    missing_funding=False,
):
    entry = pd.Timestamp("2026-01-01T00:00Z")
    bars = pd.DataFrame(
        {
            "timestamp": pd.date_range(entry, periods=count, freq="15min"),
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
        }
    )
    if missing_bar:
        bars = bars.drop(index=1)
    funding = pd.DataFrame(
        {
            "timestamp": pd.date_range(entry, periods=25, freq="h"),
            "funding_rate": 0.0001,
        }
    )
    if missing_funding:
        funding = funding.drop(index=1)
    return resolve_pattern_outcome(
        bars,
        funding,
        entry_time=entry,
        as_of=entry + pd.Timedelta(minutes=15 * count),
        direction=direction,
        stop_distance=stop,
        take_distance=take,
        cost_bps=9.0,
    )


def test_stop_wins_ambiguous_bar():
    result = resolve(stop=0.005, take=0.005)
    assert result.status == "stop"
    assert result.gross_return == -0.005
    assert result.net_return == pytest.approx(-0.0059)
    assert result.exit_time == pd.Timestamp("2026-01-01T00:15Z")


@pytest.mark.parametrize("direction", [-1, 1])
def test_time_limit_and_funding_sign(direction):
    result = resolve(count=96, direction=direction)
    assert result.status == "null"
    assert result.funding_return == pytest.approx(-direction * 24 * 0.0001)
    assert result.net_return == pytest.approx(
        direction * 0.01 - direction * 0.0024 - 0.0009
    )


def test_missing_prices_and_funding_wait_for_retry():
    assert resolve(count=96, missing_bar=True).data_status == "missing_prices"
    result = resolve(count=96, missing_funding=True)
    assert result.status == "open"
    assert result.net_return is None
    assert result.data_status == "missing_funding"
    assert resolve(count=96).status == "null"


def test_unresolved_handoff_keeps_position_open():
    assert resolve(count=2).status == "open"
    assert resolve(count=96).status == "null"


def test_fractional_hour_funding_coverage_preserves_actual_payment_time():
    entry = pd.Timestamp("2026-01-01T00:45Z")
    bars = pd.DataFrame(
        {
            "timestamp": [entry],
            "open": [100],
            "high": [102],
            "low": [99],
            "close": [101],
        }
    )
    funding = pd.DataFrame(
        {
            "timestamp": pd.date_range(entry.floor("h"), periods=2, freq="h")
            + pd.Timedelta(milliseconds=5),
            "funding_rate": [0.0001, 0.0001],
        }
    )
    result = resolve_pattern_outcome(
        bars,
        funding,
        entry_time=entry,
        as_of=entry + pd.Timedelta(minutes=30),
        direction=1,
        stop_distance=0.005,
        take_distance=0.005,
        cost_bps=9,
    )
    assert result.status == "stop"
    # 01:00:00.005 proves the hour is present but occurs after the 01:00 exit.
    assert result.funding_return == 0
    missing = resolve_pattern_outcome(
        bars.iloc[:0],
        funding,
        entry_time=entry,
        as_of=entry + pd.Timedelta(minutes=30),
        direction=1,
        stop_distance=0.005,
        take_distance=0.005,
        cost_bps=9,
    )
    assert missing.data_status == "missing_prices"


@pytest.mark.parametrize("separation", [1, 12, 24, 48, 96])
def test_fast_selector_matches_original_greedy_order(separation):
    rng = np.random.default_rng(42)
    starts = np.sort(rng.choice(1200, 900, replace=False))
    scores, distances = rng.integers(0, 10, size=(2, 900))
    expected = []
    for index in np.lexsort((starts, distances, -scores)):
        if all(abs(starts[index] - starts[other]) >= separation for other in expected):
            expected.append(index)
        if len(expected) == 63:
            break
    np.testing.assert_array_equal(
        _select_non_overlapping(
            scores, distances, starts, separation=separation, top=63
        ),
        expected,
    )
