"""Canonical entry-trigger library for `signal_scan` — the breadth tool.

Live sessions showed the failure mode this module removes: an agent
hand-rewriting `precompute()` nine times to test nine trigger ideas, each
rewrite a fresh chance for an implementation bug, each result read in
isolation with no multiple-testing discipline. The library computes the
standard trigger families ONCE, correctly and causally, and `signal_scan`
event-studies all of them in a single call.

Every builder is an EVENT column: boolean, row-aligned with the input bars,
True on bars where the trigger fires. Directional variants are separate
events (a fresh 5-bar low is not the mirror image of a fresh 5-bar high), and
the scan classifies edge direction from the SIGN of the t-stat, so a trigger
that "fails" as a short entry but predicts bounces surfaces as a long
candidate instead of a dead end.

Causality contract: builders may only use rolling/shift/ewm transforms over
the current and past rows — the prefix property (appending future bars never
changes past values) is pinned by tests.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SignalDef:
    name: str
    family: str
    description: str
    min_bars: int
    build: Callable[[pd.DataFrame], pd.Series]


def _wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return 100 - 100 / (1 + rs)


def wilder_atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    prev_close = frame["close"].astype(float).shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


_atr = wilder_atr


def _close(frame: pd.DataFrame) -> pd.Series:
    return frame["close"].astype(float)


def _new_extreme(frame: pd.DataFrame, period: int, direction: int) -> pd.Series:
    close = _close(frame)
    prior = close.shift(1).rolling(period)
    if direction < 0:
        return close < prior.min()
    return close > prior.max()


def _fresh(event: pd.Series) -> pd.Series:
    return event & ~event.shift(1, fill_value=False).astype(bool)


def _ema_slope_dn(close: pd.Series, span: int = 50, lag: int = 10) -> pd.Series:
    ema = close.ewm(span=span, adjust=False).mean()
    return ema < ema.shift(lag)


def _cross(fast: pd.Series, slow: pd.Series, direction: int) -> pd.Series:
    if direction < 0:
        return (fast < slow) & (fast.shift(1) >= slow.shift(1))
    return (fast > slow) & (fast.shift(1) <= slow.shift(1))


def _bb_z(close: pd.Series, period: int = 20) -> pd.Series:
    mean = close.rolling(period).mean()
    std = close.rolling(period).std()
    return (close - mean) / std.replace(0.0, np.nan)


def _wide_range(frame: pd.DataFrame, direction: int) -> pd.Series:
    close = _close(frame)
    tr = (frame["high"].astype(float) - frame["low"].astype(float)).abs()
    wide = tr > 2 * _atr(frame).shift(1)
    if direction < 0:
        return wide & (close < frame["open"].astype(float))
    return wide & (close > frame["open"].astype(float))


def _vol_surge(frame: pd.DataFrame, direction: int) -> pd.Series:
    close = _close(frame)
    volume = frame["volume"].astype(float)
    surge = volume > 2 * volume.shift(1).rolling(20).mean()
    if direction < 0:
        return surge & (close < close.shift(1))
    return surge & (close > close.shift(1))


def _compression_break(frame: pd.DataFrame, direction: int) -> pd.Series:
    close = _close(frame)
    window_range = close.rolling(20).max() - close.rolling(20).min()
    compressed = window_range.shift(1) < window_range.shift(1).rolling(100).quantile(
        0.33
    )
    return compressed & _new_extreme(frame, 20, direction)


def _extended(frame: pd.DataFrame, direction: int) -> pd.Series:
    """Deeply extended multi-day move making a fresh 12-bar extreme — the
    exhaustion event: scans often show it REVERSES rather than continues."""
    close = _close(frame)
    if direction < 0:
        staircase = (close < close.shift(24)) & (close.shift(24) < close.shift(72))
    else:
        staircase = (close > close.shift(24)) & (close.shift(24) > close.shift(72))
    return staircase & _new_extreme(frame, 12, direction)


def _momentum(frame: pd.DataFrame, period: int, direction: int) -> pd.Series:
    close = _close(frame)
    if direction < 0:
        return close < close.shift(period)
    return close > close.shift(period)


def _spike_vs_sma(frame: pd.DataFrame, pct: float, direction: int) -> pd.Series:
    close = _close(frame)
    sma20 = close.rolling(20).mean()
    if direction > 0:
        return close > sma20 * (1 + pct)
    return close < sma20 * (1 - pct)


def _rsi_extreme(frame: pd.DataFrame, level: float, direction: int) -> pd.Series:
    rsi = _wilder_rsi(_close(frame))
    return rsi >= level if direction > 0 else rsi <= level


def _bb_extreme(frame: pd.DataFrame, z: float) -> pd.Series:
    zscore = _bb_z(_close(frame))
    return zscore >= z if z > 0 else zscore <= z


def _sma_cross(frame: pd.DataFrame, direction: int) -> pd.Series:
    close = _close(frame)
    return _cross(close, close.rolling(20).mean(), direction)


def _ema_cross(
    frame: pd.DataFrame, direction: int, fast: int = 9, slow: int = 50
) -> pd.Series:
    close = _close(frame)
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()
    return _cross(fast_ema, slow_ema, direction)


def _macd_cross(frame: pd.DataFrame, direction: int) -> pd.Series:
    close = _close(frame)
    macd = (
        close.ewm(span=12, adjust=False).mean()
        - close.ewm(span=26, adjust=False).mean()
    )
    signal = macd.ewm(span=9, adjust=False).mean()
    return _cross(macd, signal, direction)


def _rsi_cross(frame: pd.DataFrame, level: float, direction: int) -> pd.Series:
    rsi = _wilder_rsi(_close(frame))
    if direction > 0:
        return (rsi > level) & (rsi.shift(1) <= level)
    return (rsi < level) & (rsi.shift(1) >= level)


def _et_stamps(frame: pd.DataFrame) -> pd.Series:
    # DST-correct wall-clock in New York — the market whose hours shape
    # tokenized-equity perp flow.
    return pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(
        "America/New_York"
    )


def _session_window(
    frame: pd.DataFrame, start_minute: int, end_minute: int
) -> pd.Series:
    """Bars whose CLOSE lands in (start, end] ET wall-clock, Mon-Fri.

    Bars are close-labeled, so the half-open window on the close timestamp is
    what keeps pre-window data out: a 15m bar labeled 09:30 holds 09:15-09:30
    (pre-open) trade — the first open-hour bar is the one closing 09:45."""
    stamps = _et_stamps(frame)
    minutes = stamps.dt.hour * 60 + stamps.dt.minute
    weekday = stamps.dt.dayofweek < 5
    return weekday & (minutes > start_minute) & (minutes <= end_minute)


def _weekend(frame: pd.DataFrame) -> pd.Series:
    return _et_stamps(frame).dt.dayofweek >= 5


def _trend_gated_extreme(frame: pd.DataFrame, direction: int) -> pd.Series:
    close = _close(frame)
    slope_dn = _ema_slope_dn(close)
    trend = slope_dn if direction < 0 else ~slope_dn
    return trend & _new_extreme(frame, 5, direction)


SIGNAL_LIBRARY: tuple[SignalDef, ...] = (
    SignalDef(
        "new_low_5",
        "breakout",
        "close below the prior 5 closes' minimum",
        7,
        lambda f: _new_extreme(f, 5, -1),
    ),
    SignalDef(
        "new_high_5",
        "breakout",
        "close above the prior 5 closes' maximum",
        7,
        lambda f: _new_extreme(f, 5, +1),
    ),
    SignalDef(
        "new_low_20",
        "breakout",
        "close below the prior 20 closes' minimum",
        22,
        lambda f: _new_extreme(f, 20, -1),
    ),
    SignalDef(
        "new_high_20",
        "breakout",
        "close above the prior 20 closes' maximum",
        22,
        lambda f: _new_extreme(f, 20, +1),
    ),
    SignalDef(
        "fresh_new_low_5",
        "breakout",
        "first 5-bar-low break after a non-break bar (first trigger only)",
        8,
        lambda f: _fresh(_new_extreme(f, 5, -1)),
    ),
    SignalDef(
        "fresh_new_high_5",
        "breakout",
        "first 5-bar-high break after a non-break bar (first trigger only)",
        8,
        lambda f: _fresh(_new_extreme(f, 5, +1)),
    ),
    SignalDef(
        "mom_dn_20",
        "momentum",
        "close below the close 20 bars ago",
        21,
        lambda f: _momentum(f, 20, -1),
    ),
    SignalDef(
        "mom_up_20",
        "momentum",
        "close above the close 20 bars ago",
        21,
        lambda f: _momentum(f, 20, +1),
    ),
    SignalDef(
        "trend_dn_new_low_5",
        "trend",
        "5-bar-low break while the 50-EMA slopes down (regime-gated)",
        62,
        lambda f: _trend_gated_extreme(f, -1),
    ),
    SignalDef(
        "trend_up_new_high_5",
        "trend",
        "5-bar-high break while the 50-EMA slopes up (regime-gated)",
        62,
        lambda f: _trend_gated_extreme(f, +1),
    ),
    SignalDef(
        "ema_cross_dn_9_50",
        "trend",
        "9-EMA crossed below 50-EMA this bar",
        52,
        lambda f: _ema_cross(f, -1),
    ),
    SignalDef(
        "ema_cross_up_9_50",
        "trend",
        "9-EMA crossed above 50-EMA this bar",
        52,
        lambda f: _ema_cross(f, +1),
    ),
    SignalDef(
        "sma20_lose",
        "trend",
        "close crossed below the 20-SMA this bar",
        22,
        lambda f: _sma_cross(f, -1),
    ),
    SignalDef(
        "sma20_reclaim",
        "trend",
        "close crossed above the 20-SMA this bar",
        22,
        lambda f: _sma_cross(f, +1),
    ),
    SignalDef(
        "rsi14_ge_70",
        "mean_reversion",
        "Wilder RSI(14) at or above 70 (overbought)",
        30,
        lambda f: _rsi_extreme(f, 70, +1),
    ),
    SignalDef(
        "rsi14_le_30",
        "mean_reversion",
        "Wilder RSI(14) at or below 30 (oversold)",
        30,
        lambda f: _rsi_extreme(f, 30, -1),
    ),
    SignalDef(
        "bb20_z_ge_2",
        "mean_reversion",
        "close 2+ standard deviations above the 20-SMA",
        22,
        lambda f: _bb_extreme(f, 2.0),
    ),
    SignalDef(
        "bb20_z_le_neg2",
        "mean_reversion",
        "close 2+ standard deviations below the 20-SMA",
        22,
        lambda f: _bb_extreme(f, -2.0),
    ),
    SignalDef(
        "spike_up_5pct_20",
        "mean_reversion",
        "close 5%+ above the 20-SMA (upside overextension)",
        22,
        lambda f: _spike_vs_sma(f, 0.05, +1),
    ),
    SignalDef(
        "spike_dn_5pct_20",
        "mean_reversion",
        "close 5%+ below the 20-SMA (downside overextension)",
        22,
        lambda f: _spike_vs_sma(f, 0.05, -1),
    ),
    SignalDef(
        "wide_range_dn",
        "volatility",
        "bar range over 2x ATR(14) closing down",
        17,
        lambda f: _wide_range(f, -1),
    ),
    SignalDef(
        "wide_range_up",
        "volatility",
        "bar range over 2x ATR(14) closing up",
        17,
        lambda f: _wide_range(f, +1),
    ),
    SignalDef(
        "vol_surge_dn",
        "volatility",
        "volume over 2x its 20-bar average on a down close",
        23,
        lambda f: _vol_surge(f, -1),
    ),
    SignalDef(
        "vol_surge_up",
        "volatility",
        "volume over 2x its 20-bar average on an up close",
        23,
        lambda f: _vol_surge(f, +1),
    ),
    SignalDef(
        "compression_break_dn",
        "volatility",
        "20-bar range compressed to its bottom tercile, then a 20-bar-low break",
        122,
        lambda f: _compression_break(f, -1),
    ),
    SignalDef(
        "compression_break_up",
        "volatility",
        "20-bar range compressed to its bottom tercile, then a 20-bar-high break",
        122,
        lambda f: _compression_break(f, +1),
    ),
    SignalDef(
        "extended_low_24_72",
        "exhaustion",
        "staircase decline (24h and 72h down) making a fresh 12-bar low",
        85,
        lambda f: _extended(f, -1),
    ),
    SignalDef(
        "extended_high_24_72",
        "exhaustion",
        "staircase rally (24h and 72h up) making a fresh 12-bar high",
        85,
        lambda f: _extended(f, +1),
    ),
    SignalDef(
        "macd_cross_up_12_26_9",
        "trend",
        "MACD(12,26) line crossed above its 9-EMA signal line this bar",
        37,
        lambda f: _macd_cross(f, +1),
    ),
    SignalDef(
        "macd_cross_dn_12_26_9",
        "trend",
        "MACD(12,26) line crossed below its 9-EMA signal line this bar",
        37,
        lambda f: _macd_cross(f, -1),
    ),
    SignalDef(
        "ema_cross_up_9_21",
        "trend",
        "9-EMA crossed above 21-EMA this bar (fast variant of 9/50)",
        23,
        lambda f: _ema_cross(f, +1, fast=9, slow=21),
    ),
    SignalDef(
        "ema_cross_dn_9_21",
        "trend",
        "9-EMA crossed below 21-EMA this bar (fast variant of 9/50)",
        23,
        lambda f: _ema_cross(f, -1, fast=9, slow=21),
    ),
    SignalDef(
        "rsi14_cross_up_50",
        "momentum",
        "Wilder RSI(14) crossed above 50 this bar (regime flip, not extreme)",
        31,
        lambda f: _rsi_cross(f, 50.0, +1),
    ),
    SignalDef(
        "rsi14_cross_dn_50",
        "momentum",
        "Wilder RSI(14) crossed below 50 this bar (regime flip, not extreme)",
        31,
        lambda f: _rsi_cross(f, 50.0, -1),
    ),
    SignalDef(
        "us_open_hour",
        "session",
        "bar CLOSED in the first US cash hour — (09:30, 10:30] ET, Mon-Fri "
        "(never fires on 1d bars; dropped by the min-events gate there)",
        2,
        lambda f: _session_window(f, 9 * 60 + 30, 10 * 60 + 30),
    ),
    SignalDef(
        "us_close_hour",
        "session",
        "bar CLOSED in the last US cash hour — (15:00, 16:00] ET, Mon-Fri "
        "(never fires on 1d bars; dropped by the min-events gate there)",
        2,
        lambda f: _session_window(f, 15 * 60, 16 * 60),
    ),
    SignalDef(
        "weekend",
        "session",
        "bar closed on Saturday or Sunday, New York time",
        2,
        lambda f: _weekend(f),
    ),
)


def build_signal_frame(
    frame: pd.DataFrame, extra_signals: Sequence[SignalDef] = ()
) -> pd.DataFrame:
    """All library signals for one symbol's OHLCV frame, as boolean columns
    row-aligned with the input. NaN warmup rows resolve to False.

    `extra_signals` (validated workspace defs) are materialized after the
    canonical library so the scan sweeps both under one test family."""
    out = pd.DataFrame(index=frame.index)
    for spec in (*SIGNAL_LIBRARY, *extra_signals):
        out[spec.name] = (
            spec.build(frame).fillna(False).astype(bool)
            if len(frame) >= spec.min_bars
            else False
        )
    return out


def signal_defs() -> dict[str, SignalDef]:
    return {spec.name: spec for spec in SIGNAL_LIBRARY}
