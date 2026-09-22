"""Causal and economic invariants of the frozen positioning research policy."""

import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.quant import pattern_match_positioning as positioning


def positions() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "market": ["ETH"] * 3,
            "size": [1.0, 3.0, -4.0],
            "notional": [100.0, 300.0, 400.0],
            "leverage": [2.0, 4.0, 5.0],
            "address": ["not retained"] * 3,
        }
    )


def snapshots() -> pd.DataFrame:
    frames = []
    for i, time in enumerate(
        [
            "2026-05-01T00:05Z",
            "2026-05-02T00:05Z",
            "2026-05-03T00:05Z",
            "2026-05-05T00:05Z",
        ]
    ):
        source = positions()
        source["size"] *= i + 1
        source["notional"] *= i + 1
        frames.append(positioning.aggregate_positions(source, pd.Timestamp(time)))
    return pd.concat(frames, ignore_index=True)


def history() -> pd.DataFrame:
    times = pd.date_range("2026-02-01T00:15Z", periods=95, freq="D")
    frame = pd.DataFrame(
        [(time, f"coin{i}") for time in times for i in range(24)],
        columns=["query_time", "coin"],
    )
    frame["entry_time"] = frame.query_time + pd.Timedelta(minutes=15)
    frame["long_exit"] = frame.entry_time + pd.Timedelta(days=1)
    frame["short_exit"] = frame.long_exit
    frame["scale"] = 0.02
    frame["long_return"] = 0.01
    frame["short_return"] = -frame.long_return
    return frame


def test_position_features_are_per_side_and_do_not_retain_identities() -> None:
    source = positions()
    result = positioning.aggregate_positions(source, pd.Timestamp("2026-05-01T00:05Z"))
    row = result.iloc[0]
    assert row.coin == "ETH"
    assert row.position_count_imbalance == pytest.approx(1 / 3)
    assert row.position_concentration == pytest.approx((100**2 + 300**2) / 400**2 - 1)
    assert row.position_leverage == pytest.approx(np.tanh(np.log(3.5 / 5)))
    assert row.gross_units == 8
    assert "address" not in result
    pd.testing.assert_frame_equal(source, positions())


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("market", " "),
        ("market", None),
        ("size", 0),
        ("notional", -1),
        ("leverage", 0),
        ("leverage", np.inf),
    ],
)
def test_invalid_positions_fail_explicitly(column: str, value: object) -> None:
    source = positions()
    source.loc[0, column] = value
    with pytest.raises(ValueError, match="Invalid open-position"):
        positioning.aggregate_positions(source, pd.Timestamp("2026-05-01T00:05Z"))


def test_single_sided_and_empty_snapshots_are_not_fabricated_positions() -> None:
    time = pd.Timestamp("2026-05-01T00:05Z")
    one_sided = positioning.aggregate_positions(positions().iloc[:2], time)
    assert one_sided.position_count_imbalance.isna().all()
    assert one_sided.gross_units.isna().all()
    empty = positioning.aggregate_positions(positions().iloc[:0], time)
    assert empty.empty
    assert empty.columns.tolist() == one_sided.columns.tolist()


def test_growth_uses_units_not_mark_price_and_does_not_bridge_missing_days() -> None:
    state = positioning.position_state(snapshots().iloc[::-1])
    assert np.isnan(state.position_growth.iloc[0])
    assert state.position_growth.iloc[1] == pytest.approx(np.tanh(np.log(2)))
    assert state.position_growth.iloc[2] == pytest.approx(np.tanh(np.log(1.5)))
    assert np.isnan(state.position_growth.iloc[3])
    first = positioning.aggregate_positions(
        positions(), pd.Timestamp("2026-05-01T00:05Z")
    )
    source = positions()
    source.notional *= 2
    second = positioning.aggregate_positions(source, pd.Timestamp("2026-05-02T00:05Z"))
    assert (
        positioning.position_state(pd.concat([first, second])).position_growth.iloc[1]
        == 0
    )


def test_snapshots_are_delayed_exactly_one_day_and_expire() -> None:
    queries = pd.DataFrame(
        {
            "coin": "ETH",
            "query_time": pd.to_datetime(
                [
                    "2026-05-03T00:04:59.999Z",
                    "2026-05-03T00:05Z",
                    "2026-05-04T00:05Z",
                    "2026-05-05T06:06Z",
                    "2026-05-06T00:05Z",
                ],
                format="mixed",
            ),
        }
    )
    source = snapshots()
    result = positioning.join_positions(queries, source)
    assert np.isnan(result.position_growth.iloc[0])
    assert result.position_growth.iloc[1] == pytest.approx(np.tanh(np.log(2)))
    assert result.position_growth.iloc[2] == pytest.approx(np.tanh(np.log(1.5)))
    assert result[positioning.POSITION_COLUMNS].iloc[3].isna().all()
    assert np.isnan(result.position_growth.iloc[4])
    source.loc[source.observed_at.ge("2026-05-03T00:05Z"), "gross_units"] *= 10
    changed = positioning.join_positions(queries, source)
    pd.testing.assert_frame_equal(result.iloc[:2], changed.iloc[:2])
    missing = positioning.join_positions(queries, source.iloc[:0])
    assert missing[positioning.POSITION_COLUMNS].isna().all().all()


def test_duplicate_snapshots_are_rejected() -> None:
    source = snapshots()
    with pytest.raises(ValueError, match="Duplicate market snapshot"):
        positioning.position_state(pd.concat([source, source.iloc[:1]]))


def test_daily_partitions_are_disjoint_causal_and_timestamp_unit_independent() -> None:
    source = history()
    cutoff = pd.Timestamp("2026-05-02T00:00Z")
    source.loc[0, "long_exit"] = cutoff + pd.Timedelta(days=1)
    structure, estimation = positioning.partitions(source, cutoff)
    assert not set(structure.query_time).intersection(estimation.query_time)
    assert 0 not in structure.index and 0 not in estimation.index
    for part in (structure, estimation):
        assert positioning.sufficient_training_history(part)
        assert part.query_time.dt.hour.eq(0).all()
        assert part.query_time.dt.minute.eq(15).all()
        assert part.query_time.max() < cutoff - pd.Timedelta(days=1)
        assert part[["long_exit", "short_exit"]].max().max() <= cutoff
        assert not part.duplicated(["coin", "query_time"]).any()
    source.query_time = source.query_time.dt.as_unit("us")
    a, b = positioning.partitions(source, cutoff)
    assert a.index.tolist() == structure.index.tolist()
    assert b.index.tolist() == estimation.index.tolist()


def test_day_weights_and_payoff_clipping_do_not_modify_realized_results() -> None:
    frame = history().iloc[:27].copy()
    weights = positioning.day_weights(frame)
    assert weights[:24].sum() == pytest.approx(1)
    assert weights[24:].sum() == pytest.approx(1)
    frame.loc[0, ["long_return", "short_return"]] = [1, -1]
    np.testing.assert_array_equal(positioning.normalized_payoffs(frame)[0], [5, -5])
    assert frame.long_return.iloc[0] == 1


def test_leaf_weights_use_matching_examples_and_only_covered_trees() -> None:
    reference = np.array([[0, 0], [0, 1], [1, 1]])
    query = np.array([[0, 1], [2, 1], [2, 2]])
    weights, coverage = positioning.analogue_weights(
        reference, query, np.array([1.0, 1.0, 2.0])
    )
    np.testing.assert_allclose(weights[0], [1 / 4, 1 / 4 + 1 / 6, 1 / 3])
    np.testing.assert_allclose(weights[1], [0, 1 / 3, 2 / 3])
    np.testing.assert_array_equal(weights[2], [0, 0, 0])
    np.testing.assert_array_equal(coverage, [2, 1, 0])
    np.testing.assert_allclose(weights.sum(axis=1), [1, 1, 0])


def test_uncertainty_clusters_correlated_coins_by_day_even_when_shuffled() -> None:
    days = pd.Series(pd.date_range("2026-01-01", periods=3, freq="D"))
    payoffs = np.array([[0.0, 0.0], [1.0, -1.0], [2.0, -2.0]])
    result = positioning.forecast(np.full((1, 3), 1 / 3), payoffs, days)
    mean, se, effective = result
    np.testing.assert_allclose(mean, [[1, -1]])
    np.testing.assert_allclose(se, [[1 / np.sqrt(3), 1 / np.sqrt(3)]])
    np.testing.assert_allclose(effective, [3])
    repeated = positioning.forecast(
        np.full((1, 6), 1 / 6), np.tile(payoffs, (2, 1)), pd.concat([days, days])
    )
    for expected, actual in zip(result, repeated, strict=True):
        np.testing.assert_allclose(actual, expected)
    unsupported = positioning.forecast(np.zeros((1, 3)), payoffs, days)
    assert np.isinf(unsupported[1]).all()
    np.testing.assert_array_equal(unsupported[2], [0])


@pytest.mark.parametrize("direction", [-1, 1])
def test_scoring_preserves_frozen_net_edge_and_the_chart_analogue_weights(
    direction: int,
) -> None:
    as_of = pd.Timestamp("2026-05-02T00:00Z")
    _, estimation = positioning.partitions(history(), as_of)
    estimation[["long_return", "short_return"]] *= direction
    reference = np.zeros((len(estimation), 64), dtype=int)
    queries = np.zeros((2, 64), dtype=int)
    queries[1] = 1  # No corresponding estimation leaves.
    result, unsupported = positioning.score_positioning_analogues(
        estimation, reference, queries, np.array([0.02, 0.02]), as_of=as_of
    )
    assert result.direction == direction
    assert result.expected_net_return == pytest.approx(0.01 - 0.0009)
    assert result.standard_error == pytest.approx(0, abs=1e-14)
    assert result.covered_trees == 64
    assert result.effective_days == pytest.approx(
        estimation.query_time.dt.date.nunique()
    )
    assert result.analogue_weights.sum() == pytest.approx(1)
    assert len(result.analogue_weights) == len(estimation)
    assert unsupported.direction is None
    assert unsupported.reason == "insufficient_analogue_support"


def test_scoring_requires_edge_beyond_cost_uncertainty_and_five_basis_points() -> None:
    as_of = pd.Timestamp("2026-05-02T00:00Z")
    _, estimation = positioning.partitions(history(), as_of)
    estimation["long_return"] = 0.001399
    estimation["short_return"] = -estimation.long_return
    result = positioning.score_positioning_analogues(
        estimation,
        np.zeros((len(estimation), 64), dtype=int),
        np.zeros((1, 64), dtype=int),
        np.array([0.02]),
        as_of=as_of,
    )[0]
    assert result.direction is None
    assert result.reason == "insufficient_net_edge"


@pytest.mark.parametrize("covered_trees", [47, 48])
def test_scoring_requires_at_least_48_covered_trees(covered_trees: int) -> None:
    as_of = pd.Timestamp("2026-05-02T00:00Z")
    _, estimation = positioning.partitions(history(), as_of)
    query = np.ones((1, 64), dtype=int)
    query[:, :covered_trees] = 0
    result = positioning.score_positioning_analogues(
        estimation,
        np.zeros((len(estimation), 64), dtype=int),
        query,
        np.array([0.02]),
        as_of=as_of,
    )[0]
    assert result.covered_trees == covered_trees
    assert result.direction == (1 if covered_trees == 48 else None)


def test_many_matching_coins_cannot_replace_20_independent_days() -> None:
    as_of = pd.Timestamp("2026-05-02T00:00Z")
    _, estimation = positioning.partitions(history(), as_of)
    first_days = estimation.query_time.unique()[:19]
    references = np.ones((len(estimation), 64), dtype=int)
    references[estimation.query_time.isin(first_days)] = 0
    result = positioning.score_positioning_analogues(
        estimation,
        references,
        np.zeros((1, 64), dtype=int),
        np.array([0.02]),
        as_of=as_of,
    )[0]
    assert result.covered_trees == 64
    assert result.effective_days == pytest.approx(19)
    assert result.direction is None
    assert result.reason == "insufficient_analogue_support"


@pytest.mark.parametrize("invalid", ["future", "nan", "scale", "trees", "history"])
def test_scoring_rejects_invalid_or_unresolved_reference_data(invalid: str) -> None:
    as_of = pd.Timestamp("2026-05-02T00:00Z")
    _, estimation = positioning.partitions(history(), as_of)
    if invalid == "future":
        estimation.loc[estimation.index[0], "long_exit"] = as_of + pd.Timedelta(
            seconds=1
        )
    elif invalid == "nan":
        estimation.loc[estimation.index[0], "long_return"] = np.nan
    elif invalid == "history":
        estimation = estimation.iloc[:499]
    with pytest.raises(ValueError, match="Invalid or incomplete positioning"):
        positioning.score_positioning_analogues(
            estimation,
            np.zeros((len(estimation), 63 if invalid == "trees" else 64), dtype=int),
            np.zeros((1, 64), dtype=int),
            np.array([0 if invalid == "scale" else 0.02]),
            as_of=as_of,
        )
