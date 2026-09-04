"""Selection-bias arithmetic for a search that tries many candidates.

A campaign that screens N candidates and keeps the best has, under the null
of no edge, an expected best t-statistic of about
``(1 - g) * Phi^-1(1 - 1/N) + g * Phi^-1(1 - 1/(N e))`` (Bailey and López de
Prado's expected maximum Sharpe ratio, g = Euler's constant). A result that
does not clear that bar is what the search would have produced from noise.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist

EULER_GAMMA = 0.5772156649015329
_NORMAL = NormalDist()


def expected_max_sharpe(trials: int) -> float:
    """Expected maximum of ``trials`` independent standard-normal t-statistics
    (0.0 for a single trial: one look is not a search)."""
    n = max(1, int(trials))
    if n == 1:
        return 0.0
    return (1.0 - EULER_GAMMA) * _NORMAL.inv_cdf(1.0 - 1.0 / n) + EULER_GAMMA * (
        _NORMAL.inv_cdf(1.0 - 1.0 / (n * math.e))
    )


def t_statistic(values: Sequence[float]) -> float | None:
    """Mean over its standard error; None below two observations or with a
    degenerate spread."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    variance = sum((value - mean) ** 2 for value in values) / (n - 1)
    if variance <= 0:
        return None
    return mean / math.sqrt(variance / n)


def haircut(
    values: Sequence[float], trials: int, *, min_observations: int = 20
) -> dict:
    """The trial-count haircut on one series: its t-statistic against the
    expected maximum of ``trials`` null trials. ``cleared`` is None when the
    series is too short to say anything."""
    t_stat = t_statistic(values) if len(values) >= min_observations else None
    expected = expected_max_sharpe(trials)
    return {
        "trials": max(1, int(trials)),
        "observations": len(values),
        "t_stat": round(t_stat, 4) if t_stat is not None else None,
        "expected_max_t": round(expected, 4),
        "cleared": (t_stat >= expected) if t_stat is not None else None,
    }
