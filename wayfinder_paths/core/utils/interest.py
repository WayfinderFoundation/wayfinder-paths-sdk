from __future__ import annotations

from wayfinder_paths.core.constants.base import SECONDS_PER_YEAR

RAY = 10**27


def ray_to_apr(ray: int) -> float:
    if not ray:
        return 0.0
    return float(ray) / RAY


def apr_to_apy(apr: float) -> float:
    return (1 + float(apr) / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR - 1
