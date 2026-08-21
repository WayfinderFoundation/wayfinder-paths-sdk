"""Compact market-regime baselines built from the job's resident dataset.

This module runs on the hourly derived-feature refresh, where the full bars
frame is already in memory. The tick-time health monitor reads the resulting
small JSON artifact instead of loading the 120-day dataset a second time.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.regime_contract import MARKET_STATE_PATH, WINDOW_DAYS


def summarize_market_state(
    bars: pd.DataFrame,
    *,
    funding_rows: Sequence[Mapping[str, Any]] = (),
    windows: Sequence[int] = WINDOW_DAYS,
) -> dict[str, Any]:
    """Compare each recent market window with the preceding dataset.

    Excluding the recent window from its baseline prevents a new regime from
    diluting its own comparison population. Calculations are descriptive and
    causal; no future bar participates in a recent-window metric.
    """
    if bars.empty:
        return {"available": False, "reason": "empty bars dataset"}

    frame = bars.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    as_of = pd.Timestamp(frame["timestamp"].max())

    from wayfinder_paths.jobs.indicators import classify_regimes

    labels: list[pd.DataFrame] = []
    for _symbol, symbol_frame in frame.groupby("symbol", sort=False):
        ordered = symbol_frame.sort_values("timestamp").copy()
        ordered["regime"] = classify_regimes(ordered.reset_index(drop=True)).to_numpy()
        labels.append(ordered[["timestamp", "symbol", "regime"]])
    regime_frame = (
        pd.concat(labels, ignore_index=True)
        if labels
        else pd.DataFrame(columns=["timestamp", "symbol", "regime"])
    )

    dataset_first = pd.Timestamp(frame["timestamp"].min())
    funding = _funding_frame(funding_rows)
    if not funding.empty:
        funding = funding[
            (funding["timestamp"] >= dataset_first) & (funding["timestamp"] <= as_of)
        ]
    comparisons: dict[str, Any] = {}
    for days in windows:
        cutoff = as_of - pd.Timedelta(days=int(days))
        recent = frame[frame["timestamp"] > cutoff]
        baseline = frame[frame["timestamp"] <= cutoff]
        recent_regimes = regime_frame[regime_frame["timestamp"] > cutoff]
        baseline_regimes = regime_frame[regime_frame["timestamp"] <= cutoff]
        if recent.empty or baseline.empty:
            comparisons[str(days)] = {
                "available": False,
                "reason": "dataset does not contain both baseline and recent bars",
            }
            continue
        recent_metrics = _market_metrics(recent, recent_regimes)
        baseline_metrics = _market_metrics(baseline, baseline_regimes)
        comparisons[str(days)] = {
            "available": True,
            "recent": recent_metrics,
            "baseline": baseline_metrics,
            "volatility_ratio": _ratio(
                recent_metrics.get("realized_volatility"),
                baseline_metrics.get("realized_volatility"),
            ),
            "correlation_delta": _difference(
                recent_metrics.get("mean_pairwise_correlation"),
                baseline_metrics.get("mean_pairwise_correlation"),
            ),
            "liquidity_ratio": _ratio(
                recent_metrics.get("median_notional_volume"),
                baseline_metrics.get("median_notional_volume"),
            ),
            "regime_js_divergence": _js_divergence(
                baseline_metrics.get("regime_distribution") or {},
                recent_metrics.get("regime_distribution") or {},
            ),
            "funding_shift": _funding_shift(funding, cutoff),
        }
    return {
        "available": any(row.get("available") for row in comparisons.values()),
        "as_of": as_of.isoformat(),
        "dataset_first_ts": dataset_first.isoformat(),
        "symbols": sorted(str(value) for value in frame["symbol"].unique()),
        "windows": comparisons,
        "_basis": (
            "Each recent 7/14/30-day market window is compared with all earlier "
            "bars in the same frozen dataset. volatility_ratio, correlation_delta, "
            "liquidity_ratio, causal regime-mix JS divergence and funding shift are "
            "descriptive drift inputs; they do not claim alpha by themselves."
        ),
    }


def write_market_state(
    root: Path,
    bars: pd.DataFrame,
    *,
    funding_rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    report = summarize_market_state(bars, funding_rows=funding_rows)
    path = Path(root) / MARKET_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    return report


def _market_metrics(frame: pd.DataFrame, regime_frame: pd.DataFrame) -> dict[str, Any]:
    closes = frame.pivot_table(
        index="timestamp", columns="symbol", values="close", aggfunc="last"
    ).sort_index()
    returns = closes.pct_change(fill_method=None)
    vol_values = [
        float(returns[column].std())
        for column in returns.columns
        if pd.notna(returns[column].std())
    ]
    corr = returns.corr(min_periods=20)
    corr_values = [
        float(corr.iloc[i, j])
        for i in range(len(corr.columns))
        for j in range(i + 1, len(corr.columns))
        if pd.notna(corr.iloc[i, j])
    ]
    volumes = pd.to_numeric(frame.get("volume"), errors="coerce")
    notional = pd.to_numeric(frame["close"], errors="coerce") * volumes
    distribution = _distribution(
        str(value) for value in regime_frame["regime"].dropna().tolist()
    )
    return {
        "bars": int(len(frame)),
        "first_ts": pd.Timestamp(frame["timestamp"].min()).isoformat(),
        "last_ts": pd.Timestamp(frame["timestamp"].max()).isoformat(),
        "realized_volatility": _round_or_none(statistics.median(vol_values), 8)
        if vol_values
        else None,
        "mean_pairwise_correlation": _round_or_none(statistics.fmean(corr_values), 6)
        if corr_values
        else None,
        "median_notional_volume": _round_or_none(float(notional.median()), 4)
        if notional.notna().any()
        else None,
        "regime_distribution": distribution,
    }


def _funding_frame(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        try:
            normalized.append(
                {
                    "timestamp": pd.Timestamp(str(row["timestamp"])),
                    "symbol": str(row.get("symbol") or "portfolio"),
                    "value": float(row["value"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    if not normalized:
        return pd.DataFrame(columns=["timestamp", "symbol", "value"])
    frame = pd.DataFrame(normalized)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame.sort_values("timestamp")


def _funding_shift(frame: pd.DataFrame, cutoff: pd.Timestamp) -> dict[str, Any] | None:
    if frame.empty:
        return None
    strongest: dict[str, Any] | None = None
    for symbol, rows in frame.groupby("symbol"):
        recent = rows.loc[rows["timestamp"] > cutoff, "value"].astype(float)
        baseline = rows.loc[rows["timestamp"] <= cutoff, "value"].astype(float)
        if len(recent) < 3 or len(baseline) < 10:
            continue
        sigma = float(baseline.std())
        if not math.isfinite(sigma) or sigma <= 0:
            continue
        z = (float(recent.mean()) - float(baseline.mean())) / sigma
        candidate = {
            "symbol": str(symbol),
            "z_score": round(z, 4),
            "recent_mean": round(float(recent.mean()), 9),
            "baseline_mean": round(float(baseline.mean()), 9),
            "recent_n": int(len(recent)),
            "baseline_n": int(len(baseline)),
        }
        if strongest is None or abs(z) > abs(float(strongest["z_score"])):
            strongest = candidate
    return strongest


def _distribution(values: Iterable[str]) -> dict[str, float]:
    counts = Counter(values)
    total = sum(counts.values())
    return (
        {key: round(value / total, 6) for key, value in sorted(counts.items())}
        if total
        else {}
    )


def _js_divergence(left: Mapping[str, Any], right: Mapping[str, Any]) -> float | None:
    keys = set(left) | set(right)
    if not keys:
        return None
    p = [float(left.get(key) or 0.0) for key in keys]
    q = [float(right.get(key) or 0.0) for key in keys]
    if sum(p) <= 0 or sum(q) <= 0:
        return None
    p = [value / sum(p) for value in p]
    q = [value / sum(q) for value in q]
    middle = [(a + b) / 2.0 for a, b in zip(p, q, strict=True)]

    def kl(values: Sequence[float], reference: Sequence[float]) -> float:
        return sum(
            value * math.log(value / ref)
            for value, ref in zip(values, reference, strict=True)
            if value > 0 and ref > 0
        )

    return round((kl(p, middle) + kl(q, middle)) / 2.0, 6)


def _ratio(numerator: Any, denominator: Any) -> float | None:
    if not _finite(numerator) or not _finite(denominator) or float(denominator) == 0:
        return None
    return round(float(numerator) / float(denominator), 6)


def _difference(left: Any, right: Any) -> float | None:
    if not _finite(left) or not _finite(right):
        return None
    return round(float(left) - float(right), 6)


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _round_or_none(value: float, digits: int) -> float | None:
    return round(value, digits) if math.isfinite(value) else None
