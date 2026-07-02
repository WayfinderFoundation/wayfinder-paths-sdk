"""Indicator math ported line-for-line from the live SNX/IMX strategy scripts
(vault-backend ops_runs prod pull, 2026-06-29). Quirks are intentional and
must be preserved — the ports are parity-tested against verbatim copies:

- wilder_atr: tr[0] uses prev_close = closes[0] (so tr[0] = high-low), seed is
  the plain mean of the first `period` TRs (tr[0] included), then Wilder
  smoothing. Used by the SNX SuperTrend.
- simple_atr: tr[0] stays 0.0 (loop starts at 1), rolling-mean window. Used by
  the IMX ATR-target exit.
- supertrend: band ratcheting mutates upper/lower in place against the
  PREVIOUS iteration's already-ratcheted values; unset trend defaults bearish.
"""

from __future__ import annotations

from collections.abc import Sequence


def sma(values: Sequence[float], period: int) -> list[float]:
    """SMA of given period, aligned to the end of values (zeros before)."""
    n = len(values)
    out = [0.0] * n
    for i in range(period - 1, n):
        out[i] = sum(values[i - period + 1 : i + 1]) / period
    return out


def wilder_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 7,
) -> list[float]:
    """ATR with Wilder's smoothing (SNX script `atr`)."""
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


def simple_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[float]:
    """Rolling-mean true-range ATR (IMX ATR-target script `atr`)."""
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


def supertrend(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 7,
    mult: float = 2.5,
) -> list[int]:
    """SuperTrend: 1=bearish (short), -1=bullish (exit for shorts)."""
    n = len(closes)
    a = wilder_atr(highs, lows, closes, period)
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
