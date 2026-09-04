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
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.jobs.indicators import atr as wilder_atr
from wayfinder_paths.jobs.indicators import wilder_rsi


@dataclass(frozen=True)
class SignalDef:
    name: str
    family: str
    description: str
    min_bars: int
    build: Callable[[pd.DataFrame], pd.Series]
    # DSL source for defs that were composed rather than hand-written (the
    # population search, designer proposals): what a worker pastes.
    expression: str | None = None


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


def _wide_range(
    frame: pd.DataFrame, direction: int, *, multiple: float = 2.0, period: int = 14
) -> pd.Series:
    close = _close(frame)
    tr = (frame["high"].astype(float) - frame["low"].astype(float)).abs()
    wide = tr > multiple * _atr(frame, period).shift(1)
    if direction < 0:
        return wide & (close < frame["open"].astype(float))
    return wide & (close > frame["open"].astype(float))


def _vol_surge(
    frame: pd.DataFrame, direction: int, *, multiple: float = 2.0, window: int = 20
) -> pd.Series:
    close = _close(frame)
    volume = frame["volume"].astype(float)
    surge = volume > multiple * volume.shift(1).rolling(window).mean()
    if direction < 0:
        return surge & (close < close.shift(1))
    return surge & (close > close.shift(1))


def _compression_break(
    frame: pd.DataFrame,
    direction: int,
    *,
    period: int = 20,
    lookback: int = 100,
    quantile: float = 0.33,
) -> pd.Series:
    close = _close(frame)
    window_range = close.rolling(period).max() - close.rolling(period).min()
    compressed = window_range.shift(1) < window_range.shift(1).rolling(
        lookback
    ).quantile(quantile)
    return compressed & _new_extreme(frame, period, direction)


def _extended(
    frame: pd.DataFrame,
    direction: int,
    *,
    short: int = 24,
    long: int = 72,
    extreme: int = 12,
) -> pd.Series:
    """Deeply extended multi-day move making a fresh 12-bar extreme — the
    exhaustion event: scans often show it REVERSES rather than continues."""
    close = _close(frame)
    if direction < 0:
        staircase = (close < close.shift(short)) & (
            close.shift(short) < close.shift(long)
        )
    else:
        staircase = (close > close.shift(short)) & (
            close.shift(short) > close.shift(long)
        )
    return staircase & _new_extreme(frame, extreme, direction)


def _momentum(frame: pd.DataFrame, period: int, direction: int) -> pd.Series:
    close = _close(frame)
    if direction < 0:
        return close < close.shift(period)
    return close > close.shift(period)


def _spike_vs_sma(
    frame: pd.DataFrame, pct: float, direction: int, *, period: int = 20
) -> pd.Series:
    close = _close(frame)
    sma = close.rolling(period).mean()
    if direction > 0:
        return close > sma * (1 + pct)
    return close < sma * (1 - pct)


def _rsi_extreme(
    frame: pd.DataFrame, level: float, direction: int, *, period: int = 14
) -> pd.Series:
    rsi = wilder_rsi(_close(frame), period)
    return rsi >= level if direction > 0 else rsi <= level


def _bb_extreme(frame: pd.DataFrame, z: float, *, period: int = 20) -> pd.Series:
    zscore = _bb_z(_close(frame), period)
    return zscore >= z if z > 0 else zscore <= z


def _sma_cross(frame: pd.DataFrame, direction: int, *, period: int = 20) -> pd.Series:
    close = _close(frame)
    return _cross(close, close.rolling(period).mean(), direction)


def _ema_cross(
    frame: pd.DataFrame, direction: int, fast: int = 9, slow: int = 50
) -> pd.Series:
    close = _close(frame)
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()
    return _cross(fast_ema, slow_ema, direction)


def _macd_cross(
    frame: pd.DataFrame,
    direction: int,
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.Series:
    close = _close(frame)
    macd = (
        close.ewm(span=fast, adjust=False).mean()
        - close.ewm(span=slow, adjust=False).mean()
    )
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    return _cross(macd, signal_line, direction)


def _rsi_cross(
    frame: pd.DataFrame, level: float, direction: int, *, period: int = 14
) -> pd.Series:
    rsi = wilder_rsi(_close(frame), period)
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


def _trend_gated_extreme(
    frame: pd.DataFrame,
    direction: int,
    *,
    period: int = 5,
    span: int = 50,
    lag: int = 10,
) -> pd.Series:
    close = _close(frame)
    slope_dn = _ema_slope_dn(close, span, lag)
    trend = slope_dn if direction < 0 else ~slope_dn
    return trend & _new_extreme(frame, period, direction)


def _sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period).mean()


def _ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


# Public names for the builders: the DSL a composed def (population search,
# designer proposal) is written in, and what a worker imports to paste one.
close = _close
sma = _sma
ema = _ema
atr = _atr
new_extreme = _new_extreme
fresh = _fresh
cross = _cross
bb_z = _bb_z
ema_slope_dn = _ema_slope_dn
wide_range = _wide_range
vol_surge = _vol_surge
compression_break = _compression_break
extended = _extended
momentum = _momentum
spike_vs_sma = _spike_vs_sma
rsi_extreme = _rsi_extreme
bb_extreme = _bb_extreme
sma_cross = _sma_cross
ema_cross = _ema_cross
macd_cross = _macd_cross
rsi_cross = _rsi_cross
session_window = _session_window
weekend = _weekend
trend_gated_extreme = _trend_gated_extreme

SIGNAL_DSL: dict[str, Any] = {
    "pd": pd,
    "np": np,
    "close": close,
    "sma": sma,
    "ema": ema,
    "atr": atr,
    "wilder_rsi": wilder_rsi,
    "new_extreme": new_extreme,
    "fresh": fresh,
    "cross": cross,
    "bb_z": bb_z,
    "ema_slope_dn": ema_slope_dn,
    "wide_range": wide_range,
    "vol_surge": vol_surge,
    "compression_break": compression_break,
    "extended": extended,
    "momentum": momentum,
    "spike_vs_sma": spike_vs_sma,
    "rsi_extreme": rsi_extreme,
    "bb_extreme": bb_extreme,
    "sma_cross": sma_cross,
    "ema_cross": ema_cross,
    "macd_cross": macd_cross,
    "rsi_cross": rsi_cross,
    "session_window": session_window,
    "weekend": weekend,
    "trend_gated_extreme": trend_gated_extreme,
}
_DSL_BUILTINS = {"abs": abs, "min": min, "max": max, "round": round}


def compile_signal_expression(
    *,
    name: str,
    family: str,
    description: str,
    min_bars: int,
    expression: str,
) -> SignalDef:
    """A SignalDef from DSL source: one Python expression over ``f`` (the bar
    frame) and the SIGNAL_DSL names. Same trust boundary as an exec'd
    ``workspace/src/signals.py``; the causality validator judges the result,
    not this function."""
    source = str(expression).strip()
    if not source or "\n" in source:
        raise ValueError(f"{name!r}: expression must be one non-empty line")
    build = eval(  # noqa: S307 - DSL over a fixed namespace, validated after
        f"lambda f: ({source})", {"__builtins__": _DSL_BUILTINS, **SIGNAL_DSL}
    )
    return SignalDef(name, family, description, int(min_bars), build, source)


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
    frame: pd.DataFrame,
    extra_signals: Sequence[SignalDef] = (),
    *,
    include_canonical: bool = True,
    canonical_signals: Sequence[SignalDef] = (),
) -> pd.DataFrame:
    """All library signals for one symbol's OHLCV frame, as boolean columns
    row-aligned with the input. NaN warmup rows resolve to False.

    `extra_signals` (validated workspace defs) are materialized after the
    canonical library so the scan sweeps both under one test family.
    `canonical_signals` selects required canonical controls when the complete
    library is disabled for a declared campaign."""
    library = SIGNAL_LIBRARY if include_canonical else tuple(canonical_signals)
    # One concat, not one insert per column: a population of a few hundred
    # defs on a year of 5-minute bars fragments the frame otherwise.
    columns: dict[str, pd.Series] = {}
    for spec in (*library, *extra_signals):
        columns[spec.name] = (
            spec.build(frame).fillna(False).astype(bool)
            if len(frame) >= spec.min_bars
            else pd.Series(False, index=frame.index, dtype=bool)
        )
    if not columns:
        return pd.DataFrame(index=frame.index)
    return pd.DataFrame(columns, index=frame.index)


def signal_defs() -> dict[str, SignalDef]:
    return {spec.name: spec for spec in SIGNAL_LIBRARY}
