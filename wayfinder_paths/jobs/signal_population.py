"""Mechanical population search: the library's triggers with varied windows
and thresholds, and pairs composed across families, as scan definitions.

The origin of the one starter that passes the screen (Codex, 2026-08-19) was
a scan, not a prompt; the cheap inner loop that MadEvolve and AlphaAgent use
is this: a few hundred definitions scanned under one Benjamini-Hochberg
family, survivors only in front of the model. Every def carries its DSL
source so a survivor is pasteable (``compile_signal_expression``).
"""

from __future__ import annotations

import random

from wayfinder_paths.jobs.signal_library import SignalDef, compile_signal_expression

POPULATION_FAMILY = "population"
POPULATION_LIMIT_CAP = 400

# Canonical atoms by side and family, as DSL source with their warmup.
_ATOMS: dict[str, tuple[str, int]] = {
    "rsi14_le_30": ("rsi_extreme(f, 30, -1)", 30),
    "rsi14_ge_70": ("rsi_extreme(f, 70, +1)", 30),
    "bb20_z_le_neg2": ("bb_extreme(f, -2.0)", 22),
    "bb20_z_ge_2": ("bb_extreme(f, 2.0)", 22),
    "spike_dn_5pct_20": ("spike_vs_sma(f, 0.05, -1)", 22),
    "spike_up_5pct_20": ("spike_vs_sma(f, 0.05, +1)", 22),
    "wide_range_dn": ("wide_range(f, -1)", 17),
    "wide_range_up": ("wide_range(f, +1)", 17),
    "vol_surge_dn": ("vol_surge(f, -1)", 23),
    "vol_surge_up": ("vol_surge(f, +1)", 23),
    "new_low_5": ("new_extreme(f, 5, -1)", 7),
    "new_high_5": ("new_extreme(f, 5, +1)", 7),
    "new_low_20": ("new_extreme(f, 20, -1)", 22),
    "new_high_20": ("new_extreme(f, 20, +1)", 22),
    "ema_cross_dn_9_21": ("ema_cross(f, -1, fast=9, slow=21)", 23),
    "ema_cross_up_9_21": ("ema_cross(f, +1, fast=9, slow=21)", 23),
    "trend_dn_new_low_5": ("trend_gated_extreme(f, -1)", 62),
    "trend_up_new_high_5": ("trend_gated_extreme(f, +1)", 62),
    "us_open_hour": ("session_window(f, 9 * 60 + 30, 10 * 60 + 30)", 1),
    "us_close_hour": ("session_window(f, 15 * 60, 16 * 60)", 1),
    "weekend": ("weekend(f)", 1),
}
_DOWN: dict[str, tuple[str, ...]] = {
    "mean_reversion": ("rsi14_le_30", "bb20_z_le_neg2", "spike_dn_5pct_20"),
    "volatility": ("wide_range_dn", "vol_surge_dn"),
    "breakout": ("new_low_5", "new_low_20"),
    "trend": ("ema_cross_dn_9_21", "trend_dn_new_low_5"),
}
_UP: dict[str, tuple[str, ...]] = {
    "mean_reversion": ("rsi14_ge_70", "bb20_z_ge_2", "spike_up_5pct_20"),
    "volatility": ("wide_range_up", "vol_surge_up"),
    "breakout": ("new_high_5", "new_high_20"),
    "trend": ("ema_cross_up_9_21", "trend_up_new_high_5"),
}
_SESSIONS = ("us_open_hour", "us_close_hour", "weekend")
_PAIRS = (
    ("mean_reversion", "volatility"),
    ("breakout", "volatility"),
    ("trend", "breakout"),
)
_LAGS = (0, 1, 3)

Row = tuple[str, str, int, str]  # name, expression, min_bars, description


def _token(value: float) -> str:
    text = f"{value:g}".replace("-", "neg").replace(".", "p")
    return text


def _singles() -> list[Row]:
    rows: list[Row] = []
    for period in (3, 8, 13, 34, 55):
        rows.append(
            (
                f"new_low_{period}",
                f"new_extreme(f, {period}, -1)",
                period + 2,
                f"close below the prior {period} closes' minimum",
            )
        )
        rows.append(
            (
                f"new_high_{period}",
                f"new_extreme(f, {period}, +1)",
                period + 2,
                f"close above the prior {period} closes' maximum",
            )
        )
    for period in (5, 10, 40):
        rows.append(
            (
                f"mom_dn_{period}",
                f"momentum(f, {period}, -1)",
                period + 1,
                f"close below the close {period} bars ago",
            )
        )
        rows.append(
            (
                f"mom_up_{period}",
                f"momentum(f, {period}, +1)",
                period + 1,
                f"close above the close {period} bars ago",
            )
        )
    for level in (20, 25, 35):
        rows.append(
            (
                f"rsi14_le_{level}",
                f"rsi_extreme(f, {level}, -1)",
                30,
                f"Wilder RSI(14) at or below {level}",
            )
        )
    for level in (65, 75, 80):
        rows.append(
            (
                f"rsi14_ge_{level}",
                f"rsi_extreme(f, {level}, +1)",
                30,
                f"Wilder RSI(14) at or above {level}",
            )
        )
    rows.append(("rsi7_le_30", "rsi_extreme(f, 30, -1, period=7)", 16, "RSI(7) <= 30"))
    rows.append(("rsi7_ge_70", "rsi_extreme(f, 70, +1, period=7)", 16, "RSI(7) >= 70"))
    for z in (1.5, 2.5, 3.0):
        rows.append(
            (
                f"bb20_z_le_neg{_token(z)}",
                f"bb_extreme(f, -{z})",
                22,
                f"close {z}+ standard deviations below the 20-SMA",
            )
        )
        rows.append(
            (
                f"bb20_z_ge_{_token(z)}",
                f"bb_extreme(f, {z})",
                22,
                f"close {z}+ standard deviations above the 20-SMA",
            )
        )
    for pct in (0.03, 0.08):
        label = int(round(pct * 100))
        rows.append(
            (
                f"spike_dn_{label}pct_20",
                f"spike_vs_sma(f, {pct}, -1)",
                22,
                f"close {label}%+ below the 20-SMA",
            )
        )
        rows.append(
            (
                f"spike_up_{label}pct_20",
                f"spike_vs_sma(f, {pct}, +1)",
                22,
                f"close {label}%+ above the 20-SMA",
            )
        )
    for multiple in (1.5, 3.0):
        for direction, side in ((-1, "dn"), (+1, "up")):
            rows.append(
                (
                    f"wide_range_{side}_x{_token(multiple)}",
                    f"wide_range(f, {direction:+d}, multiple={multiple})",
                    17,
                    f"bar range over {multiple}x ATR(14) closing {side}",
                )
            )
    for multiple in (1.5, 3.0):
        for window in (10, 40):
            for direction, side in ((-1, "dn"), (+1, "up")):
                rows.append(
                    (
                        f"vol_surge_{side}_x{_token(multiple)}_w{window}",
                        f"vol_surge(f, {direction:+d}, multiple={multiple}, window={window})",
                        window + 3,
                        f"volume over {multiple}x its {window}-bar average, {side} close",
                    )
                )
    for fast, slow in ((5, 20), (12, 48), (20, 100)):
        for direction, side in ((-1, "dn"), (+1, "up")):
            rows.append(
                (
                    f"ema_cross_{side}_{fast}_{slow}",
                    f"ema_cross(f, {direction:+d}, fast={fast}, slow={slow})",
                    slow + 2,
                    f"{fast}-EMA crossed {'below' if direction < 0 else 'above'} "
                    f"{slow}-EMA this bar",
                )
            )
    for fast, slow, signal in ((8, 17, 9), (19, 39, 9)):
        for direction, side in ((-1, "dn"), (+1, "up")):
            rows.append(
                (
                    f"macd_cross_{side}_{fast}_{slow}_{signal}",
                    f"macd_cross(f, {direction:+d}, fast={fast}, slow={slow}, signal={signal})",
                    slow + signal + 2,
                    f"MACD({fast},{slow}) crossed its {signal}-EMA signal, {side}",
                )
            )
    for short, long in ((12, 36), (48, 144)):
        for direction, side in ((-1, "low"), (+1, "high")):
            rows.append(
                (
                    f"extended_{side}_{short}_{long}",
                    f"extended(f, {direction:+d}, short={short}, long={long})",
                    long + 14,
                    f"staircase move ({short} and {long} bars) making a fresh 12-bar {side}",
                )
            )
    for period in (10, 50):
        for direction, side in ((-1, "lose"), (+1, "reclaim")):
            rows.append(
                (
                    f"sma{period}_{side}",
                    f"sma_cross(f, {direction:+d}, period={period})",
                    period + 2,
                    f"close crossed {'below' if direction < 0 else 'above'} the {period}-SMA",
                )
            )
    for period, lookback in ((10, 50), (40, 200)):
        for direction, side in ((-1, "dn"), (+1, "up")):
            rows.append(
                (
                    f"compression_break_{side}_{period}_{lookback}",
                    f"compression_break(f, {direction:+d}, period={period}, lookback={lookback})",
                    period + lookback + 2,
                    f"{period}-bar range in its bottom tercile of {lookback}, then a {period}-bar break {side}",
                )
            )
    for direction, side in ((-1, "dn"), (+1, "up")):
        rows.append(
            (
                f"rsi7_cross_{side}_50",
                f"rsi_cross(f, 50.0, {direction:+d}, period=7)",
                16,
                f"RSI(7) crossed {'below' if direction < 0 else 'above'} 50",
            )
        )
    return rows


def _compose(left: str, right: str, lag: int) -> Row:
    left_source, left_bars = _ATOMS[left]
    right_source, right_bars = _ATOMS[right]
    if lag:
        expression = (
            f"({left_source}) & ({right_source}).shift({lag}, fill_value=False)"
        )
        description = f"{left} with {right} {lag} bars earlier"
        name = f"{left}_and_{right}_lag{lag}"
    else:
        expression = f"({left_source}) & ({right_source})"
        description = f"{left} and {right} on the same bar"
        name = f"{left}_and_{right}"
    return name, expression, max(left_bars, right_bars) + lag + 2, description


def _compositions(seed: int) -> list[Row]:
    rows: list[Row] = []
    for side in (_DOWN, _UP):
        for left_family, right_family in _PAIRS:
            for left in side[left_family]:
                for right in side[right_family]:
                    for lag in _LAGS:
                        rows.append(_compose(left, right, lag))
    for session in _SESSIONS:
        for family in ("mean_reversion", "breakout"):
            for atom in (*_DOWN[family], *_UP[family]):
                for lag in _LAGS:
                    rows.append(_compose(session, atom, lag))
    random.Random(seed).shuffle(rows)
    return rows


def population_defs(*, limit: int = 300, seed: int = 0) -> tuple[SignalDef, ...]:
    """Parameter variants first (deterministic), then cross-family pairs in a
    seeded order, truncated to ``limit`` (capped). Names are prefixed so they
    never collide with the canonical library."""
    bound = max(1, min(int(limit), POPULATION_LIMIT_CAP))
    rows = [*_singles(), *_compositions(seed)][:bound]
    defs = tuple(
        compile_signal_expression(
            name=f"pop_{name}",
            family=POPULATION_FAMILY,
            description=description,
            min_bars=min_bars,
            expression=expression,
        )
        for name, expression, min_bars, description in rows
    )
    names = [spec.name for spec in defs]
    if len(set(names)) != len(names):
        raise ValueError("population names collide")
    return defs
