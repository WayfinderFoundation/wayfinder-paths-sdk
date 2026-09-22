import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.quant.pattern_match_positioning_projection import (
    positioning_projection_series,
)


def inputs() -> tuple[pd.DataFrame, np.ndarray, dict[str, pd.DataFrame]]:
    query = pd.Timestamp("2026-09-01T00:15Z")
    times = pd.date_range(query, periods=97, freq="15min")
    reference = pd.DataFrame({"coin": ["A", "B", "C"], "query_time": query})
    histories = {
        coin: pd.DataFrame(
            {"timestamp": times, "close": 100 + np.linspace(0, change, 97)}
        )
        for coin, change in (("A", -20), ("B", 10), ("C", 30))
    }
    return reference, np.array([0.1, 0.7, 0.2]), histories


def test_projection_uses_actual_model_mass_and_is_anchored_at_zero() -> None:
    result = positioning_projection_series(
        *inputs(), as_of=pd.Timestamp("2026-09-03T00:00Z")
    )
    assert result["sample_count"] == 3
    for key in ("median_bps", "q25_bps", "q75_bps"):
        assert len(result[key]) == 97
        assert result[key][0] == 0
        assert result[key][-1] == pytest.approx(1000)
    assert result["hit_rate_up"][-1] == pytest.approx(0.9)
    assert "hedge excluded" in result["label"]
    assert result["analogues"] == []


def test_projection_ignores_zero_weight_rows_but_never_drops_a_weighted_missing_path() -> (
    None
):
    reference, _, histories = inputs()
    del histories["A"]
    result = positioning_projection_series(
        reference,
        np.array([0, 0.8, 0.2]),
        histories,
        as_of=pd.Timestamp("2026-09-03T00:00Z"),
    )
    assert result["sample_count"] == 2
    with pytest.raises(ValueError, match="Missing or future"):
        positioning_projection_series(
            reference,
            np.array([0.1, 0.7, 0.2]),
            histories,
            as_of=pd.Timestamp("2026-09-03T00:00Z"),
        )


@pytest.mark.parametrize("invalid", ["future", "gap", "negative_weight", "zero_weight"])
def test_invalid_projections_fail_closed(invalid: str) -> None:
    reference, weights, histories = inputs()
    as_of = pd.Timestamp("2026-09-03T00:00Z")
    if invalid == "future":
        as_of = pd.Timestamp("2026-09-01T23:00Z")
    elif invalid == "gap":
        histories["B"] = histories["B"].drop(index=3)
    elif invalid == "negative_weight":
        weights[0] = -0.1
    else:
        weights[:] = 0
    with pytest.raises(ValueError):
        positioning_projection_series(reference, weights, histories, as_of=as_of)
