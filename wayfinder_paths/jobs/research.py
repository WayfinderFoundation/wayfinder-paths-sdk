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

Stats functions adapted with thanks from
examples/paths/spread-radar-reference/scripts/lib.py (ou_half_life,
engle_granger residual test, stability idea) and
examples/paths/hedge-finder/scripts/lib.py (adf_statistic, beta) — copied,
not imported: examples/ is not a runtime package.
"""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
import pandas as pd

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
) -> dict[str, Any]:
    """Does an entry signal predict forward returns AT ALL? Run this before
    building a strategy around it — if the signal doesn't beat the series'
    own unconditional drift, no exit/sizing engineering can save it.

    For each horizon h: mean forward log-return after signal bars vs the
    unconditional mean forward return of the whole series (the random-entry
    baseline), with a t-stat on the difference and the event count. n < 30 is
    flagged insufficient — never treated as evidence of edge.
    """
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
        events = sig[: n - h]
        event_returns = fwd[events]
        n_events = int(events.sum())
        drift = float(fwd.mean())
        if n_events == 0:
            per_horizon.append({"horizon": h, "n": 0, "verdict": "no events"})
            continue
        mean_r = float(event_returns.mean())
        std_r = float(event_returns.std(ddof=1)) if n_events > 1 else 0.0
        sem = (
            std_r / math.sqrt(n_events) if n_events > 1 and std_r > 0 else float("inf")
        )
        t = (mean_r - drift) / sem if math.isfinite(sem) else 0.0
        edge = bool(t >= 2.0 and n_events >= 30)
        any_edge = any_edge or edge
        per_horizon.append(
            {
                "horizon": h,
                "n": n_events,
                "mean_fwd_return": mean_r,
                "drift_baseline": drift,
                "hit_rate": float((event_returns > 0).mean()),
                "t_stat_vs_drift": float(t),
                "edge": edge,
                "note": "insufficient sample (n<30)" if n_events < 30 else None,
            }
        )
    return {
        "horizons": per_horizon,
        "has_edge": any_edge,
        "read": (
            "signal beats the series' own drift at one or more horizons — "
            "worth building and validating"
            if any_edge
            else "no horizon beats the unconditional drift with t>=2 and "
            "n>=30 — the entry has no measured predictive power; change the "
            "idea, not the parameters"
        ),
        "multiple_testing_note": (
            "testing many signals inflates false positives — at t>=2, roughly "
            "1 in 20 random signals looks good by chance; prefer fewer, "
            "stronger hypotheses"
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
    store: Any | None = None,
) -> dict[str, Any]:
    """Event-study a strategy's precomputed signal column against the job's
    dataset — per symbol — WITHOUT running a backtest. The strategy's
    `precompute()` materializes the column (the same one decide() reads)."""
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
    dataset = _load_dataset(root, spec, job_data)
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
        results[symbol] = event_study(sub, column, horizons=horizons)
    overall = any(r["has_edge"] for r in results.values())
    return {
        "column": column,
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
