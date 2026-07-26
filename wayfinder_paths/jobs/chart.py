"""Agent chart lenses: text chart browsing and historical-analog search.

`chart_job` is the agent's window onto price: any symbol/timeframe window of
the job dataset rendered as compact per-bar rows with whatever indicator
columns the agent asks for, forward trades annotated inline, and a regime
header — the numeric equivalent of dragging indicators onto the chart and
zooming into the hours around a trade.

`analogs_job` answers "when has this happened before?": z-scores a recent
close window, finds the nearest non-overlapping historical analogs, and
reports the forward-outcome distribution of the matches. Exploratory by
construction — anything it suggests still goes through the scan/holdout gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
from wayfinder_paths.jobs.execution.primitives import (
    ExecutionSpec,
    bar_interval_seconds,
)
from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
from wayfinder_paths.jobs.indicators import compute_indicators, regime_snapshot
from wayfinder_paths.jobs.research import resample_ohlcv
from wayfinder_paths.jobs.store import JobStore

MAX_CHART_ROWS = 240
TIMEFRAME_SECONDS = {
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def _load_symbol_frame(
    job_id: str, symbol: str | None, store: JobStore
) -> tuple[pd.DataFrame, str, int, Path]:
    root = store.job_dir(job_id)
    job_data = _load_job_yaml(root)
    spec_data, _ = resolve_execution_spec(root, job_data)
    spec = ExecutionSpec.from_dict(spec_data)
    dataset = _load_dataset(root, spec, job_data, include_store_features=True)
    frame = dataset.bars.to_frame()
    symbols = sorted(frame["symbol"].astype(str).unique())
    if symbol is None:
        symbol = symbols[0]
    if symbol not in symbols:
        raise ValueError(f"unknown symbol {symbol!r}; dataset has {symbols}")
    out = frame[frame["symbol"].astype(str) == symbol].reset_index(drop=True)
    declared = bar_interval_seconds(spec.data_contract.get("bar_interval"))
    if declared:
        bar_seconds = int(declared)
    else:
        stamps = pd.to_datetime(out["timestamp"], utc=True)
        bar_seconds = int(stamps.diff().median().total_seconds())
    return out, symbol, bar_seconds, root


def _resample(
    frame: pd.DataFrame, timeframe: str | None, bar_seconds: int
) -> tuple[pd.DataFrame, int]:
    if not timeframe:
        return frame, bar_seconds
    rule = TIMEFRAME_SECONDS.get(timeframe)
    if rule is None:
        raise ValueError(
            f"unknown timeframe {timeframe!r}; known: {sorted(TIMEFRAME_SECONDS)}"
        )
    return resample_ohlcv(frame, rule, bar_seconds=bar_seconds), rule


def _forward_trade_marks(
    root: Path, symbol: str, rule_seconds: int
) -> dict[str, list[str]]:
    """Bar-timestamp -> annotation labels from the forward record. Fills mark
    entries; trade closes mark exits (bucketed to the display timeframe's
    right-closed label)."""
    from wayfinder_paths.jobs.execution.driver import _read_jsonl_tail

    def _bucket(ts: pd.Timestamp) -> str:
        stamp = ts.ceil(f"{rule_seconds}s")
        return stamp.isoformat()

    marks: dict[str, list[str]] = {}
    fills = _read_jsonl_tail(root / "results" / "forward" / "fills.jsonl", 400)
    for fill in fills:
        if str(fill.get("symbol")) != symbol:
            continue
        ts = pd.Timestamp(str(fill.get("timestamp")))
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        meta = (fill.get("raw") or {}).get("intent_metadata") or {}
        if fill.get("reduce_only"):
            label = f"EXIT:{meta.get('exit_reason') or 'bracket_stop'}"
        else:
            label = f"ENTRY:{fill.get('side')}:{meta.get('entry_reason') or ''}"
        marks.setdefault(_bucket(ts), []).append(label.rstrip(":"))
    return marks


def chart_job(
    job_id: str,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    bars: int = 96,
    indicators: list[str] | None = None,
    around_trade: str | None = None,
    store: JobStore | None = None,
) -> dict[str, Any]:
    store = store or JobStore()
    frame, symbol, bar_seconds, root = _load_symbol_frame(job_id, symbol, store)
    frame, rule_seconds = _resample(frame, timeframe, bar_seconds)
    if frame.empty:
        raise ValueError("dataset has no bars for this symbol/timeframe")

    stamps = pd.to_datetime(frame["timestamp"], utc=True)
    window_note = None
    if around_trade:
        from wayfinder_paths.jobs.execution.driver import _read_jsonl_tail

        trades = [
            t
            for t in _read_jsonl_tail(root / "results" / "forward" / "trades.jsonl", 60)
            if str(t.get("symbol")) == symbol
        ]
        if not trades:
            raise ValueError(f"no forward trades for {symbol} to center on")
        if around_trade == "last":
            trade = trades[-1]
        else:
            wanted = pd.Timestamp(around_trade)
            if wanted.tzinfo is None:
                wanted = wanted.tz_localize("UTC")
            trade = min(
                trades,
                key=lambda t: abs(
                    pd.Timestamp(str(t.get("closed_at"))).tz_localize(None)
                    - wanted.tz_localize(None)
                ),
            )
        exit_ts = pd.Timestamp(str(trade.get("closed_at")))
        if exit_ts.tzinfo is None:
            exit_ts = exit_ts.tz_localize("UTC")
        last_stamp = stamps.iloc[-1]
        if exit_ts > last_stamp:
            # The dataset is refreshed by fetch_dataset/backtests, not by live
            # ticks — a trade newer than the last dataset bar would silently
            # chart the wrong window. Fail loud with the fix instead.
            raise ValueError(
                f"trade closed {exit_ts.isoformat()} is BEYOND the dataset "
                f"end ({last_stamp.isoformat()}) — refresh first with "
                'core_jobs(action="fetch_dataset", job_id=...) or '
                "`wayfinder job fetch-dataset`, then re-chart"
            )
        center = int((stamps <= exit_ts).sum()) - 1
        lo = max(0, center - bars // 2)
        window = frame.iloc[lo : lo + bars]
        window_note = f"centered on trade closed {exit_ts.isoformat()}"
    else:
        window = frame.tail(bars)
    window = window.tail(MAX_CHART_ROWS)

    indicator_cols = compute_indicators(frame, indicators or ["ema:9", "ema:50"])
    marks = _forward_trade_marks(root, symbol, rule_seconds)

    columns = ["ts", "open", "high", "low", "close", "volume"]
    columns += list(indicator_cols)
    columns.append("mark")
    rows: list[list[Any]] = []
    for idx in window.index:
        ts = pd.Timestamp(window.at[idx, "timestamp"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        row: list[Any] = [ts.isoformat()]
        for col in ("open", "high", "low", "close", "volume"):
            row.append(round(float(window.at[idx, col]), 6))
        for series in indicator_cols.values():
            value = series.loc[idx] if idx in series.index else np.nan
            row.append(None if pd.isna(value) else round(float(value), 6))
        row.append(";".join(marks.get(ts.isoformat(), [])) or None)
        rows.append(row)

    closes = window["close"].astype(float)
    window_return_bps = (
        (closes.iloc[-1] / closes.iloc[0] - 1) * 1e4 if len(closes) > 1 else 0.0
    )
    last_ts = pd.Timestamp(window["timestamp"].iloc[-1])
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize("UTC")
    header = {
        "symbol": symbol,
        "timeframe": timeframe or f"{bar_seconds}s",
        "bars_shown": len(rows),
        "window_return_bps": round(float(window_return_bps), 1),
        "window_range_bps": round(
            float(
                (window["high"].astype(float).max() - window["low"].astype(float).min())
                / closes.iloc[-1]
                * 1e4
            ),
            1,
        ),
        "regime_at_end": regime_snapshot(frame, last_ts),
        "window_note": window_note,
        "marks": sum(len(v) for v in marks.values()),
        # Data recency, always visible: the dataset is refreshed by
        # fetch_dataset/backtests, not live ticks — stale data silently
        # invalidates chart-based reasoning about recent trades.
        "dataset_end": (stamps.iloc[-1].isoformat() if len(stamps) else None),
    }
    return {
        "header": header,
        "columns": columns,
        "rows": rows,
        "read": (
            "Per-bar view with the requested indicator lenses; marks are "
            "forward entries/exits. A pattern seen here is an OBSERVATION — "
            "turn it into a workspace SignalDef or grid hypothesis and let "
            "the scan/holdout gate adjudicate."
        ),
    }


def analogs_job(
    job_id: str,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    window: int = 24,
    at: str | None = None,
    top: int = 15,
    horizon: int = 12,
    store: JobStore | None = None,
) -> dict[str, Any]:
    store = store or JobStore()
    frame, symbol, bar_seconds, _root = _load_symbol_frame(job_id, symbol, store)
    frame, _rule = _resample(frame, timeframe, bar_seconds)
    closes = frame["close"].astype(float).to_numpy()
    stamps = pd.to_datetime(frame["timestamp"], utc=True).reset_index(drop=True)
    if len(closes) < window * 3 + horizon:
        raise ValueError(
            f"need at least {window * 3 + horizon} bars, dataset has {len(closes)}"
        )

    if at is None:
        query_end = len(closes)
    else:
        wanted = pd.Timestamp(at)
        if wanted.tzinfo is None:
            wanted = wanted.tz_localize("UTC")
        query_end = int((stamps <= wanted).sum())
        if query_end < window:
            raise ValueError("not enough history before --at for the query window")
    query = closes[query_end - window : query_end]

    def _zscore(values: np.ndarray) -> np.ndarray | None:
        std = values.std()
        if not np.isfinite(std) or std == 0:
            return None
        return (values - values.mean()) / std

    query_z = _zscore(query)
    if query_z is None:
        raise ValueError("query window has zero variance")

    candidates: list[tuple[float, int]] = []
    # Candidate windows must leave room for the forward horizon and must not
    # overlap the query window itself.
    for start in range(0, query_end - 2 * window - horizon + 1):
        segment_z = _zscore(closes[start : start + window])
        if segment_z is None:
            continue
        distance = float(np.sqrt(np.mean((segment_z - query_z) ** 2)))
        candidates.append((distance, start))
    candidates.sort()

    matches: list[dict[str, Any]] = []
    taken: list[int] = []
    for distance, start in candidates:
        if any(abs(start - other) < window // 2 for other in taken):
            continue
        taken.append(start)
        end = start + window
        outcome_bps = (closes[end - 1 + horizon] / closes[end - 1] - 1) * 1e4
        matches.append(
            {
                "start_ts": stamps.iloc[start].isoformat(),
                "end_ts": stamps.iloc[end - 1].isoformat(),
                "distance": round(distance, 4),
                f"fwd_{horizon}bar_bps": round(float(outcome_bps), 1),
            }
        )
        if len(matches) >= top:
            break

    outcomes = np.array([m[f"fwd_{horizon}bar_bps"] for m in matches])
    summary = {
        "matches": len(matches),
        "mean_bps": round(float(outcomes.mean()), 1) if len(outcomes) else None,
        "median_bps": round(float(np.median(outcomes)), 1) if len(outcomes) else None,
        "hit_rate_up": round(float((outcomes > 0).mean()), 3)
        if len(outcomes)
        else None,
        "q25_bps": round(float(np.quantile(outcomes, 0.25)), 1)
        if len(outcomes)
        else None,
        "q75_bps": round(float(np.quantile(outcomes, 0.75)), 1)
        if len(outcomes)
        else None,
    }
    return {
        "symbol": symbol,
        "window_bars": window,
        "horizon_bars": horizon,
        "query_end_ts": stamps.iloc[query_end - 1].isoformat(),
        "summary": summary,
        "matches": matches,
        "read": (
            "EXPLORATORY analog search — nearest z-scored close windows and "
            "what followed them. This is hypothesis fuel, not evidence: no "
            "multiplicity control, shape-only matching. Chart the matches, "
            "form a rule, and put it through signal-scan/holdout before "
            "believing it."
        ),
    }
