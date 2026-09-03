"""Causal portfolio-regime contract shared by every evaluation path.

Strategies declare the cells where their edge should accrue through
``execution_params.target_regimes``.  The engine owns the label, while the
economic gate and probation own the attribution.  The declaration deliberately
does not block entries: transition and mean-reversion strategies often enter
before the target cell begins, and their marked returns are still charged to
the cell in which those returns occur.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

import pandas as pd

from wayfinder_paths.jobs.indicators import REGIME_LABELS, classify_regimes

if TYPE_CHECKING:
    from wayfinder_paths.jobs.execution.primitives import CompletedBarsView

PORTFOLIO_REGIME_COLUMN = "__wf_portfolio_regime"
PORTFOLIO_REGIME_CLASSIFIER = "portfolio_majority_v1"
MIXED_REGIME = "mixed"
REGIME_VOL_BASELINE_BARS = 400
# ATR14 needs thirteen bars before the 400 lagged baseline; leave headroom for
# sparse/misaligned symbols.  Live fetches at least this much history, while
# strategies still receive only their own declared compute window.
REGIME_FEATURE_WARMUP_BARS = 450

# Macro regime: the universe-median cumulative close-to-close return over a
# window, labelled bull / bear / chop. The portfolio cells above work on the
# bar interval (a 50-bar trend is four hours on 5-minute bars) and see a +45%
# week as the same shuffle of micro-cells as the chop before it; this is the
# scale a designer means by "bear flipping to bull". Design time reads it from
# the campaign pack; runtime reads the same label as a derived feature column
# (numeric store: +1 bull, 0 chop, -1 bear) refreshed hourly.
MACRO_BULL_RETURN = 0.10
MACRO_BEAR_RETURN = -0.10
MACRO_RETURN_WINDOWS_DAYS = (7, 28)
MACRO_LABEL_WINDOW_DAYS = 28
MACRO_FEATURE_NAME = "macro_regime"
MACRO_RETURN_FEATURE_NAMES = tuple(
    f"macro_ret_{days}d" for days in MACRO_RETURN_WINDOWS_DAYS
)
MACRO_CODES = {"bull": 1.0, "chop": 0.0, "bear": -1.0}


def macro_label(median_return: float) -> str:
    if median_return >= MACRO_BULL_RETURN:
        return "bull"
    if median_return <= MACRO_BEAR_RETURN:
        return "bear"
    return "chop"


def macro_feature_columns(closes: pd.DataFrame) -> dict[str, pd.Series]:
    """Panel-wide macro series at every timestamp of a close matrix (index:
    UTC timestamps, columns: symbols): the universe-median trailing return
    over each window and the coded label of the labelling window. Causal by
    construction — the value at t uses closes at or before t and the last
    close at or before t minus the window — so appending bars never changes
    an earlier value. NaN until the window is covered."""
    if closes.empty:
        return {}
    ordered = closes.sort_index().astype(float)
    out: dict[str, pd.Series] = {}
    label_source: pd.Series | None = None
    for days in MACRO_RETURN_WINDOWS_DAYS:
        shifted = ordered.copy()
        shifted.index = shifted.index + pd.Timedelta(days=days)
        # reindex+ffill leaves every timestamp before the first shifted
        # stamp NaN, so the window is empty until it is fully covered.
        prior = shifted.reindex(ordered.index, method="ffill")
        median = (ordered / prior - 1.0).median(axis=1, skipna=True)
        median = median.where(prior.notna().any(axis=1))
        out[f"macro_ret_{days}d"] = median
        if days == MACRO_LABEL_WINDOW_DAYS:
            label_source = median
    if label_source is not None:
        out[MACRO_FEATURE_NAME] = label_source.map(
            lambda value: MACRO_CODES[macro_label(float(value))]
            if pd.notna(value)
            else float("nan")
        )
    return out


def macro_feature_store_rows(
    closes: pd.DataFrame, *, every_bars: int, written_at: str
) -> list[dict[str, Any]]:
    """The macro series as feature-store rows (one per symbol, the panel-wide
    value repeated) at a coarse cadence, the shape `derive_features_job`
    writes and `merge_features` reads."""
    columns = macro_feature_columns(closes)
    if not columns:
        return []
    stamps = closes.sort_index().index[:: max(1, int(every_bars))]
    rows: list[dict[str, Any]] = []
    for name, series in columns.items():
        sampled = series.loc[series.index.intersection(stamps)].dropna()
        for stamp, value in sampled.items():
            for symbol in closes.columns:
                rows.append(
                    {
                        "timestamp": pd.Timestamp(stamp).isoformat(),
                        "name": name,
                        "value": round(float(value), 8),
                        "symbol": str(symbol),
                        "written_at": written_at,
                    }
                )
    rows.sort(key=lambda row: (row["timestamp"], row["name"], row["symbol"]))
    return rows


def declared_regimes(params: Mapping[str, Any]) -> tuple[str, ...]:
    """Validated regime declaration; an absent declaration means legacy mode."""
    raw = params.get("target_regimes")
    if raw is None or raw == []:
        return ()
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise ValueError("execution_params.target_regimes must be a list")
    values = tuple(dict.fromkeys(str(value).strip() for value in raw))
    invalid = sorted(set(values) - set(REGIME_LABELS))
    if invalid:
        raise ValueError(
            "execution_params.target_regimes contains unknown cells: "
            + ", ".join(invalid)
        )
    if not 1 <= len(values) <= 2:
        raise ValueError("target_regimes must declare one or two regime cells")
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
        labels = classify_regimes(ordered, vol_baseline_bars=vol_baseline_bars)
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
    from wayfinder_paths.jobs.execution.primitives import CompletedBarsView

    if not declared_regimes(params):
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
    declared = declared_regimes(params)
    if not declared:
        return {"enabled": False}
    return {
        "enabled": True,
        "classifier": PORTFOLIO_REGIME_CLASSIFIER,
        "current": current_portfolio_regime(view),
        "target": list(declared),
        "universe": list(regime_universe(params, view.symbols)),
    }
