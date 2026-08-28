"""Derived research features: the cross-symbol / exogenous / venue bridge.

Workspace signals and scans see ONE symbol's frame at a time, so any
information that lives BETWEEN symbols (correlation state, ratio spreads,
breadth), OUTSIDE the panel (BTC regime), or BETWEEN venues (basis) must
arrive as feature columns — the funding pattern: rows appended to
`state/features.jsonl`, carried into every frame by `merge_features` and the
resample. This op computes those rows deterministically from the job dataset
(+ a Hyperliquid candle fetch for exogenous/venue sets).

Resource discipline: features are written at a coarser cadence
(`every_bars`, default 12 = hourly on 5m bars) — merge_asof-backward gives
step-wise values between writes, which is the right semantic for regime-ish
features and keeps the store small. Research-side only: nothing here touches
the execution data contract; a strategy consuming one of these columns is a
proposal-gated change like any other.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
from wayfinder_paths.jobs.indicators import panel_breadth
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

CORR_BARS = 12
RATIO_Z_BARS = 96
BREADTH_SMA = 50
EXOG_TREND_SMA = 50
DEFAULT_EVERY_BARS = 12
MAX_APPEND_ROWS = 200_000
# Incremental exog/venue fetches reach back far enough for the widest rolling
# window used on fetched series, plus margin.
_FETCH_WARMUP_BARS = max(CORR_BARS, EXOG_TREND_SMA) * 2


def _native_hl_closes(coin: str, start_ms: int, end_ms: int) -> pd.Series:
    from wayfinder_paths.core.clients.HyperliquidDataClient import (
        HyperliquidDataClient,
    )

    async def _fetch() -> Any:
        client = HyperliquidDataClient()
        rows = await client.get_candles(
            coin, start_ms=start_ms, end_ms=end_ms, interval="5m"
        )
        return rows if isinstance(rows, list) else rows.get("rows", [])

    rows = asyncio.run(_fetch())
    if not rows:
        return pd.Series(dtype=float)
    from wayfinder_paths.jobs.execution.hyperliquid import (
        hyperliquid_candles_to_completed_view,
    )

    frame = hyperliquid_candles_to_completed_view(coin, rows).to_frame()
    return frame.set_index("timestamp")["close"].astype(float).sort_index()


def derive_features_job(
    job_id: str,
    *,
    sets: tuple[str, ...] = ("cross", "exog"),
    exog_symbols: tuple[str, ...] = ("BTC",),
    every_bars: int = DEFAULT_EVERY_BARS,
    store: JobStore | None = None,
    fetch_closes: Callable[[str, int, int], pd.Series] | None = None,
) -> dict[str, Any]:
    """Compute and append derived feature rows. Sets: cross, exog, venue."""
    unknown = set(sets) - {"cross", "exog", "venue", "regime"}
    if unknown:
        raise ValueError(f"unknown feature sets: {sorted(unknown)}")
    store = store or JobStore()
    root = store.job_dir(job_id)
    job_data = _load_job_yaml(root)
    spec_data, _ = resolve_execution_spec(root, job_data)
    dataset = _load_dataset(root, ExecutionSpec.from_dict(spec_data), job_data)
    frame = dataset.bars.to_frame()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    closes = frame.pivot_table(
        index="timestamp", columns="symbol", values="close", aggfunc="last"
    ).sort_index()
    symbols = [str(s) for s in closes.columns]
    fetch = fetch_closes or _native_hl_closes

    columns: dict[str, pd.DataFrame] = {}

    def _add(name: str, wide: pd.DataFrame | pd.Series) -> None:
        # A Series is panel-wide (same value for every symbol).
        if isinstance(wide, pd.Series):
            wide = pd.DataFrame(dict.fromkeys(symbols, wide))
        columns[name] = wide

    if "cross" in sets:
        returns = closes.pct_change()
        # Basket-relative dislocation: each symbol's log price vs the
        # equal-weight (geometric) basket, z-scored — varies cross-
        # sectionally by construction, so it is the natural rank-IC input
        # (pair-wise ratioz_<sym> columns have a self-hole and panel-wide
        # exog columns are cross-sectionally constant; neither can rank).
        log_closes = np.log(closes)
        basket_spread = log_closes.sub(log_closes.mean(axis=1), axis=0)
        basket_z = (
            basket_spread - basket_spread.rolling(RATIO_Z_BARS).mean()
        ) / basket_spread.rolling(RATIO_Z_BARS).std()
        _add(f"ratioz_basket{RATIO_Z_BARS}", basket_z)
        for symbol in symbols:
            for sibling in symbols:
                if sibling == symbol:
                    continue
                corr = returns[symbol].rolling(CORR_BARS).corr(returns[sibling])
                columns.setdefault(
                    f"corr_{sibling.lower()}{CORR_BARS}", pd.DataFrame()
                )[symbol] = corr
                ratio = np.log(closes[symbol] / closes[sibling])
                z = (ratio - ratio.rolling(RATIO_Z_BARS).mean()) / ratio.rolling(
                    RATIO_Z_BARS
                ).std()
                columns.setdefault(
                    f"ratioz_{sibling.lower()}{RATIO_Z_BARS}", pd.DataFrame()
                )[symbol] = z
        means = closes.rolling(BREADTH_SMA).mean()
        above = closes.gt(means).where(closes.notna() & means.notna()).astype(float)
        breadth = panel_breadth(above, 1.0, min_assets=len(symbols))
        _add(f"breadth_sma{BREADTH_SMA}", breadth * len(symbols))
        _add("panelret_lag1", returns.mean(axis=1).shift(1))

    if "regime" in sets:
        from wayfinder_paths.jobs.indicators import REGIME_LABELS, classify_regimes

        # Integer-coded (the feature store is numeric): index into
        # REGIME_LABELS — 0=up_lowvol 1=up_highvol 2=down_lowvol 3=down_highvol.
        code_by_label = {label: float(i) for i, label in enumerate(REGIME_LABELS)}
        regime_wide = pd.DataFrame(index=closes.index)
        for symbol in symbols:
            sym_frame = (
                frame[frame["symbol"].astype(str) == symbol]
                .sort_values("timestamp")
                .reset_index(drop=True)
            )
            labels = classify_regimes(sym_frame).map(code_by_label)
            regime_wide[symbol] = pd.Series(
                labels.to_numpy(),
                index=pd.to_datetime(sym_frame["timestamp"], utc=True),
            ).reindex(closes.index)
        columns["regime_code"] = regime_wide

    # Newest stored stamp PER SERIES, streamed (never a per-row key set — at
    # the live store's 600k+ rows that set alone was ~100MB+ of tuples).
    # Dedup semantics: appends are chronological per series (incremental
    # fetches only extend the tail), so "strictly newer than the series'
    # newest" is equivalent to the old exact-key check.
    features_path = root / "state" / "features.jsonl"
    features_path.parent.mkdir(parents=True, exist_ok=True)
    newest_by_series: dict[tuple[str, str], str] = {}
    funding_rows: list[dict[str, Any]] = []
    market_first_ts = pd.Timestamp(frame["timestamp"].min())
    market_last_ts = pd.Timestamp(frame["timestamp"].max())
    if features_path.exists():
        with features_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                series = (str(row.get("name")), str(row.get("symbol")))
                ts = str(row.get("timestamp"))
                if ts > newest_by_series.get(series, ""):
                    newest_by_series[series] = ts
                if series[0] == "funding":
                    try:
                        funding_ts = pd.Timestamp(ts)
                    except (TypeError, ValueError):
                        continue
                    funding_ts = (
                        funding_ts.tz_localize("UTC")
                        if funding_ts.tzinfo is None
                        else funding_ts.tz_convert("UTC")
                    )
                    if market_first_ts <= funding_ts <= market_last_ts:
                        funding_rows.append(row)
    newest_existing = max(newest_by_series.values(), default="")

    if "exog" in sets or "venue" in sets:
        start_ms = int(closes.index[0].timestamp() * 1000)
        end_ms = int(closes.index[-1].timestamp() * 1000) + 300_000
        if newest_existing:
            # Incremental: rows before the newest stored stamp are dedup'd
            # away anyway, so fetch only the tail plus rolling-window warmup.
            # The full-span fetch (120d of 5m bars, paginated, every ~30min,
            # per job) was the request storm behind the 429/credit burn.
            try:
                bar_step = (
                    closes.index[1] - closes.index[0]
                    if len(closes.index) > 1
                    else pd.Timedelta(minutes=5)
                )
                warm_start = pd.Timestamp(newest_existing) - bar_step * (
                    _FETCH_WARMUP_BARS
                )
                start_ms = max(start_ms, int(warm_start.timestamp() * 1000))
            except (ValueError, TypeError):
                pass  # unparseable stamp → keep the full window
        if "exog" in sets:
            for coin in exog_symbols:
                exog = fetch(coin, start_ms, end_ms).reindex(closes.index).ffill()
                _add(f"{coin.lower()}_ret{CORR_BARS}", exog.pct_change(CORR_BARS))
                _add(
                    f"{coin.lower()}_trend",
                    (exog > exog.rolling(EXOG_TREND_SMA).mean()).astype(float),
                )
        if "venue" in sets:
            for symbol in symbols:
                hl = fetch(symbol, start_ms, end_ms).reindex(closes.index).ffill()
                basis = (hl / closes[symbol] - 1) * 1e4
                columns.setdefault("venue_basis_bps", pd.DataFrame())[symbol] = basis

    # Serialize at the coarse cadence with the fetch-funding dedup semantics.
    written_at = utc_now_iso()
    stamps = closes.index[::every_bars]
    appended: dict[str, int] = {}
    total = 0
    with features_path.open("a", encoding="utf-8") as handle:
        for name, wide in columns.items():
            for symbol in symbols:
                if symbol not in getattr(wide, "columns", []):
                    continue
                series = wide[symbol].loc[wide.index.intersection(stamps)].dropna()
                for ts, value in series.items():
                    if total >= MAX_APPEND_ROWS:
                        raise ValueError(
                            f"append cap {MAX_APPEND_ROWS} hit — raise every_bars"
                        )
                    ts_iso = ts.isoformat()
                    if ts_iso <= newest_by_series.get((name, symbol), ""):
                        continue
                    newest_by_series[(name, symbol)] = ts_iso
                    handle.write(
                        json.dumps(
                            {
                                "timestamp": ts.isoformat(),
                                "name": name,
                                "value": round(float(value), 8),
                                "symbol": symbol,
                                "written_at": written_at,
                            }
                        )
                        + "\n"
                    )
                    appended[name] = appended.get(name, 0) + 1
                    total += 1

    # Reuse the dataset already resident in memory. Loading the 120-day bars
    # a second time in the regime monitor doubled an hourly wake's peak memory
    # on the small runner boxes. The monitor consumes this compact artifact.
    from wayfinder_paths.jobs.regime_contract import MARKET_STATE_PATH
    from wayfinder_paths.jobs.regime_market import write_market_state

    market_state = write_market_state(root, frame, funding_rows=funding_rows)
    return {
        "sets": list(sets),
        "every_bars": every_bars,
        "features": sorted(columns),
        "rows_appended": total,
        "per_feature": dict(sorted(appended.items())),
        # Newest stamp in the store after this run — rows_appended == 0 is
        # NORMAL between hourly stamps; THIS is the staleness signal.
        "newest_feature_ts": max(newest_by_series.values(), default=""),
        "market_state": {
            "available": market_state.get("available"),
            "as_of": market_state.get("as_of"),
            "path": str(root / MARKET_STATE_PATH),
        },
        "generated_at": str(dt.datetime.now(dt.UTC)),
        "read": (
            "Research-side derived features appended to the feature store — "
            "they now ride merge_features into every symbol frame for "
            "workspace signals, --column checks, and rank-check. Values are "
            "step-wise between writes (coarse cadence by design). Consuming "
            "one in the LIVE strategy is a proposal-gated change."
        ),
    }


REFRESH_STAMP_PATH = "results/research/derived_refresh.json"
# Just under the hourly design cadence so a 30m wake rhythm refreshes every
# other wake instead of aliasing to 90m.
REFRESH_MAX_AGE_S = 3300
_REFRESH_SETS = ("cross", "exog", "venue", "regime")
# Features derive over the job DATASET, so they can never advance past its
# newest bar. The dataset historically refreshed only as a side effect of
# applies/validations — features froze for 15h+ between agent activity.
DATASET_STALE_S = 2 * 3600
# Consecutive refresh failures before the degradation escalates to the
# decision log (owner-visible) instead of only journal noise.
_DEGRADED_AFTER_FAILURES = 3
# Newest feature stamp older than this despite "successful" refreshes is a
# degradation too (a wedged feed that fails silent).
_FEATURES_STALE_ALARM_S = 6 * 3600


# Compact the append-only store once it outgrows this — the live store hit
# 95MB/620k rows, filled the disk once, and rode through every backtest and
# wake in full.
_COMPACT_THRESHOLD_BYTES = 32 * 1024 * 1024
# Keep the full evidence window plus margin so gate backtests and scans see
# an unchanged store.
_COMPACT_KEEP_DAYS = 130.0


def compact_feature_store(
    root: Path, *, keep_days: float = _COMPACT_KEEP_DAYS
) -> dict[str, Any] | None:
    """Rewrite features.jsonl keeping rows newer than the cutoff plus each
    series' as-of anchor (newest row at/before cutoff) — merge_asof(backward)
    observes identical values everywhere inside the kept window."""
    path = root / "state" / "features.jsonl"
    if not path.exists():
        return None
    before = path.stat().st_size
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=keep_days)).isoformat()
    anchors: dict[tuple[str, str], tuple[str, str]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            ts = str(row.get("timestamp"))
            if ts >= cutoff:
                continue
            series = (str(row.get("name")), str(row.get("symbol")))
            if ts > anchors.get(series, ("", ""))[0]:
                anchors[series] = (ts, line if line.endswith("\n") else line + "\n")
    kept = 0
    dropped = 0
    tmp = path.with_suffix(".jsonl.compact")
    with tmp.open("w", encoding="utf-8") as out:
        for _, line in sorted(anchors.values()):
            out.write(line)
            kept += 1
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    dropped += 1
                    continue
                if isinstance(row, dict) and str(row.get("timestamp")) >= cutoff:
                    out.write(line if line.endswith("\n") else line + "\n")
                    kept += 1
                else:
                    dropped += 1
    tmp.replace(path)
    dropped -= len(anchors)  # anchor rows were re-written, not dropped
    return {
        "before_bytes": before,
        "after_bytes": path.stat().st_size,
        "rows_kept": kept,
        "rows_dropped": max(dropped, 0),
    }


def _feed_cause(error: str) -> str:
    for cause in ("out_of_credits", "rate_limited"):
        if cause in error:
            return cause
    return "unknown"


def _refresh_dataset_if_stale(job_id: str, store: JobStore, root: Any) -> str | None:
    """Extend the job dataset's tail when its newest bar has aged out.

    Reuses build_live_dataset's incremental path with the dataset's OWN
    recorded provenance so only the tail is fetched. Returns a note string
    for the caller's result payload, or None when nothing was needed."""
    path = root / "results" / "backtest" / "input_bars.json"
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    meta = doc.get("metadata") if isinstance(doc, dict) else None
    rows = doc.get("bars") if isinstance(doc, dict) else None
    if not isinstance(meta, dict) or not isinstance(rows, list) or not rows:
        return None
    days = int(float(meta.get("days") or 0))
    if not days:
        return None
    newest = max((str(row.get("timestamp") or "") for row in rows[-500:]), default="")
    try:
        age = (
            dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(newest)
        ).total_seconds()
    except (ValueError, TypeError):
        return None
    if age < DATASET_STALE_S:
        return None
    # circular import: preflight chains back into this module's refresh
    from wayfinder_paths.jobs.execution.preflight import build_live_dataset

    if str(meta.get("source") or "") == "ccxt":
        kwargs: dict[str, Any] = {
            "source": "ccxt",
            "exchange": str(meta.get("exchange") or "binance"),
        }
    else:
        kwargs = {"source": "venues"}
    build_live_dataset(job_id, days=days, store=store, **kwargs)
    return f"dataset tail refreshed (was {int(age)}s stale)"


def refresh_derived_features_if_stale(
    job_id: str,
    *,
    store: JobStore | None = None,
    max_age_seconds: int = REFRESH_MAX_AGE_S,
    derive: Callable[..., dict[str, Any]] | None = None,
    refresh_dataset: bool = True,
) -> dict[str, Any]:
    """Keep research-side derived features live from the wake path.

    The derive op alone rots: a one-time backfill leaves btc_trend/cross
    columns silently frozen (they merge cleanly into scans with stale
    values). The consumer of these columns is the agent wake's research, so
    the wake is the refresh point — stamp-gated to the design's hourly
    cadence, and NEVER raising: a broken exog fetch degrades to a journaled
    skip, not a dead wake.

    Degradations escalate: >=3 consecutive failures, or features stale past
    the alarm window despite successful refreshes, journal a single
    `data_feed_degraded` event (owner-visible via the decision log) carrying
    the structured cause; recovery journals `data_feed_recovered` once."""
    store = store or JobStore()
    stamp = store.read_json(job_id, REFRESH_STAMP_PATH) or {}
    refreshed_at = str(stamp.get("refreshed_at") or "")
    if refreshed_at:
        age = (
            dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(refreshed_at)
        ).total_seconds()
        if age < max_age_seconds:
            return {"refreshed": False, "reason": f"fresh ({int(age)}s old)"}

    dataset_note: str | None = None
    if refresh_dataset:
        try:
            dataset_note = _refresh_dataset_if_stale(
                job_id, store, store.job_dir(job_id)
            )
        except Exception as exc:  # noqa: BLE001 — derive over the old dataset instead
            store.append_journal(
                job_id,
                {
                    "type": "derived_features_refresh_failed",
                    "error": f"dataset refresh: {str(exc)[:280]}",
                },
            )
            dataset_note = f"dataset refresh failed: {str(exc)[:120]}"

    run = derive or derive_features_job
    try:
        result = run(job_id, sets=_REFRESH_SETS, store=store)
    except Exception as exc:  # noqa: BLE001 — wake must not die on research prep
        error = str(exc)[:300]
        store.append_journal(
            job_id,
            {"type": "derived_features_refresh_failed", "error": error},
        )
        failures = int(stamp.get("consecutive_failures") or 0) + 1
        stamp["consecutive_failures"] = failures
        if failures >= _DEGRADED_AFTER_FAILURES and not stamp.get("degraded_since"):
            stamp["degraded_since"] = str(dt.datetime.now(dt.UTC))
            store.append_journal(
                job_id,
                {
                    "type": "data_feed_degraded",
                    "cause": _feed_cause(error),
                    "consecutive_failures": failures,
                    "error": error,
                },
            )
        store.write_json(job_id, REFRESH_STAMP_PATH, stamp)
        return {"refreshed": False, "reason": f"failed: {exc}"}

    newest_feature = str(result.get("newest_feature_ts") or "")
    feature_age: float | None = None
    if newest_feature:
        try:
            feature_age = (
                dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(newest_feature)
            ).total_seconds()
        except (ValueError, TypeError):
            feature_age = None
    if (
        feature_age is not None
        and feature_age > _FEATURES_STALE_ALARM_S
        and not stamp.get("degraded_since")
    ):
        stamp["degraded_since"] = str(dt.datetime.now(dt.UTC))
        store.append_journal(
            job_id,
            {
                "type": "data_feed_degraded",
                "cause": "features_stale",
                "newest_feature_ts": newest_feature,
                "error": f"newest feature {int(feature_age)}s old after a "
                "successful refresh",
            },
        )
    elif stamp.get("degraded_since") and (
        feature_age is None or feature_age <= _FEATURES_STALE_ALARM_S
    ):
        store.append_journal(
            job_id,
            {"type": "data_feed_recovered", "newest_feature_ts": newest_feature},
        )
        stamp.pop("degraded_since", None)

    root = store.job_dir(job_id)
    features_path = root / "state" / "features.jsonl"
    if (
        features_path.exists()
        and features_path.stat().st_size > _COMPACT_THRESHOLD_BYTES
    ):
        try:
            compacted = compact_feature_store(root)
        except Exception as exc:  # noqa: BLE001 — compaction must not kill a wake
            compacted = None
            store.append_journal(
                job_id,
                {
                    "type": "derived_features_refresh_failed",
                    "error": f"feature store compaction: {str(exc)[:250]}",
                },
            )
        if compacted:
            store.append_journal(
                job_id, {"type": "feature_store_compacted", **compacted}
            )

    stamp.update(
        {
            "refreshed_at": str(dt.datetime.now(dt.UTC)),
            "rows_appended": result.get("rows_appended"),
            "newest_feature_ts": newest_feature,
            "sets": result.get("sets"),
            "consecutive_failures": 0,
        }
    )
    if dataset_note:
        stamp["dataset_note"] = dataset_note
    else:
        stamp.pop("dataset_note", None)
    store.write_json(job_id, REFRESH_STAMP_PATH, stamp)
    return {
        "refreshed": True,
        "rows_appended": result.get("rows_appended"),
        **({"dataset": dataset_note} if dataset_note else {}),
    }
