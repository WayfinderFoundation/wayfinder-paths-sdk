"""Causal portfolio-regime contract shared by every execution path.

Strategies declare the cells in which they may add risk through
``execution_params.enabled_regimes``.  The engine owns the label and the entry
gate so a candidate cannot make its backtest conditional while trading a
different contract in paper or live execution.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import CompletedBarsView
from wayfinder_paths.jobs.indicators import REGIME_LABELS, atr

PORTFOLIO_REGIME_COLUMN = "__wf_portfolio_regime"
PORTFOLIO_REGIME_CLASSIFIER = "portfolio_majority_v1"
MIXED_REGIME = "mixed"
REGIME_VOL_BASELINE_BARS = 400
# ATR14 needs thirteen bars before the 400 lagged baseline; leave headroom for
# sparse/misaligned symbols.  Live fetches at least this much history, while
# strategies still receive only their own declared compute window.
REGIME_FEATURE_WARMUP_BARS = 450


def enabled_regimes(params: Mapping[str, Any]) -> tuple[str, ...]:
    """Validated regime declaration; an absent declaration means legacy mode."""
    raw = params.get("enabled_regimes")
    if raw is None or raw == []:
        return ()
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise ValueError("execution_params.enabled_regimes must be a list")
    values = tuple(dict.fromkeys(str(value).strip() for value in raw))
    invalid = sorted(set(values) - set(REGIME_LABELS))
    if invalid:
        raise ValueError(
            "execution_params.enabled_regimes contains unknown cells: "
            + ", ".join(invalid)
        )
    if not 1 <= len(values) <= 2:
        raise ValueError("enabled_regimes must declare one or two regime cells")
    return values


def regime_universe(
    params: Mapping[str, Any], available_symbols: Sequence[str]
) -> tuple[str, ...]:
    declared = params.get("symbols")
    values = (
        declared
        if isinstance(declared, Sequence) and not isinstance(declared, str)
        else available_symbols
    )
    return tuple(dict.fromkeys(str(value) for value in values))


def classify_portfolio_regimes(
    bars: pd.DataFrame,
    *,
    universe: Sequence[str],
    vol_baseline_bars: int = REGIME_VOL_BASELINE_BARS,
    minimum_coverage: float = 0.75,
) -> pd.Series:
    """Strict-majority cell across the candidate's own trading universe.

    Trend is close versus SMA50.  Volatility is ATR14/close versus the prior
    400 observations' median.  The one-bar shift prevents the current shock
    from moving its own threshold.  Ties and incomplete panels are ``mixed``.
    """
    if bars.empty:
        return pd.Series(dtype=object, name=PORTFOLIO_REGIME_COLUMN)
    if vol_baseline_bars < 50:
        raise ValueError("regime volatility baseline must be at least 50 bars")
    if not 0 < minimum_coverage <= 1:
        raise ValueError("regime minimum coverage must be in (0, 1]")
    symbols = tuple(dict.fromkeys(str(value) for value in universe))
    if not symbols:
        raise ValueError("portfolio regime classification needs a trading universe")

    frame = bars.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["symbol"] = frame["symbol"].astype(str)
    frame = frame[frame["symbol"].isin(symbols)]
    per_symbol: list[pd.DataFrame] = []
    for symbol in symbols:
        ordered = (
            frame[frame["symbol"] == symbol]
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
        if ordered.empty:
            continue
        close = pd.to_numeric(ordered["close"], errors="coerce")
        trend_up = close > close.rolling(50, min_periods=50).mean()
        volatility = atr(ordered, 14) / close
        threshold = (
            volatility.rolling(
                vol_baseline_bars, min_periods=vol_baseline_bars
            ).median()
        ).shift(1)
        valid = trend_up.notna() & volatility.notna() & threshold.notna()
        labels = pd.Series(None, index=ordered.index, dtype=object)
        high = volatility > threshold
        labels.loc[valid & trend_up & high] = "up_highvol"
        labels.loc[valid & trend_up & ~high] = "up_lowvol"
        labels.loc[valid & ~trend_up & high] = "down_highvol"
        labels.loc[valid & ~trend_up & ~high] = "down_lowvol"
        per_symbol.append(
            pd.DataFrame(
                {
                    "timestamp": ordered["timestamp"],
                    "symbol": symbol,
                    "regime": labels,
                }
            )
        )

    timestamps = pd.Index(sorted(frame["timestamp"].unique()), name="timestamp")
    output = pd.Series(MIXED_REGIME, index=timestamps, dtype=object)
    if not per_symbol:
        output.name = PORTFOLIO_REGIME_COLUMN
        return output
    panel = pd.concat(per_symbol, ignore_index=True).pivot(
        index="timestamp", columns="symbol", values="regime"
    )
    panel = panel.reindex(timestamps)
    required = max(1, math.ceil(len(symbols) * minimum_coverage))
    eligible = panel.notna().sum(axis=1)
    counts = pd.DataFrame(
        {label: panel.eq(label).sum(axis=1) for label in REGIME_LABELS},
        index=panel.index,
    )
    winner = counts.idxmax(axis=1)
    winner_count = counts.max(axis=1)
    strict_majority = winner_count > eligible / 2
    usable = (eligible >= required) & strict_majority
    output.loc[usable] = winner.loc[usable]
    output.name = PORTFOLIO_REGIME_COLUMN
    return output


def add_portfolio_regime_feature(
    view: CompletedBarsView, params: Mapping[str, Any]
) -> CompletedBarsView:
    """Attach the engine-owned label only for an explicitly specialized job."""
    if not enabled_regimes(params):
        return view
    bars = view.to_frame()
    labels = classify_portfolio_regimes(
        bars,
        universe=regime_universe(params, view.symbols),
    )
    bars[PORTFOLIO_REGIME_COLUMN] = bars["timestamp"].map(labels)
    bars[PORTFOLIO_REGIME_COLUMN] = bars[PORTFOLIO_REGIME_COLUMN].where(
        bars[PORTFOLIO_REGIME_COLUMN].notna(), MIXED_REGIME
    )
    return CompletedBarsView(bars)


def current_portfolio_regime(view: CompletedBarsView) -> str:
    try:
        value = view.feature(PORTFOLIO_REGIME_COLUMN)
    except ValueError:
        return MIXED_REGIME
    return str(value or MIXED_REGIME)


def portfolio_regime_labels(view: CompletedBarsView) -> dict[pd.Timestamp, str]:
    frame = view.to_frame()
    if PORTFOLIO_REGIME_COLUMN not in frame.columns:
        return {}
    return {
        _utc_timestamp(stamp): str(label or MIXED_REGIME)
        for stamp, label in frame[["timestamp", PORTFOLIO_REGIME_COLUMN]]
        .drop_duplicates("timestamp")
        .itertuples(index=False, name=None)
    }


def partition_regime_returns(
    equity_curve: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    *,
    labels: Mapping[pd.Timestamp, str],
    target_regimes: Sequence[str],
) -> dict[str, Any]:
    """Partition marked-equity increments and entry counts by market cell."""
    targets = set(target_regimes)
    target_by_day: dict[str, float] = {}
    outside_by_day: dict[str, float] = {}
    ordered = sorted(equity_curve, key=lambda row: _utc_timestamp(row["timestamp"]))
    for previous, current in zip(ordered, ordered[1:], strict=False):
        before = float(previous["equity"])
        after = float(current["equity"])
        if before <= 0 or after <= 0:
            continue
        stamp = _utc_timestamp(current["timestamp"])
        bucket = target_by_day if labels.get(stamp) in targets else outside_by_day
        day = str(stamp.date())
        bucket[day] = bucket.get(day, 0.0) + math.log(after / before)

    target_entries = 0
    outside_entries = 0
    for trade in trades:
        if trade.get("reduce_only") is True:
            continue
        raw_stamp = trade.get("timestamp") or trade.get("ts")
        if raw_stamp is None:
            continue
        if labels.get(_utc_timestamp(raw_stamp)) in targets:
            target_entries += 1
        else:
            outside_entries += 1
    target_daily = sorted(target_by_day.items())
    outside_daily = sorted(outside_by_day.items())
    target_growth = sum(value for _, value in target_daily)
    outside_growth = sum(value for _, value in outside_daily)
    return {
        "target_daily": target_daily,
        "outside_daily": outside_daily,
        "target_trade_count": target_entries,
        "outside_trade_count": outside_entries,
        "target_net_log_growth": target_growth,
        "target_net_return": math.exp(target_growth) - 1.0,
        "outside_net_log_growth": outside_growth,
        "outside_loss_pct": max(0.0, 1.0 - math.exp(outside_growth)),
    }


def _utc_timestamp(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        return stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


utc_timestamp = _utc_timestamp


def opposite_regime(regime: str) -> str:
    """Flip both independent axes; down/quiet's counter is up/volatile."""
    mapping = {
        "up_lowvol": "down_highvol",
        "up_highvol": "down_lowvol",
        "down_lowvol": "up_highvol",
        "down_highvol": "up_lowvol",
    }
    if regime not in mapping:
        raise ValueError(f"cannot derive an opposite for regime {regime!r}")
    return mapping[regime]


def regime_metadata(
    view: CompletedBarsView, params: Mapping[str, Any]
) -> dict[str, Any]:
    declared = enabled_regimes(params)
    if not declared:
        return {"enabled": False}
    return {
        "enabled": True,
        "classifier": PORTFOLIO_REGIME_CLASSIFIER,
        "current": current_portfolio_regime(view),
        "allowed": list(declared),
        "universe": list(regime_universe(params, view.symbols)),
    }
