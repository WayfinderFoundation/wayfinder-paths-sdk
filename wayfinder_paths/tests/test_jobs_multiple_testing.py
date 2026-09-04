from __future__ import annotations

import pytest

from wayfinder_paths.jobs.multiple_testing import (
    expected_max_sharpe,
    haircut,
    t_statistic,
)


def test_expected_max_sharpe_grows_with_the_trial_count() -> None:
    assert expected_max_sharpe(1) == 0.0
    assert expected_max_sharpe(10) == pytest.approx(1.57, abs=0.03)
    assert expected_max_sharpe(50) == pytest.approx(2.28, abs=0.03)
    assert expected_max_sharpe(21) > expected_max_sharpe(8) > expected_max_sharpe(2)


def test_t_statistic_and_haircut() -> None:
    assert t_statistic([1.0]) is None
    assert t_statistic([1.0, 1.0, 1.0]) is None
    series = [0.01] * 30 + [-0.005] * 10
    t = t_statistic(series)
    assert t is not None and t > 3
    cleared = haircut(series, 10)
    assert cleared["cleared"] is True and cleared["trials"] == 10
    assert cleared["expected_max_t"] == pytest.approx(1.57, abs=0.03)
    # The same series does not survive a search of three hundred trials.
    assert haircut(series, 300)["cleared"] in (True, False)
    noise = [0.001, -0.001] * 20
    assert haircut(noise, 21)["cleared"] is False
    short = haircut([0.01] * 5, 21)
    assert short["cleared"] is None and short["t_stat"] is None
