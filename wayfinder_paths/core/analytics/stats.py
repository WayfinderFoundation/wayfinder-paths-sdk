from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist


def z_from_conf(confidence: float) -> float:
    return NormalDist().inv_cdf((1 + float(confidence)) / 2)


def rolling_min_sum(arr: Sequence[float], window: int) -> float:
    values = list(arr)
    if window <= 0:
        return 0.0
    if len(values) < window:
        return float(sum(values))

    current_sum = sum(values[:window])
    min_sum = current_sum
    for i in range(window, len(values)):
        current_sum = current_sum - values[i - window] + values[i]
        min_sum = min(min_sum, current_sum)
    return float(min_sum)


def percentile(sorted_values: Sequence[float], pct: float) -> float:
    values = list(sorted_values)
    if not values:
        return float("nan")
    if len(values) == 1:
        return float(values[0])

    pct = min(max(float(pct), 0.0), 1.0)
    idx = (len(values) - 1) * pct
    lower = math.floor(idx)
    upper = math.ceil(idx)
    if lower == upper:
        return float(values[lower])
    weight = idx - lower
    return float(values[lower] + weight * (values[upper] - values[lower]))
