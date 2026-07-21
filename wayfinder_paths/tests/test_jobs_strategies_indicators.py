"""Parity tests: the ported indicator math must equal the live scripts'
functions exactly (quirks included). The script functions are embedded
verbatim below (from vault-backend ops_runs prod pull 2026-06-29)."""

from __future__ import annotations

import random

import pytest

from wayfinder_paths.jobs.strategies.indicators import (
    simple_atr,
    sma,
    supertrend,
    wilder_atr,
)


# ── verbatim copies from snx_momentum_strategy.py ────────────────────────────
def _script_sma(values: list[float], period: int) -> list[float]:
    n = len(values)
    out = [0.0] * n
    for i in range(period - 1, n):
        out[i] = sum(values[i - period + 1 : i + 1]) / period
    return out


def _script_wilder_atr(highs, lows, closes, period: int = 7) -> list[float]:
    n = len(closes)
    tr = [0.0] * n
    for i in range(n):
        prev_close = closes[i - 1] if i > 0 else closes[0]
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        )
    out = [0.0] * n
    out[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def _script_supertrend(highs, lows, closes, period: int = 7, mult: float = 2.5):
    n = len(closes)
    a = _script_wilder_atr(highs, lows, closes, period)
    hl2 = [(highs[i] + lows[i]) / 2.0 for i in range(n)]
    upper = [hl2[i] + mult * a[i] for i in range(n)]
    lower = [hl2[i] - mult * a[i] for i in range(n)]
    trend = [0] * n
    for i in range(1, n):
        if closes[i] > upper[i - 1]:
            trend[i] = -1
        elif closes[i] < lower[i - 1]:
            trend[i] = 1
        else:
            trend[i] = trend[i - 1] if trend[i - 1] != 0 else 1
        if trend[i] == 1:
            lower[i] = max(lower[i], lower[i - 1]) if i > 0 else lower[i]
        elif trend[i] == -1:
            upper[i] = min(upper[i], upper[i - 1]) if i > 0 else upper[i]
    return trend


# ── verbatim copy from imx_atr_target_strategy.py ────────────────────────────
def _script_simple_atr(highs, lows, closes, period: int = 14) -> list[float]:
    n = len(closes)
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    out = [0.0] * n
    for i in range(period - 1, n):
        out[i] = sum(tr[i - period + 1 : i + 1]) / period
    return out


def _random_walk(seed: int, count: int = 120) -> tuple[list, list, list]:
    rng = random.Random(seed)
    closes, highs, lows = [], [], []
    price = 10.0
    for _ in range(count):
        price = max(0.5, price * (1 + rng.uniform(-0.05, 0.05)))
        high = price * (1 + rng.uniform(0, 0.03))
        low = price * (1 - rng.uniform(0, 0.03))
        closes.append(price)
        highs.append(high)
        lows.append(low)
    return highs, lows, closes


def test_sma_matches_hand_computed() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    out = sma(values, 3)
    assert out[:2] == [0.0, 0.0]
    assert out[2] == pytest.approx(2.0)
    assert out[5] == pytest.approx(5.0)
    assert out == _script_sma(values, 3)


def test_wilder_atr_seed_and_recurrence() -> None:
    highs = [11.0, 12.0, 13.0, 12.5]
    lows = [9.0, 10.0, 11.0, 11.5]
    closes = [10.0, 11.0, 12.0, 12.0]
    out = wilder_atr(highs, lows, closes, period=2)
    # tr[0] quirk: prev_close = closes[0] -> tr[0] = high-low = 2.0
    # tr[1] = max(2.0, |12-10|, |10-10|) = 2.0 ; seed = mean(2.0, 2.0) = 2.0
    assert out[1] == pytest.approx(2.0)
    # tr[2] = max(2.0, |13-11|, |11-11|) = 2.0 -> (2.0*1 + 2.0)/2 = 2.0
    assert out[2] == pytest.approx(2.0)
    # tr[3] = max(1.0, |12.5-12|, |11.5-12|) = 1.0 -> (2.0 + 1.0)/2 = 1.5
    assert out[3] == pytest.approx(1.5)


def test_simple_atr_tr0_quirk() -> None:
    highs = [11.0, 12.0, 13.0]
    lows = [9.0, 10.0, 11.0]
    closes = [10.0, 11.0, 12.0]
    out = simple_atr(highs, lows, closes, period=2)
    # tr[0] stays 0.0 by construction; window mean includes it
    assert out[1] == pytest.approx((0.0 + 2.0) / 2)
    assert out[2] == pytest.approx((2.0 + 2.0) / 2)


def test_supertrend_flips_and_defaults_bearish() -> None:
    # steady series -> unset trend defaults to 1 (bearish)
    flat_h = [10.5] * 20
    flat_l = [9.5] * 20
    flat_c = [10.0] * 20
    assert supertrend(flat_h, flat_l, flat_c)[-1] == 1

    # a single huge close punches above the ratcheted upper band -> bullish
    highs = [10.5] * 19 + [21.0]
    lows = [9.5] * 19 + [19.0]
    closes = [10.0] * 19 + [20.0]
    assert supertrend(highs, lows, closes)[-1] == -1


@pytest.mark.parametrize("seed", [1, 7, 42, 1337])
def test_ports_match_script_functions_on_random_walks(seed: int) -> None:
    highs, lows, closes = _random_walk(seed)
    assert sma(closes, 20) == _script_sma(closes, 20)
    assert wilder_atr(highs, lows, closes, 7) == _script_wilder_atr(
        highs, lows, closes, 7
    )
    assert simple_atr(highs, lows, closes, 14) == _script_simple_atr(
        highs, lows, closes, 14
    )
    assert supertrend(highs, lows, closes, 7, 2.5) == _script_supertrend(
        highs, lows, closes, 7, 2.5
    )
