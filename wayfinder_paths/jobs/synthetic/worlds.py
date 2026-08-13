"""Deterministic synthetic market worlds with planted, known structure.

Each generator returns 1h OHLCV rows for `PreparedExecutionDataset.from_rows`.
All randomness flows from an explicit seed — identical worlds every run, so
benchmark numbers are comparable across pipeline versions.
"""

from __future__ import annotations

import math
import random
from typing import Any

BAR_SECONDS = 3600
_EPOCH = "2026-01-01T00:00:00+00:00"


def _rows(closes: list[float], symbol: str) -> list[dict[str, Any]]:
    import datetime as dt

    start = dt.datetime.fromisoformat(_EPOCH)
    rows = []
    for index, close in enumerate(closes):
        stamp = (start + dt.timedelta(seconds=index * BAR_SECONDS)).isoformat()
        rows.append(
            {
                "timestamp": stamp,
                "symbol": symbol,
                "open": close * 0.9995,
                "high": close * 1.0015,
                "low": close * 0.9985,
                "close": close,
                "volume": 100,
            }
        )
    return rows


def reversion_world(
    *, bars: int = 2400, seed: int = 11, symbol: str = "SYN"
) -> list[dict[str, Any]]:
    """A TRUE edge and a FAKE one in the same series.

    True (whole series, broad plateau): after a >=1.2% drop over 3 bars, the
    next 2 bars drift +0.45% each — dip-buying works for a RANGE of
    thresholds, everywhere.
    Fake (development region only): at hour 17 in the first 60% of the
    series, periodic +1.2% pops — concentrated luck that VANISHES in the
    final 40%, exactly where the economic gate's OOS folds and audit slice
    live. A full-history grid ranks it top; the paired fold evaluation must
    refuse it.
    """
    rng = random.Random(seed)
    closes = [100.0]
    bounce_left = 0
    for index in range(1, bars):
        drift = 0.0
        if bounce_left > 0:
            drift += 0.0045
            bounce_left -= 1
        if index % 24 == 17 and index < bars * 0.6 and rng.random() < 0.34:
            drift += 0.012
        noise = rng.gauss(0.0, 0.004)
        closes.append(max(1.0, closes[-1] * (1.0 + drift + noise)))
        if len(closes) >= 4:
            drop = closes[-1] / closes[-4] - 1.0
            if drop <= -0.012 and bounce_left == 0:
                bounce_left = 2
    return _rows(closes, symbol)


def churn_world(
    *, bars: int = 1200, seed: int = 23, symbol: str = "SYN"
) -> list[dict[str, Any]]:
    """Pure noise: zero edge anywhere. Any strategy that trades pays fees for
    nothing — the gate must refuse gross-flat, net-negative churn."""
    rng = random.Random(seed)
    closes = [100.0]
    for _ in range(1, bars):
        closes.append(max(1.0, closes[-1] * (1.0 + rng.gauss(0.0, 0.004))))
    return _rows(closes, symbol)


def regime_world(
    *, bars: int = 2400, seed: int = 37, symbol: str = "SYN", shift_at: float = 0.6
) -> list[dict[str, Any]]:
    """A regime flip: steady uptrend (+0.08%/bar) becomes a downtrend
    (-0.10%/bar) at `shift_at`. The long incumbent goes stale exactly where
    the gate's recent folds look; a short candidate must clear the gate, and
    the incumbent's post-shift forward losses must trip typed kill rules."""
    rng = random.Random(seed)
    closes = [100.0]
    flip = int(bars * shift_at)
    for index in range(1, bars):
        drift = 0.0008 if index < flip else -0.0010
        closes.append(max(1.0, closes[-1] * (1.0 + drift + rng.gauss(0.0, 0.003))))
    return _rows(closes, symbol)


def sine_plateau_grid(center: float, spread: float, points: int = 5) -> list[float]:
    """Evenly spaced parameter values around a center — used to probe whether
    neighboring cells of a winning cell also work (plateau vs spike)."""
    step = spread / max(points - 1, 1)
    return [round(center - spread / 2 + index * step, 4) for index in range(points)]


def synthetic_growth(closes: list[float]) -> float:
    return math.log(closes[-1] / closes[0]) if closes else 0.0
