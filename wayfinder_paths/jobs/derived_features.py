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
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

CORR_BARS = 12
RATIO_Z_BARS = 96
BREADTH_SMA = 50
EXOG_TREND_SMA = 50
DEFAULT_EVERY_BARS = 12
MAX_APPEND_ROWS = 200_000


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
    stamps = [pd.Timestamp(int(r["t"]), unit="ms", tz="UTC") for r in rows]
    return pd.Series([float(r["c"]) for r in rows], index=pd.Index(stamps)).sort_index()


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
        above = (closes > closes.rolling(BREADTH_SMA).mean()).sum(axis=1)
        _add(f"breadth_sma{BREADTH_SMA}", above.astype(float))
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

    if "exog" in sets or "venue" in sets:
        start_ms = int(closes.index[0].timestamp() * 1000)
        end_ms = int(closes.index[-1].timestamp() * 1000) + 300_000
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
    features_path = root / "state" / "features.jsonl"
    features_path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[tuple[str, str, str]] = set()
    if features_path.exists():
        for line in features_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                existing.add(
                    (
                        str(row.get("timestamp")),
                        str(row.get("name")),
                        str(row.get("symbol")),
                    )
                )
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
                    key = (ts.isoformat(), name, symbol)
                    if key in existing:
                        continue
                    existing.add(key)
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
    return {
        "sets": list(sets),
        "every_bars": every_bars,
        "features": sorted(columns),
        "rows_appended": total,
        "per_feature": dict(sorted(appended.items())),
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


def refresh_derived_features_if_stale(
    job_id: str,
    *,
    store: JobStore | None = None,
    max_age_seconds: int = REFRESH_MAX_AGE_S,
    derive: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Keep research-side derived features live from the wake path.

    The derive op alone rots: a one-time backfill leaves btc_trend/cross
    columns silently frozen (they merge cleanly into scans with stale
    values). The consumer of these columns is the agent wake's research, so
    the wake is the refresh point — stamp-gated to the design's hourly
    cadence, and NEVER raising: a broken exog fetch degrades to a journaled
    skip, not a dead wake."""
    store = store or JobStore()
    stamp = store.read_json(job_id, REFRESH_STAMP_PATH) or {}
    refreshed_at = str(stamp.get("refreshed_at") or "")
    if refreshed_at:
        age = (
            dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(refreshed_at)
        ).total_seconds()
        if age < max_age_seconds:
            return {"refreshed": False, "reason": f"fresh ({int(age)}s old)"}
    run = derive or derive_features_job
    try:
        result = run(job_id, sets=_REFRESH_SETS, store=store)
    except Exception as exc:  # noqa: BLE001 — wake must not die on research prep
        store.append_journal(
            job_id,
            {"type": "derived_features_refresh_failed", "error": str(exc)[:300]},
        )
        return {"refreshed": False, "reason": f"failed: {exc}"}
    store.write_json(
        job_id,
        REFRESH_STAMP_PATH,
        {
            "refreshed_at": str(dt.datetime.now(dt.UTC)),
            "rows_appended": result.get("rows_appended"),
            "sets": result.get("sets"),
        },
    )
    return {"refreshed": True, "rows_appended": result.get("rows_appended")}
