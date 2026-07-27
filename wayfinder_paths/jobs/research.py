"""Pre-trade statistical research toolkit: signal checks and pair admission.

Why this exists: live sessions showed the strategy agent building six parameter
variants of signals that never had predictive power, and pair trades on
correlated-but-not-cointegrated majors (ETH/SOL) where no parameter could
rescue a spread that doesn't mean-revert. The cheap fix is to test the SIGNAL
or the PAIR before building the strategy — a REJECT here is the methodology
working, not failing.

Layout contract: everything above `pair_admission_gate` is PURE (numpy/pandas
only — importable from a strategy `precompute()` inside the purity sandbox).
`*_job` orchestrators at the bottom do I/O (JobStore, ccxt) with function-local
imports, mirroring op_runner.

Statistical caveats (documented, deliberate):
- Engle-Granger critical values are hard-coded MacKinnon case-2 asymptotic
  anchors (constant, two variables): 1% = -3.90, 5% = -3.34, 10% = -3.04.
  Small-sample corrections at n>=1500 bars shift t by <0.05 — ignored.
- Multiple testing: at the 5% level, ~1 in 20 random pairs "passes"
  cointegration by chance. The rolling-stability majority requirement is the
  main defense; reports carry an explicit note. Scan wide, trade few.
- Event-study p-values use the normal approximation of the t-stat via
  math.erfc (the n>=30 sample gate makes the small-sample error negligible);
  the scan's multiplicity control is Benjamini-Hochberg over the full test
  family, and event decimation (horizon-spaced) removes forward-window
  overlap — the dominant dependence source at these frequencies.
- Deferred rigor (recorded, not built): HAC/Newey-West + block-bootstrap
  standard errors (revisit if the null-world tests start failing), Deflated
  Sharpe / PBO-CSCV strategy-level overfit stats, BTC/ETH market-relative
  controls + matched regime baselines, funding/carry scan family.

Stats functions adapted with thanks from
examples/paths/spread-radar-reference/scripts/lib.py (ou_half_life,
engle_granger residual test, stability idea) and
examples/paths/hedge-finder/scripts/lib.py (adf_statistic, beta) — copied,
not imported: examples/ is not a runtime package.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.jobs.signal_library import (
    SIGNAL_LIBRARY,
    SignalDef,
    build_signal_frame,
    signal_defs,
    wilder_atr,
)

# MacKinnon case-2 (constant, 2 variables) critical values for the
# Engle-Granger residual t-stat. The upstream example labeled -3.34 as
# p=0.03; it is the 5% critical value — corrected here.
EG_CRITICAL = {"1%": -3.90, "5%": -3.34, "10%": -3.04}
# Plain ADF (single series, constant) 5% critical value, n≈500.
ADF_CRITICAL_5PCT = -2.86

BAR_90D_SECONDS = 90 * 86_400


# ── pure statistics ──────────────────────────────────────────────────────────


def ou_half_life(spread: np.ndarray) -> float:
    """Ornstein-Uhlenbeck half-life in BARS via AR(1). inf = not mean-reverting."""
    spread = np.asarray(spread, dtype=float)
    y, x = spread[1:], spread[:-1]
    if len(x) < 20:
        return float("inf")
    xm = x.mean()
    denom = np.sum((x - xm) ** 2)
    if denom == 0:
        return float("inf")
    b = np.sum((x - xm) * (y - xm)) / denom
    if b >= 1 or b <= 0:
        return float("inf")
    return float(np.log(2) / (-np.log(b)))


def adf_stat(series: np.ndarray) -> float:
    """ADF(1) t-statistic with constant. More negative = more stationary.
    5% critical ≈ -2.86 (n≈500). Returns 0.0 when the series is too short."""
    y = np.asarray(series, dtype=float)
    y = y[~np.isnan(y)]
    if len(y) < 24:
        return 0.0
    dy = np.diff(y)
    y_lag = y[:-1]
    dy_lag = np.concatenate([[0.0], dy[:-1]])
    n = len(dy)
    x = np.column_stack([np.ones(n), y_lag, dy_lag])
    try:
        coeffs, _, _, _ = np.linalg.lstsq(x, dy, rcond=None)
    except np.linalg.LinAlgError:
        return 0.0
    b = coeffs[1]
    resid = dy - x @ coeffs
    s2 = float((resid**2).sum()) / max(n - 3, 1)
    xtx_inv = np.linalg.pinv(x.T @ x)
    se = math.sqrt(max(s2 * xtx_inv[1, 1], 1e-30))
    return float(b / se)


def _residual_adf_t(resid: np.ndarray) -> float:
    """Engle-Granger step 2: ADF t-stat on regression residuals (no constant —
    residuals are mean-zero by construction)."""
    dy, yl = np.diff(resid), resid[:-1]
    denom = np.sum(yl**2)
    if denom == 0 or len(dy) < 2:
        return 0.0
    rho = np.sum(yl * dy) / denom
    se = np.sqrt(np.sum((dy - rho * yl) ** 2) / (len(dy) - 1)) / np.sqrt(denom)
    if se == 0:
        return 0.0
    return float(rho / se)


def engle_granger(
    y: np.ndarray, x: np.ndarray, *, log_prices: bool = True
) -> dict[str, Any]:
    """Engle-Granger cointegration test of y on x.

    Returns hedge_ratio (regression slope), residual ADF t-stat, the critical
    bucket it clears, a 5% pass flag, and the residual half-life in bars.
    """
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    if log_prices:
        y, x = np.log(y), np.log(x)
    n = len(y)
    if n < 50 or n != len(x):
        return {
            "hedge_ratio": None,
            "t_stat": 0.0,
            "level": None,
            "cointegrated_5pct": False,
            "half_life_bars": float("inf"),
        }
    design = np.column_stack([np.ones(n), x])
    coeffs = np.linalg.lstsq(design, y, rcond=None)[0]
    resid = y - design @ coeffs
    t = _residual_adf_t(resid)
    level = None
    for name, crit in EG_CRITICAL.items():
        if t < crit:
            level = name
            break
    return {
        "hedge_ratio": float(coeffs[1]),
        "t_stat": t,
        "level": level,
        "cointegrated_5pct": t < EG_CRITICAL["5%"],
        "half_life_bars": ou_half_life(resid),
    }


def engle_granger_both(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    """EG in both directions (the test is order-sensitive). Pass = both clear
    the 5% critical value on log prices."""
    ab = engle_granger(a, b)
    ba = engle_granger(b, a)
    return {
        "ab": ab,
        "ba": ba,
        "pass": bool(ab["cointegrated_5pct"] and ba["cointegrated_5pct"]),
    }


def hurst_exponent(series: np.ndarray, max_lag: int = 100) -> float:
    """Hurst exponent via variance of differences. H < 0.5 mean-reverting,
    H ≈ 0.5 random walk, H > 0.5 trending."""
    x = np.asarray(series, dtype=float)
    x = x[~np.isnan(x)]
    max_lag = min(max_lag, len(x) // 4)
    if max_lag < 8:
        return 0.5
    lags = np.unique(np.geomspace(2, max_lag, num=12).astype(int))
    taus = []
    for lag in lags:
        diff = x[lag:] - x[:-lag]
        std = diff.std()
        taus.append(std if std > 0 else 1e-12)
    slope = np.polyfit(np.log(lags), np.log(taus), 1)[0]
    return float(slope)


def rolling_hedge_ratio(y: pd.Series, x: pd.Series, window: int) -> dict[str, Any]:
    """Rolling OLS slope of log(y) on log(x) — the cointegration-vector hedge
    ratio (never size a pair 1:1 by dollars)."""
    ly = np.log(pd.Series(y, dtype=float))
    lx = np.log(pd.Series(x, dtype=float))
    cov = ly.rolling(window).cov(lx)
    var = lx.rolling(window).var()
    ratio = (cov / var.replace(0, np.nan)).dropna()
    if ratio.empty:
        return {"current": None, "mean": None, "std": None, "window": window}
    return {
        "current": float(ratio.iloc[-1]),
        "mean": float(ratio.mean()),
        "std": float(ratio.std()),
        "window": window,
    }


def mean_crossings(spread: np.ndarray) -> int:
    """Number of times the spread crosses its own mean."""
    x = np.asarray(spread, dtype=float)
    x = x[~np.isnan(x)]
    if len(x) < 2:
        return 0
    centered = x - x.mean()
    return int(np.sum(np.sign(centered[1:]) != np.sign(centered[:-1])))


def rolling_stability(
    log_a: np.ndarray,
    log_b: np.ndarray,
    *,
    window_bars: int,
    step_bars: int,
) -> dict[str, Any]:
    """Slide a window over the pair and test cointegration in each — a
    relationship that only appears in one window is likely spurious. Windows
    use the 10% critical value (less power at smaller n); the gate requires a
    majority of windows to pass."""
    a = np.asarray(log_a, dtype=float)
    b = np.asarray(log_b, dtype=float)
    n = len(a)
    results = []
    start = 0
    while start + window_bars <= n:
        seg_t = engle_granger(
            a[start : start + window_bars],
            b[start : start + window_bars],
            log_prices=False,
        )["t_stat"]
        results.append(bool(seg_t < EG_CRITICAL["10%"]))
        start += step_bars
    if not results:
        return {"windows": 0, "pass_fraction": 0.0}
    return {
        "windows": len(results),
        "pass_fraction": float(np.mean(results)),
        "per_window": results,
    }


def cost_hurdle(
    spread_sigma: float,
    *,
    z_entry: float,
    z_exit: float,
    fee_bps: float,
    slippage_bps: float,
    legs: int = 2,
) -> dict[str, Any]:
    """Expected capture per trade vs total round-trip cost across all legs.
    Practitioner floor: capture should be >= 3x cost."""
    expected_capture = (z_entry - z_exit) * spread_sigma
    round_trip_cost = legs * 2 * (fee_bps + slippage_bps) / 1e4
    multiple = (
        expected_capture / round_trip_cost if round_trip_cost > 0 else float("inf")
    )
    return {
        "expected_capture": float(expected_capture),
        "round_trip_cost": float(round_trip_cost),
        "multiple": float(multiple),
        "pass": bool(multiple >= 3.0),
    }


def net_funding_carry(
    funding_long: pd.Series,
    funding_short: pd.Series,
    *,
    hedge_ratio: float = 1.0,
    periods_per_year: float = 365 * 3,
) -> dict[str, Any]:
    """Annualized net funding carry of long-A / short-B. Longs PAY positive
    funding; shorts RECEIVE it. Positive result = the position is paid to wait."""
    fl = pd.Series(funding_long, dtype=float).dropna()
    fs = pd.Series(funding_short, dtype=float).dropna()
    if fl.empty or fs.empty:
        return {"available": False, "annualized": None}
    per_period = -fl.mean() + hedge_ratio * fs.mean()
    return {
        "available": True,
        "annualized": float(per_period * periods_per_year),
        "long_leg_mean": float(fl.mean()),
        "short_leg_mean": float(fs.mean()),
    }


def rolling_adf_kill(
    spread: pd.Series, *, window: int, threshold: float = ADF_CRITICAL_5PCT
) -> pd.Series:
    """Causal rolling ADF stat of the spread — the live kill-switch column.
    Strategies compute this in precompute() and stand down when it rises above
    the threshold (cointegration breakdown is THE tail risk of pair trading)."""
    s = pd.Series(spread, dtype=float)
    out = pd.Series(np.nan, index=s.index)
    values = s.to_numpy()
    for i in range(window, len(values) + 1):
        out.iloc[i - 1] = adf_stat(values[i - window : i])
    return out


def event_study(
    frame: pd.DataFrame,
    signal: str | pd.Series,
    horizons: list[int] | None = None,
    *,
    direction: str = "long",
    decimate: bool = True,
    min_events: int = 30,
) -> dict[str, Any]:
    """Does an entry signal predict forward returns AT ALL? Run this before
    building a strategy around it — if the signal doesn't beat the series'
    own unconditional drift, no exit/sizing engineering can save it.

    For each horizon h: mean forward log-return after signal bars vs the
    unconditional mean forward return of the whole series (the random-entry
    baseline), with a t-stat on the difference and the event count.
    n < min_events is flagged insufficient — never treated as evidence.

    `direction` declares the trade side under test: "long" needs t >= 2,
    "short" needs t <= -2 (a genuine short edge produces NEGATIVE forward
    returns — the pre-fix behavior rejected exactly those), and "auto" reads
    the side from the t-stat sign per horizon (|t| >= 2) but counts as TWO
    trials per horizon (`trials_multiplier`). The hit rate is
    direction-adjusted: mean(side * fwd > 0).

    Events are DECIMATED to at least h bars apart (`decimate=True`) so
    forward windows never overlap — clustered triggers would otherwise count
    one episode as dozens of "independent" samples. `n` is the decimated
    count the t-test used; `n_raw` is pre-decimation.
    """
    if direction not in {"long", "short", "auto"}:
        raise ValueError(f"direction must be long|short|auto, got {direction!r}")
    horizons = horizons or [1, 4, 12, 24, 48]
    close = frame["close"].astype(float).to_numpy()
    if isinstance(signal, str):
        if signal not in frame.columns:
            raise KeyError(
                f"signal column {signal!r} not in frame; available: "
                f"{sorted(c for c in frame.columns)}"
            )
        sig = frame[signal]
    else:
        sig = signal
    sig = pd.Series(sig).fillna(False).astype(bool).to_numpy()
    n = len(close)
    per_horizon = []
    any_edge = False
    for h in sorted({int(h) for h in horizons}):
        if h <= 0 or h >= n:
            continue
        fwd = np.log(close[h:] / close[:-h])  # forward return starting at t
        raw_events = sig[: n - h]
        n_raw = int(raw_events.sum())
        events = _decimate_events(raw_events, h) if decimate else raw_events
        event_returns = fwd[events]
        n_events = int(events.sum())
        drift = float(fwd.mean())
        if n_events == 0:
            per_horizon.append(
                {"horizon": h, "n": 0, "n_raw": n_raw, "verdict": "no events"}
            )
            continue
        mean_r = float(event_returns.mean())
        std_r = float(event_returns.std(ddof=1)) if n_events > 1 else 0.0
        sem = (
            std_r / math.sqrt(n_events) if n_events > 1 and std_r > 0 else float("inf")
        )
        t = (mean_r - drift) / sem if math.isfinite(sem) else 0.0
        if direction == "long":
            side = 1
            edge = bool(t >= 2.0 and n_events >= min_events)
        elif direction == "short":
            side = -1
            edge = bool(t <= -2.0 and n_events >= min_events)
        else:
            side = 1 if t > 0 else -1 if t < 0 else 0
            edge = bool(abs(t) >= 2.0 and n_events >= min_events)
        row_direction = (
            direction
            if direction != "auto"
            else ("long" if side > 0 else "short" if side < 0 else None)
        )
        any_edge = any_edge or edge
        per_horizon.append(
            {
                "horizon": h,
                "n": n_events,
                "n_raw": n_raw,
                "mean_fwd_return": mean_r,
                "drift_baseline": drift,
                "hit_rate": float((side * event_returns > 0).mean()),
                "t_stat_vs_drift": float(t),
                "direction": row_direction,
                "edge": edge,
                "note": (
                    f"insufficient sample (n<{min_events})"
                    if n_events < min_events
                    else None
                ),
            }
        )
    multiple_testing_note = (
        "testing many signals inflates false positives — at t>=2, roughly "
        "1 in 20 random signals looks good by chance; prefer fewer, "
        "stronger hypotheses"
    )
    if direction == "auto":
        multiple_testing_note += (
            " — auto evaluates both sides, so count this as two trials per horizon"
        )
    result: dict[str, Any] = {
        "direction": direction,
        "horizons": per_horizon,
        "has_edge": any_edge,
        "read": (
            f"signal beats the series' own drift at one or more horizons "
            f"(direction={direction}) — worth building and validating"
            if any_edge
            else f"no horizon beats the unconditional drift with the required "
            f"sign (direction={direction}, |t|>=2, n>={min_events} "
            "non-overlapping events) — the entry has no measured predictive "
            "power; change the idea, not the parameters"
        ),
        "multiple_testing_note": multiple_testing_note,
    }
    if direction == "auto":
        result["trials_multiplier"] = 2
    return result


def _decimate_events(events: np.ndarray, min_gap: int) -> np.ndarray:
    """Keep only events at least `min_gap` bars after the last kept event, so
    horizon-h forward windows never overlap."""
    kept = np.zeros_like(events, dtype=bool)
    last = -min_gap
    for index in np.flatnonzero(events):
        if index - last >= min_gap:
            kept[index] = True
            last = index
    return kept


def resample_ohlcv(
    frame: pd.DataFrame, rule_seconds: int, *, bar_seconds: int
) -> pd.DataFrame:
    """Causal OHLCV resample of one symbol's CLOSE-labeled bars.

    Right-closed/right-labeled: an output bar labeled T aggregates source
    bars with close-timestamps in (T - rule, T] — only bars already completed
    at T, so appending source bars never changes earlier output (prefix
    property, pinned by test). The trailing bucket is dropped unless the last
    source bar lands exactly on its label (an in-progress bucket is not a
    completed bar). Both dataset feeds label bars by CLOSE time; an
    open-labeled source would silently leak one bar — keep it that way.

    Non-OHLCV columns (merged feature columns like `funding`) are carried
    with last-value aggregation — the value as-of the bucket close, matching
    merge_asof(direction="backward") semantics — and coerced to numeric:
    merge_features emits object dtype with Nones, and builders must see the
    same dtype whether or not the timeframe resamples (identity path too).
    Flow-like features wanting `sum` aggregation are not supported.
    """
    if rule_seconds <= 0 or bar_seconds <= 0 or rule_seconds % bar_seconds != 0:
        raise ValueError(
            f"rule_seconds ({rule_seconds}) must be a positive multiple of "
            f"bar_seconds ({bar_seconds})"
        )
    bar_columns = ["timestamp", "symbol", "open", "high", "low", "close", "volume"]
    extras = [c for c in frame.columns if c not in bar_columns]
    if rule_seconds == bar_seconds:
        out = frame.reset_index(drop=True)
        if extras:
            out = out.copy()
            for column in extras:
                out[column] = pd.to_numeric(out[column], errors="coerce")
        return out
    symbol = str(frame["symbol"].iloc[0]) if len(frame) else ""
    indexed = frame.set_index(pd.to_datetime(frame["timestamp"], utc=True))
    for column in extras:
        indexed[column] = pd.to_numeric(indexed[column], errors="coerce")
    resampled = indexed.resample(f"{rule_seconds}s", closed="right", label="right").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
        | dict.fromkeys(extras, "last")
    )
    resampled = resampled.dropna(subset=["close"])
    if len(resampled) and len(indexed):
        last_source = indexed.index[-1]
        if resampled.index[-1] != last_source:
            resampled = resampled.iloc[:-1]
    out = resampled.reset_index().rename(columns={"index": "timestamp"})
    out["symbol"] = symbol
    return out[bar_columns + extras]


def bh_qvalues(pvalues: Sequence[float]) -> list[float]:
    """Benjamini-Hochberg q-values (monotone step-up): q_(i) = min over
    j >= i of (m * p_(j) / j), clipped to 1, returned in input order."""
    m = len(pvalues)
    if m == 0:
        return []
    order = np.argsort(pvalues)
    ranked = np.asarray(pvalues, dtype=float)[order]
    scaled = ranked * m / np.arange(1, m + 1)
    qvals = np.minimum.accumulate(scaled[::-1])[::-1].clip(max=1.0)
    out = np.empty(m, dtype=float)
    out[order] = qvals
    return out.tolist()


def _t_to_pvalue(t: float) -> float:
    """Two-sided p from the normal approximation of a t-stat — the right
    trial accounting for sign-inferred direction (inferring the side from the
    sign does not double-count under a two-sided p)."""
    return math.erfc(abs(t) / math.sqrt(2.0))


def _variance_ratio(returns: np.ndarray, q: int) -> float | None:
    if len(returns) <= q or q < 2:
        return None
    base_var = float(returns.var(ddof=1))
    if base_var <= 0:
        return None
    agg = np.convolve(returns, np.ones(q), mode="valid")
    return float(agg.var(ddof=1) / (q * base_var))


def dataset_fingerprint(
    frame: pd.DataFrame,
    *,
    bar_seconds: int,
    fee_bps: float = 5.0,
    slippage_bps: float = 3.5,
) -> dict[str, Any]:
    """Compact market map read BEFORE any scan row: cost-to-range kills
    short-horizon families outright, return dependence routes budget toward
    continuation vs reversal families (a VR rejection of a random walk is a
    routing hint, not proof of stationary mean reversion), and a dominant
    regime quadrant warns that any edge found is regime-conditional."""
    timestamps = pd.to_datetime(frame["timestamp"], utc=True)
    close = frame["close"].astype(float).to_numpy()
    high = frame["high"].astype(float).to_numpy()
    low = frame["low"].astype(float).to_numpy()
    n = len(close)
    if n < 3:
        return {"span": {"bars": n}, "note": "too few bars to fingerprint"}
    gaps = timestamps.diff().dt.total_seconds().to_numpy()[1:]
    missing = int(np.round(gaps[gaps > bar_seconds] / bar_seconds - 1).sum())
    returns = np.diff(np.log(close))
    abs_returns = np.abs(returns)
    round_trip_bps = 2 * (fee_bps + slippage_bps)
    median_range_bps = float(np.median((high - low) / close) * 1e4)
    close_series = pd.Series(close)
    sma200 = close_series.rolling(min(200, max(n // 4, 2))).mean()
    vol24 = pd.Series(returns).rolling(min(24, max(n // 8, 2))).std()
    trend_up = (close_series >= sma200).to_numpy()[1:]
    vol_high = (vol24 >= vol24.median()).to_numpy()
    size = min(len(trend_up), len(vol_high))
    trend_up, vol_high = trend_up[:size], vol_high[:size]
    quadrants = {
        "up_high_vol": float((trend_up & vol_high).mean()),
        "up_low_vol": float((trend_up & ~vol_high).mean()),
        "down_high_vol": float((~trend_up & vol_high).mean()),
        "down_low_vol": float((~trend_up & ~vol_high).mean()),
    }
    return {
        "span": {
            "first_ts": str(timestamps.iloc[0]),
            "last_ts": str(timestamps.iloc[-1]),
            "bars": n,
            "days": round((timestamps.iloc[-1] - timestamps.iloc[0]).days, 1),
        },
        "gaps": {
            "missing_bars": missing,
            "max_gap_bars": int(np.max(gaps) // bar_seconds) if len(gaps) else 0,
        },
        "cost": {
            "fee_bps": fee_bps,
            "slippage_bps": slippage_bps,
            "round_trip_bps": round_trip_bps,
            "median_bar_range_bps": round(median_range_bps, 2),
            "cost_to_range": round(round_trip_bps / max(median_range_bps, 1e-9), 3),
            "note": (
                "cost_to_range > ~0.3 makes short-horizon families unviable after costs"
            ),
        },
        "returns": {
            "acf1": float(pd.Series(returns).autocorr(1)),
            "vr4": _variance_ratio(returns, 4),
            "vr24": _variance_ratio(returns, 24),
            "abs_acf1": float(pd.Series(abs_returns).autocorr(1)),
        },
        "regimes": {
            **{k: round(v, 3) for k, v in quadrants.items()},
            "note": (
                "single-regime data; expect regime-dependent edges"
                if max(quadrants.values()) > 0.5
                else None
            ),
        },
    }


def event_path_stats(
    frame: pd.DataFrame,
    events: np.ndarray,
    *,
    horizon: int,
    direction: str,
    atr_period: int = 14,
) -> dict[str, Any]:
    """Path shape after DECIMATED events: median favorable/adverse excursion
    in ATR units and how fast each arrives — enough to shortlist an exit
    family (target vs fixed-time/trail vs time-stop) without optimizing
    exits. Endpoint returns alone can't tell you how to trade a signal."""
    close = frame["close"].astype(float).to_numpy()
    high = frame["high"].astype(float).to_numpy()
    low = frame["low"].astype(float).to_numpy()
    atr = wilder_atr(frame, atr_period).to_numpy()
    n = len(close)
    idx = np.flatnonzero(events)
    idx = idx[(idx + horizon < n) & (idx >= atr_period)]
    if len(idx) == 0:
        return {"n": 0, "note": "no events with a full forward window"}
    high_w = np.lib.stride_tricks.sliding_window_view(high[1:], horizon)[idx]
    low_w = np.lib.stride_tricks.sliding_window_view(low[1:], horizon)[idx]
    entry = close[idx]
    unit = np.maximum(atr[idx], 1e-12)
    if direction == "short":
        favorable = (entry - low_w.min(axis=1)) / unit
        adverse = (high_w.max(axis=1) - entry) / unit
        bars_to_fav = low_w.argmin(axis=1) + 1
        bars_to_adv = high_w.argmax(axis=1) + 1
    else:
        favorable = (high_w.max(axis=1) - entry) / unit
        adverse = (entry - low_w.min(axis=1)) / unit
        bars_to_fav = high_w.argmax(axis=1) + 1
        bars_to_adv = low_w.argmin(axis=1) + 1
    mfe = float(np.median(favorable))
    mae = float(np.median(adverse))
    bars_to_mfe = float(np.median(bars_to_fav))
    ratio = mfe / max(mae, 1e-9)
    if ratio < 1.25:
        exit_hint = "time_stop_only"
    elif bars_to_mfe <= horizon / 3:
        exit_hint = "target"
    else:
        exit_hint = "fixed_time_or_trail"
    return {
        "n": int(len(idx)),
        "mfe_atr_median": round(mfe, 3),
        "mae_atr_median": round(mae, 3),
        "mfe_mae_ratio": round(ratio, 3),
        "bars_to_mfe_median": bars_to_mfe,
        "bars_to_mae_median": float(np.median(bars_to_adv)),
        "horizon": horizon,
        "exit_hint": exit_hint,
    }


def apply_bh_verdicts(
    rows: list[dict[str, Any]],
    *,
    q_threshold: float = 0.10,
    min_folds_agree: int = 3,
) -> None:
    """Annotate scan rows in place with BH q-values and verdicts over ONE
    test family (all rows passed in). promote = q <= threshold AND fold
    stability; candidate = |t| >= 2 (report continuity)."""
    qvals = bh_qvalues([row["p_value"] for row in rows])
    for row, q in zip(rows, qvals, strict=True):
        row["q_value"] = round(float(q), 4)
        t = row["t_stat_vs_drift"]
        promote = (
            q <= q_threshold
            and bool(row.get("fold_stable"))
            and int(row.get("folds_agreeing") or 0) >= min_folds_agree
        )
        # Short-window families cap at probation: fewer events by construction
        # means the same q is weaker evidence — forward paper adjudicates.
        if row.get("window_days"):
            promote = False
        # PROBATION: eligibility for reduced-size paper deployment with
        # pre-registered kill/graduate criteria. Forward paper is the honest
        # holdout — that is why this tier is allowed to be looser. Paths:
        # (a) near-miss whose edge is alive NOW; (b) regime-conditional edge
        # in the CURRENT regime; (c) declared recent-window survivor.
        t_recent = row.get("t_recent")
        alive_now = t_recent is not None and abs(t_recent) >= 2 and t_recent * t > 0
        probation = not promote and (
            (q <= 0.20 and int(row.get("folds_agreeing") or 0) >= 2 and alive_now)
            or (
                bool(row.get("in_current_regime"))
                and q <= 0.15
                and int(row.get("n") or 0) >= 20
            )
            or (bool(row.get("window_days")) and q <= q_threshold)
        )
        row["verdict"] = (
            "promote"
            if promote
            else "probation"
            if probation
            else "candidate"
            if abs(t) >= 2
            else None
        )


def holdout_event_study(
    frame: pd.DataFrame,
    *,
    signal: str,
    horizon: int,
    direction: str,
    cutoff_ts: Any,
    bar_seconds: int,
    timeframe_seconds: int | None = None,
    min_events: int = 10,
    t_threshold: float = 1.0,
    extra_defs: Mapping[str, SignalDef] | None = None,
) -> dict[str, Any]:
    """One pre-registered confirmation of a FROZEN scan candidate on the
    reserved holdout tail. The bar is deliberately lower than the scan's
    q-gate (directional t >= 1, n >= 10) because this is a single declared
    test, not a search — and it is spendable ONCE per candidate (the scan
    ledger remembers). `extra_defs` extends the lookup to validated
    workspace signals so composed candidates confirm the same way.

    The signal is computed over the FULL series so holdout events get proper
    indicator warmup; the causal prefix property means this cannot leak."""
    defs = {**signal_defs(), **(extra_defs or {})}
    if signal not in defs:
        raise KeyError(
            f"signal {signal!r} not in the canonical library or workspace "
            f"signals; available: {sorted(defs)}"
        )
    if direction not in {"long", "short"}:
        raise ValueError(
            f"a frozen candidate is directional — direction must be long|short, "
            f"got {direction!r}"
        )
    tf_seconds = timeframe_seconds or bar_seconds
    bars = resample_ohlcv(frame, tf_seconds, bar_seconds=bar_seconds)
    close = bars["close"].astype(float).to_numpy()
    n = len(close)
    spec = defs[signal]
    sig = (
        spec.build(bars).fillna(False).astype(bool).to_numpy()
        if n >= spec.min_bars
        else np.zeros(n, dtype=bool)
    )
    cutoff = pd.Timestamp(cutoff_ts)
    in_tail = (pd.to_datetime(bars["timestamp"], utc=True) > cutoff).to_numpy()
    if horizon <= 0 or horizon >= n:
        raise ValueError(f"horizon {horizon} out of range for {n} resampled bars")
    fwd = np.log(close[horizon:] / close[:-horizon])
    tail_mask = in_tail[: n - horizon]
    tail_fwd = fwd[tail_mask]
    events = _decimate_events(sig[: n - horizon] & tail_mask, horizon)
    n_raw = int((sig[: n - horizon] & tail_mask).sum())
    n_events = int(events.sum())
    side = 1 if direction == "long" else -1
    if n_events == 0 or len(tail_fwd) == 0:
        return {
            "signal": signal,
            "horizon": horizon,
            "direction": direction,
            "cutoff_ts": str(cutoff),
            "n": 0,
            "n_raw": n_raw,
            "verdict": "insufficient",
            "read": "no holdout events yet — the tail grows as new data accrues",
        }
    event_returns = fwd[events]
    drift = float(tail_fwd.mean())
    mean_r = float(event_returns.mean())
    std_r = float(event_returns.std(ddof=1)) if n_events > 1 else 0.0
    sem = std_r / math.sqrt(n_events) if n_events > 1 and std_r > 0 else float("inf")
    t = (mean_r - drift) / sem if math.isfinite(sem) else 0.0
    if n_events < min_events:
        verdict = "insufficient"
    elif side * t >= t_threshold:
        verdict = "confirmed"
    else:
        verdict = "failed"
    return {
        "signal": signal,
        "timeframe_seconds": tf_seconds,
        "horizon": horizon,
        "direction": direction,
        "cutoff_ts": str(cutoff),
        "n": n_events,
        "n_raw": n_raw,
        "mean_fwd_return": mean_r,
        "drift_baseline": drift,
        "t_stat_vs_drift": float(t),
        "hit_rate": float((side * event_returns > 0).mean()),
        "verdict": verdict,
        "read": {
            "confirmed": "the frozen candidate held up on data the scan never "
            "saw — proceed to the minimal strategy build",
            "failed": "the edge did not survive the holdout tail — the "
            "candidate is dead; do not retune it against this tail",
            "insufficient": f"fewer than {min_events} non-overlapping holdout "
            "events — wait for more data rather than lowering the bar",
        }[verdict],
    }


_DEFAULT_SCAN_HORIZONS = {
    3600: [1, 4, 12, 24, 48],
    14400: [1, 3, 6, 12, 24],
    86400: [1, 2, 3, 5, 10],
}
_GENERIC_SCAN_HORIZONS = [1, 2, 4, 8, 16]


def _fold_stability(
    events: np.ndarray,
    fwd: np.ndarray,
    reference_sign: float,
    *,
    folds: int,
    min_fold_events: int,
) -> tuple[list[float] | None, int, bool]:
    """Chronological fold deltas (event mean minus the fold's own drift) and
    the count agreeing in sign with the full-sample effect. Any fold below
    min_fold_events decimated events → insufficient evidence of stability."""
    bounds = np.linspace(0, len(fwd), folds + 1, dtype=int)
    deltas: list[float] = []
    for start, end in zip(bounds[:-1], bounds[1:], strict=True):
        fold_events = events[start:end]
        fold_fwd = fwd[start:end]
        if int(fold_events.sum()) < min_fold_events or len(fold_fwd) == 0:
            return None, 0, False
        deltas.append(float(fold_fwd[fold_events].mean()) - float(fold_fwd.mean()))
    agreeing = int(sum(1 for d in deltas if np.sign(d) == reference_sign != 0))
    return deltas, agreeing, True


def scan_signals(
    frame: pd.DataFrame,
    horizons: list[int] | None = None,
    *,
    bar_seconds: int = 3600,
    timeframes: list[str] | None = None,
    holdout_fraction: float = 0.15,
    min_events: int = 30,
    min_fold_events: int = 5,
    folds: int = 4,
    min_folds_agree: int = 3,
    q_threshold: float = 0.10,
    fee_bps: float = 5.0,
    slippage_bps: float = 3.5,
    extra_signals: Sequence[SignalDef] = (),
    include_canonical: bool = True,
    condition_regime: bool = False,
) -> dict[str, Any]:
    """Event-study EVERY canonical library trigger against one symbol's bars
    — across timeframes, in a single pass — the breadth tool that replaces
    hand-rewriting `precompute()` once per trigger idea. `extra_signals`
    (validated workspace defs) join the sweep as first-class family members:
    same decimation, same folds, same pooled BH — composed trials pay the
    same multiple-testing bill as canonical ones.

    Discipline built in:
    - A holdout tail (`holdout_fraction`, default the final 15%) is reserved
      BEFORE anything is measured — the scan never sees it; confirm a frozen
      candidate later via `holdout_event_study`, once.
    - Events are DECIMATED to horizon spacing so forward windows never
      overlap (clustered triggers otherwise inflate t-stats — a pure random
      walk shows spurious stable edges without this).
    - Multiple testing is controlled by Benjamini-Hochberg q-values over all
      tests in the scan; `promote` requires q <= q_threshold AND sign
      agreement in >= min_folds_agree of `folds` chronological folds.
    - Direction is classified from the SIGN of the t-stat (t <= -2 short,
      t >= +2 long) — a trigger that fails one side surfaces as a candidate
      for the other instead of a dead end.
    - Promoted rows carry `path_stats` (MFE/MAE shape → exit-family hint)
      and `edge_by_horizon` (decay across sibling horizons).
    """
    from wayfinder_paths.jobs.execution.primitives import bar_interval_seconds

    bars_full = frame.reset_index(drop=True)
    n_full = len(bars_full)
    cutoff_index = int(n_full * (1 - holdout_fraction))
    base = bars_full.iloc[:cutoff_index].reset_index(drop=True)
    cutoff_ts = str(base["timestamp"].iloc[-1]) if len(base) else None
    fingerprint = dataset_fingerprint(
        base, bar_seconds=bar_seconds, fee_bps=fee_bps, slippage_bps=slippage_bps
    )
    tf_specs: list[tuple[str, int]] = []
    timeframes_skipped: list[dict[str, str]] = []
    for tf in timeframes or [f"{bar_seconds}s"]:
        seconds = bar_interval_seconds(tf)
        if not seconds or seconds < bar_seconds or seconds % bar_seconds != 0:
            timeframes_skipped.append(
                {"timeframe": str(tf), "reason": "not a multiple of the base bar"}
            )
            continue
        tf_specs.append((str(tf), seconds))
    extra_names = {spec.name for spec in extra_signals}
    rows: list[dict[str, Any]] = []
    tests_run = 0
    tests_skipped = 0
    horizons_used: dict[str, list[int]] = {}
    frames_by_tf: dict[str, pd.DataFrame] = {}
    events_cache: dict[tuple[str, str, int], np.ndarray] = {}
    regime_arrays: dict[str, Any] = {}
    regime_now: str | None = None
    for tf_name, tf_seconds in tf_specs:
        bars = resample_ohlcv(base, tf_seconds, bar_seconds=bar_seconds)
        frames_by_tf[tf_name] = bars
        if condition_regime:
            from wayfinder_paths.jobs.indicators import (
                REGIME_LABELS,
                classify_regimes,
            )

            labels = classify_regimes(bars)
            regime_arrays[tf_name] = labels.to_numpy()
            tail = labels.dropna()
            if regime_now is None and len(tail):
                regime_now = str(tail.iloc[-1])
        close = bars["close"].astype(float).to_numpy()
        n = len(close)
        signals = build_signal_frame(
            bars, extra_signals, include_canonical=include_canonical
        )
        tf_horizons = sorted(
            {int(h) for h in horizons}
            if horizons
            else set(_DEFAULT_SCAN_HORIZONS.get(tf_seconds, _GENERIC_SCAN_HORIZONS))
        )
        horizons_used[tf_name] = tf_horizons
        for h in tf_horizons:
            if h <= 0 or h >= n:
                tests_skipped += 1
                continue
            if n // h < min_events:
                # Decimated-event ceiling can't reach the sample gate.
                tests_skipped += 1
                continue
            fwd = np.log(close[h:] / close[:-h])
            drift = float(fwd.mean())

            def _stats_row(
                events: np.ndarray,
                spec: SignalDef,
                *,
                h: int = h,
                fwd: np.ndarray = fwd,
                drift: float = drift,
                n: int = n,
                cell_min_events: int = min_events,
                tf_name: str = tf_name,
                extra: dict[str, Any] | None = None,
            ) -> dict[str, Any] | None:
                n_events = int(events.sum())
                if n_events < cell_min_events:
                    return None
                event_returns = fwd[events]
                mean_r = float(event_returns.mean())
                std_r = float(event_returns.std(ddof=1))
                if std_r <= 0:
                    return None
                t = (mean_r - drift) / (std_r / math.sqrt(n_events))
                fold_deltas, agreeing, measurable = _fold_stability(
                    events,
                    fwd,
                    float(np.sign(mean_r - drift)),
                    folds=folds,
                    min_fold_events=min_fold_events,
                )
                # Recency diagnostics: same t construction on each half of the
                # sample. No gate reads these except the probation tier — they
                # exist so a live edge and a decayed one stop looking alike.
                mid = (n - h) // 2
                halves: dict[str, float | None] = {"t_early": None, "t_recent": None}
                for key, half in (
                    ("t_early", events[:mid]),
                    ("t_recent", events[mid:]),
                ):
                    half_returns = (
                        fwd[:mid][half] if key == "t_early" else fwd[mid:][half]
                    )
                    if len(half_returns) >= max(8, cell_min_events // 3):
                        h_std = float(half_returns.std(ddof=1))
                        if h_std > 0:
                            halves[key] = float(
                                (float(half_returns.mean()) - drift)
                                / (h_std / math.sqrt(len(half_returns)))
                            )
                sign = float(np.sign(t)) or 1.0
                if halves["t_early"] is None or halves["t_recent"] is None:
                    recency_trend = None
                else:
                    delta = sign * (halves["t_recent"] - halves["t_early"])
                    recency_trend = (
                        "strengthening"
                        if delta >= 0.75
                        else "decaying"
                        if delta <= -0.75
                        else "stable"
                    )
                return {
                    "signal": spec.name,
                    "family": spec.family,
                    "library": (
                        "workspace" if spec.name in extra_names else "canonical"
                    ),
                    "description": spec.description,
                    "timeframe": tf_name,
                    "horizon": h,
                    "n": n_events,
                    "mean_fwd_return": mean_r,
                    "drift_baseline": drift,
                    "t_stat_vs_drift": float(t),
                    "p_value": _t_to_pvalue(float(t)),
                    "direction": ("short" if t <= -2 else "long" if t >= 2 else None),
                    "fold_deltas": fold_deltas,
                    "folds_agreeing": agreeing,
                    "fold_stable": bool(measurable and agreeing >= min_folds_agree),
                    "t_early": halves["t_early"],
                    "t_recent": halves["t_recent"],
                    "recency_trend": recency_trend,
                    **(extra or {}),
                }

            # Iterate the SAME def set the frame was built from: a campaign
            # frame has only workspace columns, so scoring the canonical
            # library against it would KeyError (hit live 2026-07-26).
            library = SIGNAL_LIBRARY if include_canonical else ()
            for spec in (*library, *extra_signals):
                sig = signals[spec.name].to_numpy()
                events = _decimate_events(sig[: n - h], h)
                n_raw = int(sig[: n - h].sum())
                row = _stats_row(events, spec)
                if row is None:
                    if int(events.sum()) >= min_events:
                        continue  # zero-variance cell
                    continue
                tests_run += 1
                row["n_raw"] = n_raw
                events_cache[(tf_name, spec.name, h)] = events
                rows.append(row)
                if condition_regime and regime_arrays.get(tf_name) is not None:
                    labels_arr = regime_arrays[tf_name]
                    for label in REGIME_LABELS:
                        mask = labels_arr[: n - h] == label
                        r_events = _decimate_events(sig[: n - h] & mask, h)
                        r_row = _stats_row(
                            r_events,
                            spec,
                            cell_min_events=max(15, min_events // 2),
                            extra={
                                "regime": label,
                                "in_current_regime": label == regime_now,
                            },
                        )
                        if r_row is None:
                            continue
                        tests_run += 1
                        rows.append(r_row)
    apply_bh_verdicts(rows, q_threshold=q_threshold, min_folds_agree=min_folds_agree)
    # Path stats for every |t|>=2 candidate (not just promoted): pooled
    # multi-symbol BH in signal_scan_job can shift verdicts after this
    # returns, and the set is small enough to be cheap.
    for row in rows:
        if not row["direction"] or row.get("regime"):
            continue
        direction = row["direction"]
        row["path_stats"] = event_path_stats(
            frames_by_tf[row["timeframe"]],
            events_cache[(row["timeframe"], row["signal"], row["horizon"])],
            horizon=row["horizon"],
            direction=direction,
        )
        row["edge_by_horizon"] = {
            sibling["horizon"]: {
                "t": round(sibling["t_stat_vs_drift"], 2),
                "mean_minus_drift": sibling["mean_fwd_return"]
                - sibling["drift_baseline"],
            }
            for sibling in rows
            if sibling["signal"] == row["signal"]
            and sibling["timeframe"] == row["timeframe"]
            and not sibling.get("regime")
        }
    rows.sort(key=lambda r: -abs(r["t_stat_vs_drift"]))
    candidates = [r for r in rows if r["direction"]]
    promoted = [r for r in rows if r["verdict"] == "promote"]
    expected_lucky = round(tests_run * 0.05, 1)
    return {
        "signals_tested": len(SIGNAL_LIBRARY) + len(extra_signals),
        "workspace_signals": sorted(extra_names),
        "timeframes": [name for name, _ in tf_specs],
        "timeframes_skipped": timeframes_skipped,
        "horizons": horizons_used,
        "tests_run": tests_run,
        "tests_skipped_insufficient_data": tests_skipped,
        "current_regime": regime_now,
        "expected_lucky_passes": expected_lucky,
        "candidates": candidates,
        "promoted": promoted,
        "top_by_abs_t": rows[:5],
        "holdout": {
            "fraction": holdout_fraction,
            "cutoff_ts": cutoff_ts,
            "train_bars": len(base),
            "holdout_bars": n_full - len(base),
        },
        "fingerprint": fingerprint,
        # Full row list for pooled multi-symbol BH in signal_scan_job —
        # q-values are only meaningful over the COMPLETE test family.
        # Stripped from the persisted artifact.
        "_all_rows": rows,
        "read": (
            f"{len(promoted)} of {tests_run} tests PROMOTED "
            f"(q<={q_threshold} + >={min_folds_agree}/{folds} fold sign "
            f"agreement); {len(candidates)} cleared raw |t|>=2 vs "
            f"~{expected_lucky} expected by luck. Take at most 3 promoted "
            "cards forward (CORE/ADJACENT/DIVERGENT), build the minimal "
            "fixed-time-exit strategy at the measured horizon first, and "
            "confirm a FROZEN candidate on the holdout tail (holdout_check) "
            "exactly once before trusting it"
            if promoted
            else f"0 of {tests_run} tests cleared the q<={q_threshold} + fold "
            "stability gate on this symbol — no canonical trigger has "
            "standalone timing alpha here; a complete trade SYSTEM can still "
            "work (gates + exits + regime), but new signal mining on this "
            "series is unlikely to pay"
        ),
    }


def rank_ic(
    frames: dict[str, pd.DataFrame],
    column: str,
    horizons: list[int] | None = None,
) -> dict[str, Any]:
    """Does a cross-sectional RANKING column predict relative forward returns?
    The basket-strategy analogue of event_study: baskets trade the ranking
    (long the top, short the bottom), so the right pre-build test is the rank
    information coefficient, not per-symbol event returns.

    For each horizon h and each timestamp with >= 4 ranked symbols: Spearman
    rank correlation between the column's cross-sectional ranks and the
    ranks of forward log-returns. Reports mean IC, a t-stat over the period
    ICs, the period count, and sign stability across the two halves of the
    sample. Edge bar: |t| >= 2, n >= 30 periods, and both halves agree on
    sign — testing many columns inflates false positives, same as signals.
    """
    horizons = horizons or [1, 2, 5, 10]
    aligned: dict[str, pd.DataFrame] = {}
    for symbol, frame in frames.items():
        if column not in frame.columns:
            raise KeyError(
                f"column {column!r} not in frame for {symbol}; available: "
                f"{sorted(frame.columns)}"
            )
        sub = frame[["timestamp", column, "close"]].copy()
        sub["close"] = sub["close"].astype(float)
        aligned[symbol] = sub.set_index("timestamp")
    # Fail loud on inputs where rank-IC is undefined by construction —
    # the silent alternative is n=0 "no periods" that reads like a data
    # problem and wedges lanes (2026-07-27: btc_trend is panel-wide, so
    # three agent wakes chased a phantom merge bug).
    all_nan = sorted(
        symbol for symbol, sub in aligned.items() if sub[column].isna().all()
    )
    if all_nan:
        raise ValueError(
            f"column {column!r} is entirely NaN for {all_nan} — pair-wise "
            "columns (ratioz_<sym>, corr_<sym>) have no self-value, so the "
            "full cross-section can never rank. Use the basket-relative "
            "column (ratioz_basket*) for rank-IC instead."
        )
    wide = pd.DataFrame({symbol: sub[column] for symbol, sub in aligned.items()})
    varying = (wide.nunique(axis=1, dropna=True) > 1).sum()
    if len(wide) > 0 and varying < max(4, 0.05 * len(wide)):
        raise ValueError(
            f"column {column!r} is cross-sectionally constant (same value "
            "for every symbol per bar — a panel-wide/exogenous series like "
            "btc_trend or breadth). Rank-IC is undefined for it. Test it as "
            "a CONDITIONING variable instead: signal-scan --condition-regime "
            "or event-study conditioned on its level."
        )
    per_horizon = []
    any_edge = False
    for h in sorted({int(h) for h in horizons}):
        if h <= 0:
            continue
        ics: list[float] = []
        # union of timestamps, evaluated cross-sectionally
        index = sorted(set().union(*(set(f.index) for f in aligned.values())))
        for i, ts in enumerate(index):
            if i + h >= len(index):
                break
            fwd_ts = index[i + h]
            scores: list[float] = []
            fwd: list[float] = []
            for frame in aligned.values():
                if ts not in frame.index or fwd_ts not in frame.index:
                    continue
                value = frame.at[ts, column]
                c_now = frame.at[ts, "close"]
                c_fwd = frame.at[fwd_ts, "close"]
                if pd.isna(value) or pd.isna(c_now) or pd.isna(c_fwd) or c_now <= 0:
                    continue
                scores.append(float(value))
                fwd.append(math.log(float(c_fwd) / float(c_now)))
            if len(scores) < 4:
                continue
            rank_scores = pd.Series(scores).rank()
            rank_fwd = pd.Series(fwd).rank()
            ic = float(rank_scores.corr(rank_fwd))
            if math.isfinite(ic):
                ics.append(ic)
        n = len(ics)
        if n == 0:
            per_horizon.append({"horizon": h, "n": 0, "verdict": "no periods"})
            continue
        mean_ic = float(np.mean(ics))
        std_ic = float(np.std(ics, ddof=1)) if n > 1 else 0.0
        t = mean_ic / (std_ic / math.sqrt(n)) if n > 1 and std_ic > 0 else 0.0
        half = n // 2
        first_sign = float(np.mean(ics[:half])) if half else 0.0
        second_sign = float(np.mean(ics[half:])) if half else 0.0
        stable = bool(
            half >= 5 and first_sign * second_sign > 0
        )  # both halves agree on sign
        edge = bool(abs(t) >= 2.0 and n >= 30 and stable)
        any_edge = any_edge or edge
        per_horizon.append(
            {
                "horizon": h,
                "n": n,
                "mean_ic": mean_ic,
                "t_stat": float(t),
                "ic_first_half": first_sign,
                "ic_second_half": second_sign,
                "sign_stable": stable,
                "edge": edge,
                "note": "insufficient sample (n<30)" if n < 30 else None,
            }
        )
    return {
        "column": column,
        "horizons": per_horizon,
        "has_edge": any_edge,
        "read": (
            "the ranking carries cross-sectional information at one or more "
            "horizons — worth building the basket and validating"
            if any_edge
            else "no horizon shows a stable rank IC with |t|>=2 and n>=30 — "
            "the ranking does not order future returns; change the ranking "
            "signal, not the basket parameters"
        ),
        "multiple_testing_note": (
            "testing many ranking columns inflates false positives — at "
            "|t|>=2 roughly 1 in 20 random rankings looks good by chance"
        ),
    }


# ── pair admission gate (pure: DataFrames in, dict out) ──────────────────────


def pair_admission_gate(
    prices: pd.DataFrame,
    *,
    bar_seconds: int,
    funding: pd.DataFrame | None = None,
    fee_bps: float = 5.0,
    slippage_bps: float = 3.5,
    z_entry: float = 2.0,
    z_exit: float = 0.5,
    half_life_band_hours: tuple[float, float] = (12.0, 480.0),
    stability_window_days: int = 75,
    min_stability_fraction: float = 0.6,
    min_crossings_per_90d: int = 15,
    min_days: int = 365,
) -> dict[str, Any]:
    """The hard admission gate for any two-legged (pair/spread) idea.

    REJECT if any hard check fails (cointegration both directions, rolling
    stability, half-life band, cost hurdle, data sufficiency); MARGINAL when
    only advisory checks fail (hurst, crossings); PASS otherwise. A REJECT is
    a successful outcome — it saves days of tuning a spread that cannot work.
    """
    if prices.shape[1] != 2:
        raise ValueError("pair_admission_gate expects exactly 2 price columns")
    sym_a, sym_b = [str(c) for c in prices.columns]
    clean = prices.dropna()
    a = clean[sym_a].astype(float).to_numpy()
    b = clean[sym_b].astype(float).to_numpy()
    n_bars = len(clean)
    days = n_bars * bar_seconds / 86_400
    bars_per_day = 86_400 / bar_seconds

    checks: list[dict[str, Any]] = []

    # 1) data sufficiency (hard)
    data_ok = days >= min_days
    checks.append(
        {
            "name": "data_sufficiency",
            "hard": True,
            "pass": bool(data_ok),
            "value": {"bars": n_bars, "days": round(days, 1)},
            "threshold": {"min_days": min_days},
            "detail": "multiple regimes needed; one trending regime cannot "
            "validate mean reversion",
        }
    )

    # 2) cointegration, both directions (hard)
    eg = engle_granger_both(a, b)
    checks.append(
        {
            "name": "engle_granger_both_directions",
            "hard": True,
            "pass": eg["pass"],
            "value": {
                "ab_t": round(eg["ab"]["t_stat"], 3),
                "ba_t": round(eg["ba"]["t_stat"], 3),
            },
            "threshold": EG_CRITICAL["5%"],
            "detail": "log-price residual ADF t vs MacKinnon 5% critical, "
            "both regressions",
        }
    )
    hedge = eg["ab"]["hedge_ratio"]

    # spread on the estimated hedge ratio (fallback 1.0 when degenerate)
    ratio = hedge if hedge and math.isfinite(hedge) and hedge > 0 else 1.0
    spread = np.log(a) - ratio * np.log(b)

    # 3) half-life within tradeable band (hard)
    hl_bars = ou_half_life(spread)
    hl_hours = hl_bars * bar_seconds / 3600 if math.isfinite(hl_bars) else None
    lo, hi = half_life_band_hours
    hl_ok = hl_hours is not None and lo <= hl_hours <= hi
    checks.append(
        {
            "name": "half_life",
            "hard": True,
            "pass": bool(hl_ok),
            "value": round(hl_hours, 1) if hl_hours is not None else None,
            "band_hours": [lo, hi],
            "detail": "OU half-life of the hedge-ratio spread; sets lookback "
            "(3-5x) and time stop (2-3x)",
        }
    )

    # 4) rolling stability (hard)
    window_bars = max(50, int(stability_window_days * bars_per_day))
    stability = rolling_stability(
        np.log(a),
        np.log(b),
        window_bars=window_bars,
        step_bars=max(1, window_bars // 3),
    )
    stab_ok = (
        stability["windows"] >= 3
        and stability["pass_fraction"] >= min_stability_fraction
    )
    checks.append(
        {
            "name": "rolling_stability",
            "hard": True,
            "pass": bool(stab_ok),
            "value": round(stability["pass_fraction"], 2),
            "threshold": min_stability_fraction,
            "detail": f"cointegrated in {int(stability['pass_fraction'] * stability['windows'])}"
            f"/{stability['windows']} {stability_window_days}d windows",
        }
    )

    # 5) cost hurdle (hard)
    sigma = float(
        np.std(
            spread
            - pd.Series(spread)
            .rolling(max(20, int(4 * hl_bars)) if math.isfinite(hl_bars) else 20)
            .mean()
            .to_numpy(),
            ddof=0,
        )
    )
    if not math.isfinite(sigma) or sigma == 0:
        sigma = float(np.std(spread, ddof=0))
    hurdle = cost_hurdle(
        sigma,
        z_entry=z_entry,
        z_exit=z_exit,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    checks.append(
        {
            "name": "cost_hurdle",
            "hard": True,
            "pass": hurdle["pass"],
            "value": round(hurdle["multiple"], 2),
            "threshold": 3.0,
            "detail": f"expected capture {hurdle['expected_capture']:.4f} vs "
            f"{hurdle['round_trip_cost']:.4f} round-trip on 2 legs",
        }
    )

    # 6) hurst (advisory)
    hurst = hurst_exponent(spread)
    checks.append(
        {
            "name": "hurst",
            "hard": False,
            "pass": bool(hurst < 0.5),
            "value": round(hurst, 3),
            "threshold": 0.5,
            "detail": "spread Hurst exponent; >0.5 = trending spread",
        }
    )

    # 7) mean crossings (advisory)
    crossings = mean_crossings(spread)
    per_90d = crossings * BAR_90D_SECONDS / (n_bars * bar_seconds) if n_bars else 0
    checks.append(
        {
            "name": "mean_crossings",
            "hard": False,
            "pass": bool(per_90d >= min_crossings_per_90d),
            "value": round(per_90d, 1),
            "threshold": min_crossings_per_90d,
            "detail": "mean crossings per 90 days — trade opportunity frequency",
        }
    )

    # 8) funding carry (advisory, never a REJECT trigger)
    if (
        funding is not None
        and not funding.empty
        and sym_a in funding
        and sym_b in funding
    ):
        carry = net_funding_carry(funding[sym_a], funding[sym_b], hedge_ratio=ratio)
        checks.append(
            {
                "name": "funding_carry",
                "hard": False,
                "pass": True,
                "value": round(carry["annualized"], 4) if carry["available"] else None,
                "detail": f"annualized net carry long {sym_a} / short {sym_b}"
                if carry["available"]
                else "funding history unavailable",
            }
        )

    hard_fails = [c["name"] for c in checks if c["hard"] and not c["pass"]]
    soft_fails = [c["name"] for c in checks if not c["hard"] and not c["pass"]]
    verdict = "REJECT" if hard_fails else ("MARGINAL" if soft_fails else "PASS")

    if verdict == "REJECT":
        recommendation = (
            f"REJECT — {sym_a} and {sym_b} do not form a tradeable spread on "
            f"this data (failed: {', '.join(hard_fails)}). No parameter tuning "
            "rescues a spread that does not mean-revert; consider a "
            "funding-spread pair, a momentum/trend approach, or a different, "
            "statistically related pair."
        )
    elif verdict == "MARGINAL":
        recommendation = (
            f"MARGINAL — hard checks pass but {', '.join(soft_fails)} are weak; "
            "proceed only with reduced size and strict walk-forward validation."
        )
    else:
        recommendation = (
            "PASS — build with the suggested hedge ratio and half-life-derived "
            "lookback/time-stop, then validate out-of-sample as usual."
        )

    suggested = {
        "hedge_ratio": round(ratio, 4),
        "lookback_bars": int(4 * hl_bars) if math.isfinite(hl_bars) else None,
        "time_stop_bars": int(2.5 * hl_bars) if math.isfinite(hl_bars) else None,
        "z_entry": z_entry,
        "z_exit": z_exit,
    }
    return {
        "pair": [sym_a, sym_b],
        "n_bars": n_bars,
        "days": round(days, 1),
        "verdict": verdict,
        "checks": checks,
        "suggested": suggested,
        "recommendation": recommendation,
        "multiple_testing_note": (
            "at the 5% level ~1 in 20 random pairs passes cointegration alone; "
            "when scanning a universe require the stability check too and "
            "prefer fewer, stronger pairs"
        ),
    }


# ── I/O orchestrators (function-local imports; run via op_runner) ────────────


def _job_context(job_id: str, store: Any) -> tuple[Any, dict, Any, list[str], int]:
    from wayfinder_paths.jobs.execution.job import _load_job_yaml
    from wayfinder_paths.jobs.execution.primitives import (
        ExecutionSpec,
        bar_interval_seconds,
    )
    from wayfinder_paths.jobs.execution.validation import resolve_execution_spec

    root = store.job_dir(job_id)
    job_data = _load_job_yaml(root)
    spec_data, _ = resolve_execution_spec(root, job_data)
    spec = ExecutionSpec.from_dict(spec_data)
    params = dict(job_data.get("execution_params") or {})
    symbols = [
        str(s)
        for s in (params.get("symbols") or spec.data_contract.get("symbols") or [])
    ]
    bar_seconds = bar_interval_seconds(spec.data_contract.get("bar_interval")) or 3600
    return root, params, spec, symbols, bar_seconds


def _pair_prices(
    symbols: list[str],
    *,
    bar_interval: str,
    days: int,
    exchange: str,
    feed: Any | None,
) -> pd.DataFrame:
    import asyncio

    from wayfinder_paths.jobs.execution.ccxt_feed import fetch_ccxt_dataset_rows

    rows, _meta = asyncio.run(
        fetch_ccxt_dataset_rows(
            symbols, bar_interval, days=days, exchange_id=exchange, exchange=feed
        )
    )
    frame = pd.DataFrame(rows)
    pivot = frame.pivot_table(index="timestamp", columns="symbol", values="close")
    return pivot[symbols].dropna()


def _job_funding(root: Any, symbols: list[str]) -> pd.DataFrame | None:
    path = root / "state" / "features.jsonl"
    if not path.exists():
        return None
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("name") == "funding":
            rows.append(row)
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    pivot = frame.pivot_table(index="timestamp", columns="symbol", values="value")
    return pivot if all(s in pivot.columns for s in symbols) else None


def pair_check_job(
    job_id: str,
    *,
    symbols: list[str] | None = None,
    days: int = 720,
    bar_interval: str | None = None,
    exchange: str = "binance",
    fee_bps: float | None = None,
    slippage_bps: float | None = None,
    store: Any | None = None,
    feed: Any | None = None,
) -> dict[str, Any]:
    """Run the pair admission gate for a job — fetches long history (default
    2 years) and persists the report under results/research/pair_check/."""
    from wayfinder_paths.jobs.store import JobStore

    store = store or JobStore()
    root, params, spec, job_symbols, bar_seconds = _job_context(job_id, store)
    pair = [str(s) for s in (symbols or job_symbols)]
    if len(pair) != 2:
        raise ValueError(
            f"pair_check needs exactly 2 symbols; got {pair} — pass symbols=[A, B]"
        )
    interval = bar_interval or spec.data_contract.get("bar_interval") or "1h"
    from wayfinder_paths.jobs.execution.primitives import bar_interval_seconds

    seconds = bar_interval_seconds(interval) or bar_seconds
    prices = _pair_prices(
        pair, bar_interval=interval, days=days, exchange=exchange, feed=feed
    )
    report = pair_admission_gate(
        prices,
        bar_seconds=seconds,
        funding=_job_funding(root, pair),
        fee_bps=fee_bps if fee_bps is not None else float(params.get("fee_bps") or 5.0),
        slippage_bps=slippage_bps
        if slippage_bps is not None
        else float(params.get("slippage_bps") or 3.5),
    )
    report["bar_interval"] = interval
    report["exchange"] = exchange
    out_dir = root / "results" / "research" / "pair_check"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{pair[0]}_{pair[1]}.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["artifact"] = str(out_path)
    return report


def signal_check_job(
    job_id: str,
    *,
    column: str,
    horizons: list[int] | None = None,
    direction: str = "auto",
    store: Any | None = None,
) -> dict[str, Any]:
    """Event-study a strategy's precomputed signal column against the job's
    dataset — per symbol — WITHOUT running a backtest. The strategy's
    `precompute()` materializes the column (the same one decide() reads).

    `direction` defaults to "auto" here (read the side from the t-stat sign,
    counted as two trials): the observed failure this fixes is genuine SHORT
    edges being labeled "no edge" by the long-only t >= 2 rule. Pass an
    explicit direction when the thesis is directional — that is one trial."""
    from wayfinder_paths.jobs.execution.features import apply_precompute
    from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
    from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
    from wayfinder_paths.jobs.execution.simulator import _load_strategy
    from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
    from wayfinder_paths.jobs.store import JobStore

    store = store or JobStore()
    root = store.job_dir(job_id)
    job_data = _load_job_yaml(root)
    spec_data, _ = resolve_execution_spec(root, job_data)
    spec = ExecutionSpec.from_dict(spec_data)
    params = dict(job_data.get("execution_params") or {})
    script = store.resolve_script_entrypoint(job_id, job_data)
    strategy = _load_strategy(script, params)
    dataset = _load_dataset(root, spec, job_data, include_store_features=True)
    view = apply_precompute(strategy, dataset.bars)
    frame = view.to_frame()
    results: dict[str, Any] = {}
    for symbol in sorted(frame["symbol"].astype(str).unique()):
        sub = frame[frame["symbol"] == symbol].reset_index(drop=True)
        if column not in sub.columns:
            raise KeyError(
                f"column {column!r} not found after precompute; available "
                f"non-bar columns: "
                f"{sorted(set(sub.columns) - {'timestamp', 'symbol', 'open', 'high', 'low', 'close', 'volume'})}"
            )
        results[symbol] = event_study(
            sub, column, horizons=horizons, direction=direction
        )
    overall = any(r["has_edge"] for r in results.values())
    result: dict[str, Any] = {
        "column": column,
        "direction": direction,
        "per_symbol": results,
        "has_edge": overall,
        "read": (
            "the signal shows measurable predictive power on at least one "
            "symbol — proceed to a quick backtest"
            if overall
            else "the signal has no measured predictive power on any symbol — "
            "change the idea before building a strategy around it"
        ),
    }
    if direction == "auto":
        result["trials_multiplier"] = 2
    return result


_SCAN_LEDGER = "ledger.jsonl"
_SCAN_LEDGER_COMPACT_ROWS = 20_000


def _scan_dir(root: Any) -> Any:
    return root / "results" / "research" / "signal_scan"


def _read_scan_ledger(root: Any) -> list[dict[str, Any]]:
    target = _scan_dir(root) / _SCAN_LEDGER
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a corrupt line never breaks the ledger
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _append_scan_ledger(root: Any, rows: list[dict[str, Any]]) -> None:
    from wayfinder_paths.jobs.models import utc_now_iso

    target = _scan_dir(root) / _SCAN_LEDGER
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps({"ts": utc_now_iso(), **row}, sort_keys=True, default=str)
                + "\n"
            )
    existing = _read_scan_ledger(root)
    if len(existing) <= _SCAN_LEDGER_COMPACT_ROWS:
        return
    # Compact: latest scan_test per hash; scan_meta and holdout_check rows
    # are NEVER dropped — holdout spends are the honesty backbone.
    kept: list[dict[str, Any]] = []
    latest_by_hash: dict[str, dict[str, Any]] = {}
    for row in existing:
        if row.get("kind") == "scan_test" and row.get("hash"):
            latest_by_hash[str(row["hash"])] = row
        else:
            kept.append(row)
    kept.extend(latest_by_hash.values())
    with target.open("w", encoding="utf-8") as handle:
        for row in kept:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _trial_hash(symbol: str, signal: str, timeframe: str, horizon: int) -> str:
    import hashlib

    key = f"{symbol}|{signal}|{timeframe}|{horizon}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def signal_scan_job(
    job_id: str,
    *,
    symbols: list[str] | None = None,
    horizons: list[int] | None = None,
    timeframes: list[str] | None = None,
    holdout_fraction: float = 0.15,
    include_workspace: bool = True,
    campaign: str | None = None,
    condition_regime: bool = False,
    window_days: int | None = None,
    store: Any | None = None,
) -> dict[str, Any]:
    """Scan the ENTIRE canonical trigger library against the job's dataset —
    per symbol, across timeframes, in one call. Needs no strategy script: the
    library computes its own columns from OHLCV, so it works even when the
    workspace strategy is broken or absent. Run it BEFORE hand-writing
    trigger variants into `precompute()`.

    Honesty machinery: a reserved holdout tail the scan never sees (confirm
    frozen candidates later via holdout_check, once), BH q-values recomputed
    over ALL rows across symbols (one pooled test family), and an append-only
    trial ledger so repeat scans of the same workspace stay accountable."""
    from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
    from wayfinder_paths.jobs.execution.primitives import (
        ExecutionSpec,
        bar_interval_seconds,
    )
    from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
    from wayfinder_paths.jobs.models import utc_now_iso
    from wayfinder_paths.jobs.store import JobStore
    from wayfinder_paths.jobs.workspace_signals import (
        load_workspace_signals,
        validate_workspace_signals,
    )

    store = store or JobStore()
    root = store.job_dir(job_id)
    job_data = _load_job_yaml(root)
    spec_data, _ = resolve_execution_spec(root, job_data)
    spec = ExecutionSpec.from_dict(spec_data)
    params = dict(job_data.get("execution_params") or {})
    bar_seconds = bar_interval_seconds(spec.data_contract.get("bar_interval")) or 3600
    fee_bps = float(params.get("fee_bps") or 5.0)
    slippage_bps = float(params.get("slippage_bps") or 3.5)
    dataset = _load_dataset(root, spec, job_data, include_store_features=True)
    frame = dataset.bars.to_frame()
    available = sorted(frame["symbol"].astype(str).unique())
    targets = [str(s) for s in symbols] if symbols else available
    missing = [s for s in targets if s not in available]
    if missing:
        raise ValueError(
            f"symbols {missing} not in the job dataset; available: {available}"
        )
    workspace = load_workspace_signals(root) if include_workspace else None
    if workspace is not None:
        # Validation reads only pass/fail causality on the full frame — no
        # statistic escapes, so touching the tail here is not snooping.
        for symbol in targets:
            validate_workspace_signals(
                workspace.defs,
                frame[frame["symbol"] == symbol].reset_index(drop=True),
            )
    extra_signals = workspace.defs if workspace is not None else ()
    # A campaign is its own declared BH family: workspace defs only, pooled
    # only with each other — the canonical library neither taxes nor is taxed
    # by the campaign. Provenance (name + defs sha) lands in the ledger so a
    # renamed re-run is visible snooping, exactly like workspace sha tracking.
    if campaign is not None:
        if not extra_signals:
            raise ValueError(
                "a campaign scan needs workspace signals — declare the "
                "campaign's defs in workspace/src/signals.py first"
            )
        if not str(campaign).strip():
            raise ValueError("campaign name must be non-empty")
    include_canonical = campaign is None
    if window_days is not None:
        if window_days < 7:
            raise ValueError("window_days must be >= 7")
        # Declared recent-window family: trailing window only, ledger-tagged.
        # Survivors cap at PROBATION (short window = weaker stats by
        # construction); forward paper adjudicates.
        stamps_all = pd.to_datetime(frame["timestamp"], utc=True)
        cutoff = stamps_all.max() - pd.Timedelta(days=window_days)
        frame = frame[stamps_all >= cutoff].reset_index(drop=True)
    per_symbol = {
        symbol: scan_signals(
            frame[frame["symbol"] == symbol].reset_index(drop=True),
            horizons=horizons,
            bar_seconds=bar_seconds,
            timeframes=timeframes,
            holdout_fraction=holdout_fraction,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            extra_signals=extra_signals,
            include_canonical=include_canonical,
            condition_regime=condition_regime,
        )
        for symbol in targets
    }
    # One pooled BH family across every row of every symbol — the per-symbol
    # q-values were provisional; these are the final numbers.
    all_rows_by_symbol = {
        symbol: scan.pop("_all_rows") for symbol, scan in per_symbol.items()
    }
    if window_days is not None:
        for rows_ in all_rows_by_symbol.values():
            for row in rows_:
                row["window_days"] = window_days
    apply_bh_verdicts([row for rows in all_rows_by_symbol.values() for row in rows])
    for symbol, scan in per_symbol.items():
        pooled_rows = all_rows_by_symbol[symbol]
        scan["promoted"] = [
            row for row in pooled_rows if row.get("verdict") == "promote"
        ]
        scan["probation"] = [
            row for row in pooled_rows if row.get("verdict") == "probation"
        ]
    prior = _read_scan_ledger(root)
    prior_scans = sum(1 for row in prior if row.get("kind") == "scan_meta")
    prior_tests = sum(1 for row in prior if row.get("kind") == "scan_test")
    prior_unique = len(
        {row.get("hash") for row in prior if row.get("kind") == "scan_test"}
    )
    ledger_rows: list[dict[str, Any]] = [
        {
            "kind": "scan_meta",
            "scan_id": utc_now_iso(),
            "symbols": targets,
            "timeframes": timeframes or ["base"],
            "tests_run": sum(s["tests_run"] for s in per_symbol.values()),
            "holdout_fraction": holdout_fraction,
            "cutoff_ts": {
                sym: scan["holdout"]["cutoff_ts"] for sym, scan in per_symbol.items()
            },
            "workspace_signals": [spec.name for spec in extra_signals],
            "workspace_signals_sha": workspace.sha if workspace else None,
            "campaign": campaign,
            "condition_regime": condition_regime,
            "window_days": window_days,
        }
    ]
    # EVERY executed test is a recorded trial — not just the survivors.
    for symbol, rows in all_rows_by_symbol.items():
        for row in rows:
            ledger_rows.append(
                {
                    "kind": "scan_test",
                    # Regime-conditional and windowed cells are DISTINCT
                    # trials — they must not collide with the base row's hash.
                    "hash": _trial_hash(
                        symbol,
                        row["signal"]
                        + (f"|{row['regime']}" if row.get("regime") else "")
                        + (f"|w{window_days}" if window_days else ""),
                        row["timeframe"],
                        row["horizon"],
                    ),
                    "symbol": symbol,
                    "signal": row["signal"],
                    "timeframe": row["timeframe"],
                    "horizon": row["horizon"],
                    "direction": row["direction"],
                    "n": row["n"],
                    "t": round(row["t_stat_vs_drift"], 3),
                    "q": row.get("q_value"),
                    "folds_agreeing": row.get("folds_agreeing"),
                    "verdict": row.get("verdict"),
                    "library": row.get("library"),
                    "campaign": campaign,
                    "regime": row.get("regime"),
                    "recency_trend": row.get("recency_trend"),
                }
            )
    _append_scan_ledger(root, ledger_rows)
    cumulative_tests = prior_tests + sum(s["tests_run"] for s in per_symbol.values())
    result: dict[str, Any] = {
        "per_symbol": per_symbol,
        "campaign": campaign,
        "workspace_signals": [spec.name for spec in extra_signals],
        "holdout": {
            "fraction": holdout_fraction,
            "cutoff_ts_per_symbol": {
                sym: scan["holdout"]["cutoff_ts"] for sym, scan in per_symbol.items()
            },
        },
        "ledger": {
            "prior_scans": prior_scans,
            "prior_tests": prior_tests,
            "prior_unique_tests": prior_unique,
            "cumulative_tests": cumulative_tests,
        },
        "read": (
            "promote = q<=0.10 (pooled across symbols) + fold stability; take "
            "at most 3 promoted cards forward (CORE/ADJACENT/DIVERGENT), build "
            "the minimal fixed-time-exit strategy at the measured horizon "
            "first, and spend the one holdout_check per candidate only after "
            f"the spec is frozen. This workspace has now run "
            f"{cumulative_tests} tests across {prior_scans + 1} scans — "
            "q-values control this scan only; the ledger keeps repeat scans "
            "honest"
        ),
    }
    out_dir = _scan_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "scan.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    result["artifact"] = str(out_path)
    return result


def holdout_check_job(
    job_id: str,
    *,
    signal: str,
    horizon: int,
    direction: str,
    timeframe: str | None = None,
    symbols: list[str] | None = None,
    holdout_fraction: float = 0.15,
    store: Any | None = None,
) -> dict[str, Any]:
    """One-shot confirmation of a FROZEN scan candidate on the reserved
    holdout tail. Spend it once per candidate: a second look at the same tail
    is data snooping — the trial ledger remembers, and a repeat run is
    flagged `already_spent` (warned, not refused)."""
    from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
    from wayfinder_paths.jobs.execution.primitives import (
        ExecutionSpec,
        bar_interval_seconds,
    )
    from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
    from wayfinder_paths.jobs.store import JobStore
    from wayfinder_paths.jobs.workspace_signals import (
        load_workspace_signals,
        validate_workspace_signals,
    )

    store = store or JobStore()
    root = store.job_dir(job_id)
    job_data = _load_job_yaml(root)
    spec_data, _ = resolve_execution_spec(root, job_data)
    spec = ExecutionSpec.from_dict(spec_data)
    bar_seconds = bar_interval_seconds(spec.data_contract.get("bar_interval")) or 3600
    tf_name = timeframe or str(spec.data_contract.get("bar_interval") or "1h")
    tf_seconds = bar_interval_seconds(tf_name)
    if not tf_seconds:
        raise ValueError(f"unparseable timeframe {tf_name!r}")
    dataset = _load_dataset(root, spec, job_data, include_store_features=True)
    frame = dataset.bars.to_frame()
    available = sorted(frame["symbol"].astype(str).unique())
    targets = [str(s) for s in symbols] if symbols else available
    workspace = load_workspace_signals(root)
    extra_defs: dict[str, SignalDef] = {}
    workspace_changed_since_scan = False
    if workspace is not None and signal not in signal_defs():
        # Highest-stakes read of a workspace def: re-run the causality gate,
        # and compare the code sha against the scan that nominated it — a
        # holdout on edited code is confirming something the scan never saw.
        for symbol in targets:
            validate_workspace_signals(
                workspace.defs,
                frame[frame["symbol"] == symbol].reset_index(drop=True),
            )
        extra_defs = {spec_.name: spec_ for spec_ in workspace.defs}
        scanned_shas = [
            row.get("workspace_signals_sha")
            for row in _read_scan_ledger(root)
            if row.get("kind") == "scan_meta" and row.get("workspace_signals_sha")
        ]
        if scanned_shas and scanned_shas[-1] != workspace.sha:
            workspace_changed_since_scan = True
    scan_path = _scan_dir(root) / "scan.json"
    recorded_cutoffs: dict[str, Any] = {}
    if scan_path.exists():
        try:
            recorded_cutoffs = (
                json.loads(scan_path.read_text(encoding="utf-8"))
                .get("holdout", {})
                .get("cutoff_ts_per_symbol", {})
            )
        except ValueError:
            recorded_cutoffs = {}
    ledger = _read_scan_ledger(root)
    per_symbol: dict[str, Any] = {}
    ledger_rows: list[dict[str, Any]] = []
    already_spent = False
    for symbol in targets:
        sub = frame[frame["symbol"] == symbol].reset_index(drop=True)
        cutoff = recorded_cutoffs.get(symbol)
        cutoff_note = None
        if cutoff is None:
            cutoff_index = int(len(sub) * (1 - holdout_fraction))
            cutoff = str(sub["timestamp"].iloc[max(cutoff_index - 1, 0)])
            cutoff_note = (
                "no scan recorded a cutoff for this symbol — computed from "
                f"holdout_fraction={holdout_fraction}"
            )
        trial = _trial_hash(symbol, signal, tf_name, horizon)
        spent = any(
            row.get("kind") == "holdout_check" and row.get("hash") == trial
            for row in ledger
        )
        already_spent = already_spent or spent
        report = holdout_event_study(
            sub,
            signal=signal,
            horizon=horizon,
            direction=direction,
            cutoff_ts=cutoff,
            bar_seconds=bar_seconds,
            timeframe_seconds=tf_seconds,
            extra_defs=extra_defs,
        )
        if cutoff_note:
            report["cutoff_note"] = cutoff_note
        report["timeframe"] = tf_name
        per_symbol[symbol] = report
        ledger_rows.append(
            {
                "kind": "holdout_check",
                "hash": trial,
                "symbol": symbol,
                "signal": signal,
                "timeframe": tf_name,
                "horizon": horizon,
                "direction": direction,
                "n": report.get("n"),
                "t": round(report.get("t_stat_vs_drift", 0.0), 3),
                "verdict": report["verdict"],
            }
        )
    _append_scan_ledger(root, ledger_rows)
    result: dict[str, Any] = {
        "signal": signal,
        "timeframe": tf_name,
        "horizon": horizon,
        "direction": direction,
        "per_symbol": per_symbol,
        "already_spent": already_spent,
    }
    if workspace_changed_since_scan:
        result["workspace_signals_changed_since_scan"] = True
        result["workspace_warning"] = (
            "workspace/src/signals.py changed since the last recorded scan — "
            "this holdout is confirming code the scan never tested"
        )
    if already_spent:
        result["read"] = (
            "this tail has already been used to confirm this candidate — a "
            "second look is data snooping; treat this result as descriptive "
            "only"
        )
    out_dir = _scan_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"holdout_{_trial_hash('*', signal, tf_name, horizon)}.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    result["artifact"] = str(out_path)
    return result


def rank_check_job(
    job_id: str,
    *,
    column: str,
    horizons: list[int] | None = None,
    store: Any | None = None,
) -> dict[str, Any]:
    """Rank-IC study of a strategy's precomputed ranking column across the
    job's symbols — the pre-build test for basket/cross-sectional ideas
    (event_study covers per-symbol entry signals; this covers rankings)."""
    from wayfinder_paths.jobs.execution.features import apply_precompute
    from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
    from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
    from wayfinder_paths.jobs.execution.simulator import _load_strategy
    from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
    from wayfinder_paths.jobs.store import JobStore

    store = store or JobStore()
    root = store.job_dir(job_id)
    job_data = _load_job_yaml(root)
    spec_data, _ = resolve_execution_spec(root, job_data)
    spec = ExecutionSpec.from_dict(spec_data)
    params = dict(job_data.get("execution_params") or {})
    script = store.resolve_script_entrypoint(job_id, job_data)
    strategy = _load_strategy(script, params)
    dataset = _load_dataset(root, spec, job_data, include_store_features=True)
    view = apply_precompute(strategy, dataset.bars)
    frame = view.to_frame()
    symbols = sorted(frame["symbol"].astype(str).unique())
    if len(symbols) < 4:
        raise ValueError(
            f"rank-check needs >=4 symbols for a cross-section; job has "
            f"{symbols} — for 1-2 symbols use signal-check instead"
        )
    frames = {
        symbol: frame[frame["symbol"] == symbol].reset_index(drop=True)
        for symbol in symbols
    }
    missing = [s for s, f in frames.items() if column not in f.columns]
    if missing:
        non_bar = sorted(
            set(frame.columns)
            - {"timestamp", "symbol", "open", "high", "low", "close", "volume"}
        )
        raise KeyError(
            f"column {column!r} not found after precompute (missing for "
            f"{missing}); available non-bar columns: {non_bar}"
        )
    result = rank_ic(frames, column, horizons=horizons)
    result["symbols"] = symbols
    return result
