from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from wayfinder_paths.jobs.backtest_artifacts import (
    VIEW_KINDS,
    _in_range,
    _parse_ts,
    order_series_for_display,
)
from wayfinder_paths.jobs.forward import (
    default_forward_summary,
    read_jsonl,
    rebuild_forward_curve,
)
from wayfinder_paths.jobs.models import (
    DEFAULT_FORWARD_CURVE,
    DEFAULT_FORWARD_FILLS,
    DEFAULT_FORWARD_SUMMARY,
    DEFAULT_FORWARD_TICKS,
    DEFAULT_FORWARD_TRADES,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.monitor_state import atomic_write_text

# Chart context beyond the first forward tick, and a hard fetch cap — the
# forward window grows unboundedly, the chart payload must not.
_WARMUP_BARS = 24
_MAX_PRICE_BARS = 2000
# Price bars persisted between builds, {open_ms: [open, high, low, close,
# volume]} per symbol; the chart is polled far more often than a bar closes.
_PRICE_CACHE_NAME = "price_cache.json"
_Bars = dict[int, list[float | None]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def forward_pnl_breakdown(forward_dir: Path) -> dict[str, Any]:
    """Paper-vs-live split of closed forward trades, from trades.jsonl.

    Every forward record carries the mode it executed under (ForwardRecorder
    bakes it in), so the split is a group-by — no separate ledgers exist.
    Returns {"pnl_by_mode": {...}, "trades_by_mode": {...}} with both modes
    always present so consumers can tell "no live trades" (count 0) from
    "live is flat" (pnl 0.0).
    """
    pnl = {"paper": 0.0, "live": 0.0}
    counts = {"paper": 0, "live": 0}
    for row in read_jsonl(forward_dir / Path(DEFAULT_FORWARD_TRADES).name):
        mode = str(row.get("mode") or "paper")
        if mode not in pnl:
            continue
        raw_pnl = row.get("net_pnl")
        if raw_pnl is None:
            continue
        pnl[mode] += float(raw_pnl)
        counts[mode] += 1
    return {"pnl_by_mode": pnl, "trades_by_mode": counts}


def forward_open_position(
    forward_dir: Path, *, last_closes: dict[str, float] | None = None
) -> dict[str, Any] | None:
    """The currently-open position (if any) from the latest tick's ledger,
    with unrealized PnL marked at the last known close when available."""
    tick = _last_jsonl_row(forward_dir / Path(DEFAULT_FORWARD_TICKS).name)
    if tick is None:
        return None
    # Skipped ticks (no_new_bar) record an empty top-level ledger; the real
    # unchanged state lives in engine_state_pre. Without this fallback the
    # open position vanishes from the snapshot on every between-bar tick.
    ledger = tick.get("ledger") or (
        (tick.get("engine_state_pre") or {}).get("ledger") or {}
    )
    positions = ledger.get("positions") or {}
    for symbol, position in positions.items():
        side = str(position.get("side") or "")
        size = float(position.get("size") or 0.0)
        avg_price = float(position.get("avg_price") or 0.0)
        if not size:
            continue
        result: dict[str, Any] = {
            "symbol": str(symbol),
            "side": side,
            "size": size,
            "avg_price": avg_price,
            "opened_at": position.get("opened_at"),
            "mode": tick.get("mode"),
        }
        last_close = (last_closes or {}).get(str(symbol))
        if last_close is not None and avg_price:
            direction = -1.0 if side == "short" else 1.0
            result["unrealized_pnl"] = direction * (last_close - avg_price) * size
            result["marked_at_price"] = last_close
        return result  # engine holds at most one position per strategy today
    return None


def load_forward_view(
    job_id: str,
    *,
    store: JobStore | None = None,
    view: str = "all",
    series_names: list[str] | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    max_points: int = 1500,
    include_prices: bool = True,
    price_fetcher: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Bounded forward (paper/live) visualization payload for the jobs UI.

    Mirrors `load_backtest_view`'s response shape so the whole chart pipeline
    (backend proxy + FE renderer) is reused, but is built on demand from the
    forward artifacts instead of a pre-written visualization.json:
    - markers from fills.jsonl, each tagged with the MODE it executed under
    - a PnL curve from the curve ledger (the chart-facing fields of each
      tick; the full tick ledger is replay evidence and is never parsed here)
    - market_price OHLC series fetched through the same venue feed the driver
      uses (forward ticks don't persist bars); on fetch failure the payload
      degrades to markers + PnL with a `price_note` instead of failing.
    """
    store = store or JobStore()
    forward_dir = store.job_dir(job_id) / "results" / "forward"
    fills = read_jsonl(forward_dir / Path(DEFAULT_FORWARD_FILLS).name)
    ticks = _forward_curve(forward_dir)
    if not fills and not ticks:
        return {"available": False}

    markers = _fill_markers(fills)
    trades = _closed_trades(forward_dir, fills)
    series: list[dict[str, Any]] = [_pnl_series(job_id, ticks, store=store)]
    # The script's own reads (funding, token values, yields) come straight
    # from the tick rows; a harnessed job's declared feature feeds come from
    # its feature store. Both are cheap and never hit a venue.
    series.extend(_read_series(ticks))
    try:
        if not _lifecycle_script(store.load(job_id)):
            first_tick = _parse_ts(
                str(ticks[0].get("bar_ts") or ticks[0].get("ts")) if ticks else None
            )
            series.extend(feature_series(store, job_id, start=first_tick))
    except Exception:  # noqa: BLE001 — a feature store must never hide the run
        pass

    price_note: str | None = None
    last_closes: dict[str, float] = {}
    if include_prices:
        try:
            # `price_fetcher` lets a resident caller (runner view server)
            # inject a TTL-cached fetch so UI polling cannot hammer the venue;
            # default is the direct per-call venue fetch.
            fetch_prices = price_fetcher or _fetch_price_series
            price_series = fetch_prices(job_id, ticks, store=store)
            series.extend(price_series)
            for entry in price_series:
                if entry["points"]:
                    last_closes[str(entry["symbol"])] = float(
                        entry["points"][-1]["close"]
                    )
        except Exception as exc:  # degrade: chart still shows PnL + markers
            price_note = f"price series unavailable: {exc}"

    summary_path = forward_dir / Path(DEFAULT_FORWARD_SUMMARY).name
    summary = _read_json(summary_path) or default_forward_summary(job_id)
    summary = {
        **summary,
        **forward_pnl_breakdown(forward_dir),
        "open_position": forward_open_position(forward_dir, last_closes=last_closes),
    }
    if price_note:
        summary["price_note"] = price_note

    requested = {item.strip() for item in series_names or [] if item.strip()}
    bounded_max = min(max(max_points, 100), 10_000)
    start = _parse_ts(from_ts)
    end = _parse_ts(to_ts)
    kinds = VIEW_KINDS.get(view)
    selected_series = []
    for entry in series:
        if requested and entry["name"] not in requested:
            continue
        if kinds is not None and entry["kind"] not in kinds:
            continue
        points = [
            point
            for point in entry["points"]
            if _in_range(_parse_ts(point["timestamp"]), start, end)
        ]
        if len(points) > bounded_max:
            # Even-stride downsample keeping first/last (same as backtest view).
            last_index = len(points) - 1
            points = [
                points[math.floor(index * last_index / (bounded_max - 1))]
                for index in range(bounded_max)
            ]
        selected_series.append({**entry, "points": points})
    symbols = {
        str(entry["symbol"])
        for entry in selected_series
        if entry.get("symbol") is not None
    }
    selected_markers = [
        marker
        for marker in markers
        if _in_range(_parse_ts(marker["timestamp"]), start, end)
        and (view != "legs" or not symbols or str(marker["symbol"]) in symbols)
    ]
    events = [
        event
        for event in forward_events(ticks, proposals=store.proposals(job_id))
        if _in_range(_parse_ts(event["timestamp"]), start, end)
    ]
    return {
        "available": True,
        "view": view,
        "summary": summary,
        "visualization": {
            "schema_version": "1.0",
            "source": "forward",
            "symbols": sorted(
                {
                    str(entry["symbol"])
                    for entry in series
                    if entry.get("symbol") is not None
                }
            ),
            "series": order_series_for_display(selected_series, selected_markers),
            "markers": selected_markers,
            "events": events,
        },
        # Full entry-joined trade record (direction, duration, reasons) —
        # replaces the raw 50-row tail the UI could not interpret.
        "trades": trades,
    }


def _forward_curve(forward_dir: Path) -> list[dict[str, Any]]:
    """The chart's per-tick rows from curve.jsonl, rebuilt from ticks.jsonl
    once when the curve is missing or its last row is not the last tick (a
    job recorded before the curve existed, or a torn append)."""
    last_tick = _last_jsonl_row(forward_dir / Path(DEFAULT_FORWARD_TICKS).name)
    if last_tick is None:
        return []
    curve_path = forward_dir / Path(DEFAULT_FORWARD_CURVE).name
    last_curve = _last_jsonl_row(curve_path)
    if last_curve is None or _tick_stamp(last_curve) != _tick_stamp(last_tick):
        rebuild_forward_curve(forward_dir)
    return read_jsonl(curve_path)


def _tick_stamp(row: Mapping[str, Any]) -> str | None:
    stamp = row.get("ts") or row.get("bar_ts")
    return str(stamp) if stamp else None


def _last_jsonl_row(path: Path, *, chunk_bytes: int = 8192) -> dict[str, Any] | None:
    """The last complete object row of a JSONL ledger, read from the file's
    tail — learning the final tick must not stream a 32 MB ledger. A torn or
    blank last line yields the row before it, as `tail_jsonl` does."""
    if not path.exists():
        return None
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        window = min(size, chunk_bytes)
        while True:
            handle.seek(size - window)
            lines = handle.read(window).split(b"\n")
            # Unless the window is the whole file its first fragment may
            # start mid-line, so it is never a candidate.
            for line in reversed(lines if window == size else lines[1:]):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if isinstance(row, dict):
                    return row
            if window == size:
                return None
            window = min(size, window * 2)


def forward_events(
    ticks: list[dict[str, Any]],
    *,
    proposals: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Lifecycle annotations for the forward chart, derived from the ticks.

    Every tick records the mode and revision it executed under, so transitions
    between consecutive ticks ARE the job's history: paper<->live flips, a new
    strategy version taking effect (labeled with the applied proposal's summary
    when one matches the revision), and halts engaging. The chart draws these
    as vertical event lines.
    """
    summary_by_revision: dict[str, str] = {}
    for proposal in proposals or []:
        proposal_revision = str(
            (proposal.get("candidate_report") or {}).get("revision")
            or proposal.get("candidate_revision")
            or ""
        )
        proposal_summary = str(proposal.get("summary") or "").strip()
        if proposal_revision and proposal_summary:
            summary_by_revision[proposal_revision] = proposal_summary

    events: list[dict[str, Any]] = []
    previous_mode: str | None = None
    previous_revision: str | None = None
    previous_halted = False
    for tick in ticks:
        timestamp = tick.get("bar_ts") or tick.get("ts")
        if not timestamp:
            continue
        mode = str(tick.get("mode") or "") or None
        revision = str(tick.get("revision") or "") or None
        if previous_mode is not None and mode and mode != previous_mode:
            events.append(
                {
                    "timestamp": str(timestamp),
                    "kind": "mode_flip",
                    "mode": mode,
                    "label": f"→ {mode.upper()}",
                }
            )
        if previous_revision is not None and revision and revision != previous_revision:
            summary = summary_by_revision.get(revision)
            events.append(
                {
                    "timestamp": str(timestamp),
                    "kind": "revision",
                    "revision": revision,
                    "label": (summary[:60] if summary else f"update {revision[:8]}"),
                }
            )
        guards = {str(guard.get("kind")) for guard in tick.get("guard_events") or []}
        halted = bool(guards & {"risk_halt", "manual_halt"})
        if halted and not previous_halted:
            reasons = [
                str(guard.get("reason") or "")
                for guard in tick.get("guard_events") or []
                if str(guard.get("kind")) in {"risk_halt", "manual_halt"}
            ]
            events.append(
                {
                    "timestamp": str(timestamp),
                    "kind": "halt",
                    "label": (reasons[0][:60] if reasons and reasons[0] else "halted"),
                }
            )
        previous_mode = mode or previous_mode
        previous_revision = revision or previous_revision
        previous_halted = halted
    return events


def _fill_markers(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    markers = []
    for fill in fills:
        if str(fill.get("status") or "filled") != "filled":
            continue
        timestamp = fill.get("timestamp") or fill.get("ts")
        if not timestamp:
            continue
        kind = "exit" if fill.get("reduce_only") else "entry"
        mode = str(fill.get("mode") or "paper")
        meta = _intent_metadata(fill)
        reason = meta.get("entry_reason") or meta.get("exit_reason")
        # POSITION direction, not fill side: a buy that reduces closes a
        # SHORT. The chart should say long/short — buy/sell is ambiguous.
        fill_side = str(fill.get("side") or "").lower()
        if kind == "entry":
            direction = "long" if fill_side == "buy" else "short"
        else:
            direction = "short" if fill_side == "buy" else "long"
        markers.append(
            {
                "timestamp": str(timestamp),
                "symbol": str(fill.get("symbol") or ""),
                "side": fill.get("side"),
                "direction": direction,
                "price": fill.get("avg_price"),
                "kind": kind,
                "mode": mode,
                "venue": fill.get("venue"),
                "label": f"{mode} {direction} {kind}"
                + (f": {reason}" if reason else ""),
            }
        )
    markers.sort(key=lambda marker: str(marker["timestamp"]))
    return markers


def _closed_trades(
    forward_dir: Path, fills: list[dict[str, Any]], *, limit: int = 500
) -> list[dict[str, Any]]:
    """Every closed trade, entry-joined: direction, entry/exit ts+px,
    duration, reasons. The snapshot's recent_trades tail is for the wake
    prompt; this is the owner's full record."""
    import pandas as pd

    from wayfinder_paths.jobs.trade_forensics import (
        _closing_fill_reason,
        match_entry_fill,
        position_side_of_close,
    )

    rows = read_jsonl(forward_dir / Path(DEFAULT_FORWARD_TRADES).name)[-limit:]
    trades: list[dict[str, Any]] = []
    for trade in rows:
        symbol = str(trade.get("symbol") or "")
        exit_raw = trade.get("closed_at") or trade.get("timestamp") or trade.get("ts")
        if not exit_raw:
            continue
        exit_ts = pd.Timestamp(str(exit_raw))
        if exit_ts.tzinfo is None:
            exit_ts = exit_ts.tz_localize("UTC")
        entry = match_entry_fill(fills, symbol=symbol, exit_ts=exit_ts)
        entry_ts = None
        if entry is not None:
            entry_ts = pd.Timestamp(str(entry.get("timestamp")))
            if entry_ts.tzinfo is None:
                entry_ts = entry_ts.tz_localize("UTC")
        entry_meta = _intent_metadata(entry or {})
        trades.append(
            {
                "symbol": symbol,
                "venue": trade.get("venue") or (entry or {}).get("venue"),
                "direction": position_side_of_close(str(trade.get("side"))),
                "entry_ts": entry_ts.isoformat() if entry_ts is not None else None,
                "entry_price": (entry or {}).get("avg_price"),
                "exit_ts": exit_ts.isoformat(),
                "exit_price": trade.get("price") or trade.get("avg_price"),
                "duration_minutes": (
                    round((exit_ts - entry_ts).total_seconds() / 60)
                    if entry_ts is not None
                    else None
                ),
                "net_pnl": trade.get("net_pnl"),
                "entry_reason": entry_meta.get("entry_reason"),
                "exit_reason": _closing_fill_reason(
                    fills, symbol=symbol, exit_ts=exit_ts
                )
                or "bracket_stop",
                "mode": str(trade.get("mode") or "paper"),
            }
        )
    trades.sort(key=lambda t: str(t["exit_ts"]), reverse=True)
    return trades


def _pnl_series(
    job_id: str, ticks: list[dict[str, Any]], *, store: JobStore
) -> dict[str, Any]:
    """Equity curve from the tick ledger: initial_capital + realized_pnl.

    Uses each tick's POST-tick ledger (the top-level `ledger` field), so a
    close's PnL lands on the bar it happened, and carries the tick's mode so
    the FE could shade paper vs live segments later.
    """
    job = store.load(job_id)
    initial_capital = float(job.execution_params.get("initial_capital") or 10_000)
    points = []
    for tick in ticks:
        timestamp = tick.get("bar_ts") or tick.get("ts")
        ledger = tick.get("ledger") or {}
        if not timestamp or "realized_pnl" not in ledger:
            continue
        realized = float(ledger.get("realized_pnl") or 0.0)
        # A freestyle tick marks its book to market and records the equity;
        # the harnessed driver records realized PnL only.
        equity = tick.get("equity")
        value = float(equity) if equity is not None else initial_capital + realized
        points.append(
            {
                "timestamp": str(timestamp),
                "value": value,
                "equity": value,
                "realized_pnl": realized,
                "unrealized_pnl": tick.get("unrealized_pnl"),
                "mode": tick.get("mode"),
            }
        )
    return {
        "name": "forward_equity",
        "kind": "equity_curve",
        "symbol": None,
        "points": points,
    }


def _intent_metadata(fill: Mapping[str, Any]) -> dict[str, Any]:
    """The harnessed driver nests intent metadata under the fill's `raw`;
    the freestyle runtime writes it at the top level."""
    raw = fill.get("raw") or {}
    meta = raw.get("intent_metadata") if isinstance(raw, dict) else None
    return dict(meta or fill.get("intent_metadata") or {})


def _lifecycle_script(job: Any) -> bool:
    return not job.execution_spec and str(job.execution_contract or "legacy") in {
        "freestyle_v1",
        "path_v1",
    }


def _split_mark_key(key: str) -> tuple[str | None, str]:
    venue, _, symbol = str(key).partition(":")
    return (venue, symbol) if symbol else (None, venue)


def _mark_series(ticks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One market_price series per `venue:symbol` mark a freestyle script read."""
    by_key: dict[str, list[dict[str, Any]]] = {}
    for tick in ticks:
        timestamp = tick.get("bar_ts") or tick.get("ts")
        if not timestamp:
            continue
        for key, value in (tick.get("marks") or {}).items():
            try:
                mark = float(value)
            except (TypeError, ValueError):
                continue
            by_key.setdefault(str(key), []).append(
                {"timestamp": str(timestamp), "value": mark, "close": mark}
            )
    series = []
    for key, points in by_key.items():
        venue, symbol = _split_mark_key(key)
        series.append(
            {
                "name": f"{symbol}_price",
                "kind": "market_price",
                "symbol": symbol,
                "venue": venue,
                "points": points,
            }
        )
    return series


def _read_series(ticks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Funding, token-value and yield reads as their own series."""
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for tick in ticks:
        timestamp = tick.get("bar_ts") or tick.get("ts")
        if not timestamp:
            continue
        for field in ("funding", "token_values", "yields"):
            for key, value in (tick.get(field) or {}).items():
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                buckets.setdefault((field, str(key)), []).append(
                    {"timestamp": str(timestamp), "value": number}
                )
    series = []
    for (field, key), points in buckets.items():
        if field == "funding":
            venue, symbol = _split_mark_key(key)
            series.append(
                {
                    "name": f"{symbol}_funding",
                    "kind": "funding_rate",
                    "symbol": symbol,
                    "venue": venue,
                    "key": key,
                    "points": points,
                }
            )
        elif field == "token_values":
            series.append(
                {
                    "name": f"token:{key}",
                    "kind": "token_value",
                    "symbol": None,
                    "venue": None,
                    "key": key,
                    "points": points,
                }
            )
        else:
            series.append(
                {
                    "name": f"yield:{key}",
                    "kind": "yield_rate",
                    "symbol": None,
                    "venue": None,
                    "key": key,
                    "points": points,
                }
            )
    return series


def feature_series(
    store: JobStore,
    job_id: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    """A harnessed job's declared feature feeds (state/features.jsonl) as
    `feature` series, one per feature (and per symbol when rows are keyed)."""
    import pandas as pd

    from wayfinder_paths.jobs.execution.features import (
        load_feature_rows,
        parse_feature_specs,
    )
    from wayfinder_paths.jobs.execution.primitives import ExecutionSpec

    job = store.load(job_id)
    if not job.execution_spec:
        return []
    specs = parse_feature_specs(ExecutionSpec.from_dict(dict(job.execution_spec)))
    if not specs:
        return []
    frames = load_feature_rows([store.job_dir(job_id)], specs)
    series: list[dict[str, Any]] = []
    for item in specs:
        frame = frames.get(item.name)
        if frame is None or frame.empty:
            continue
        if start is not None:
            frame = frame[frame["timestamp"] >= pd.Timestamp(start)]
        if end is not None:
            frame = frame[frame["timestamp"] <= pd.Timestamp(end)]
        symbols = (
            [s for s in frame["symbol"].unique() if isinstance(s, str) and s]
            if "symbol" in frame.columns
            else []
        )
        groups: list[tuple[str | None, Any]] = (
            [(symbol, frame[frame["symbol"] == symbol]) for symbol in symbols]
            if symbols
            else [(None, frame)]
        )
        for symbol, group in groups:
            points = [
                {"timestamp": pd.Timestamp(ts).isoformat(), "value": float(value)}
                for ts, value in zip(group["timestamp"], group["value"], strict=True)
                if pd.notna(value)
            ]
            series.append(
                {
                    "name": f"feature:{item.name}" + (f":{symbol}" if symbol else ""),
                    "kind": "feature",
                    "symbol": symbol,
                    "venue": None,
                    "key": item.name,
                    "points": points,
                }
            )
    return series


def _fetch_price_series(
    job_id: str, ticks: list[dict[str, Any]], *, store: JobStore
) -> list[dict[str, Any]]:
    """Price series covering the forward window. A harnessed job fetches
    candles through the venue feed its execution spec names; a freestyle or
    Path script has no spec, so its series are the marks it read (candles
    for hyperliquid symbols when the feed answers)."""
    job = store.load(job_id)
    if _lifecycle_script(job):
        return _freestyle_price_series(job, ticks, store=store)
    return _fetch_spec_price_series(job_id, ticks, store=store)


def _freestyle_price_series(
    job: Any, ticks: list[dict[str, Any]], *, store: JobStore
) -> list[dict[str, Any]]:
    series = _mark_series(ticks)
    for venue in _HISTORY_VENUES:
        symbols = [
            str(entry["symbol"]) for entry in series if entry.get("venue") == venue
        ]
        if not symbols:
            continue
        try:
            bars = _fetch_hyperliquid_bars(
                job, symbols, ticks, store=store, venue=venue
            )
        except Exception:  # noqa: BLE001 — marks are the fallback chart
            bars = {}
        for entry in series:
            if entry.get("venue") != venue:
                continue
            points = bars.get(str(entry["symbol"]))
            if points:
                entry["points"] = points
    return series


# Venues whose feeds serve completed candles, so a freestyle mark series can
# be upgraded to real OHLC bars.
_HISTORY_VENUES = ("hyperliquid", "onchain", "hyperliquid_spot")


def _fetch_hyperliquid_bars(
    job: Any,
    symbols: list[str],
    ticks: list[dict[str, Any]],
    *,
    store: JobStore,
    venue: str = "hyperliquid",
) -> dict[str, list[dict[str, Any]]]:
    import pandas as pd

    from wayfinder_paths.jobs.execution.primitives import (
        CompletedBarsView,
        bar_interval_seconds,
    )
    from wayfinder_paths.jobs.execution.venues import build_adapter

    validation = (
        store.read_json(job.id, "reports/validation/latest.json", default={}) or {}
    )
    interval = str(
        ((validation.get("freestyle") or {}).get("spec") or {}).get("quote_interval")
        or "5m"
    )
    interval_seconds = bar_interval_seconds(interval) or 300
    now = pd.Timestamp.now(tz="UTC")
    first_ts = _parse_ts(
        str(ticks[0].get("bar_ts") or ticks[0].get("ts")) if ticks else None
    )
    if first_ts is not None:
        window_bars = math.ceil(
            (now - pd.Timestamp(first_ts)).total_seconds() / interval_seconds
        )
    else:
        window_bars = _MAX_PRICE_BARS
    lookback_bars = min(max(window_bars + _WARMUP_BARS, _WARMUP_BARS), _MAX_PRICE_BARS)

    async def _fetch() -> CompletedBarsView:
        adapter = build_adapter(
            venue, mode="paper", params=dict(job.execution_params or {})
        )
        return await adapter.feed.get_completed_bars(
            symbols, interval, lookback_bars=lookback_bars, as_of=now
        )

    frame = asyncio.run(_fetch()).to_frame()
    out: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        symbol_frame = frame[frame["symbol"] == symbol]
        out[symbol] = [
            {
                "timestamp": row.timestamp.isoformat(),
                "value": float(row.close),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume)
                if "volume" in symbol_frame.columns and pd.notna(row.volume)
                else None,
            }
            for row in symbol_frame.itertuples()
        ]
    return out


def _fetch_spec_price_series(
    job_id: str, ticks: list[dict[str, Any]], *, store: JobStore
) -> list[dict[str, Any]]:
    """OHLC market_price series covering the forward window, fetched through
    the same venue feed the live driver uses (imported lazily — the execution
    stack pulls pandas et al., which a markers-only caller never needs).

    Bars persist in price_cache.json between builds: a build inside the bar
    the cache already ends on makes no venue call, a later one fetches only
    the missing bars plus the warmup, and the cache is replaced wholesale
    when it is unreadable or its interval or symbol set no longer match."""
    import pandas as pd

    from wayfinder_paths.jobs.execution.primitives import (
        ExecutionSpec,
        bar_interval_seconds,
    )
    from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
    from wayfinder_paths.jobs.execution.venues import build_adapter

    job = store.load(job_id)
    root = store.job_dir(job_id)
    spec_data, _ = resolve_execution_spec(root, job.to_dict())
    if not spec_data:
        raise RuntimeError("execution_spec missing")
    spec = ExecutionSpec.from_dict(spec_data)
    params = dict(job.execution_params)
    bar_interval = spec.data_contract.get("bar_interval")
    interval_seconds = bar_interval_seconds(bar_interval)
    if not interval_seconds:
        raise RuntimeError("execution_spec.data_contract.bar_interval missing")
    symbols = [
        str(symbol)
        for symbol in (params.get("symbols") or spec.data_contract.get("symbols") or [])
    ]
    if not symbols:
        raise RuntimeError("no symbols configured")

    now = pd.Timestamp(_utc_now())
    first_ts = _parse_ts(
        str(ticks[0].get("bar_ts") or ticks[0].get("ts")) if ticks else None
    )
    if first_ts is not None:
        window_bars = math.ceil(
            (now - pd.Timestamp(first_ts)).total_seconds() / interval_seconds
        )
    else:
        window_bars = _MAX_PRICE_BARS
    lookback_bars = min(max(window_bars + _WARMUP_BARS, _WARMUP_BARS), _MAX_PRICE_BARS)

    interval_ms = interval_seconds * 1000
    # Open of the latest bar that has already closed: the last bar any feed
    # can serve right now, so a cache ending there is complete.
    latest_open_ms = (
        (now.value // 1_000_000 - interval_ms) // interval_ms
    ) * interval_ms
    cache_path = root / "results" / "forward" / _PRICE_CACHE_NAME
    bars_by_symbol, venue_by_symbol = _read_price_cache(
        cache_path, interval=str(bar_interval), symbols=symbols
    )
    cached_open_ms = min(
        (max(bars) for bars in bars_by_symbol.values() if bars), default=None
    )
    if cached_open_ms != latest_open_ms:
        if cached_open_ms is None:
            fetch_bars = lookback_bars
        else:
            missing_bars = max(0, (latest_open_ms - cached_open_ms) // interval_ms)
            fetch_bars = min(missing_bars + _WARMUP_BARS, _MAX_PRICE_BARS)

        async def _fetch() -> list[Mapping[str, Any]]:
            rows: list[Mapping[str, Any]] = []
            # mode="paper" builds the read-only market-data side; no signing/keys.
            for venue in spec.venues or ["hyperliquid"]:
                adapter = build_adapter(venue, mode="paper", spec=spec, params=params)
                view = await adapter.feed.get_completed_bars(
                    symbols, str(bar_interval), lookback_bars=fetch_bars, as_of=now
                )
                venue_rows = view.to_rows()
                for row in venue_rows:
                    venue_by_symbol.setdefault(str(row["symbol"]), str(venue))
                rows.extend(venue_rows)
            return rows

        # Feed rows are close-labelled; the cache keys on the bar open. A
        # freshly fetched bar replaces the cached one at the same open.
        for row in asyncio.run(_fetch()):
            bars = bars_by_symbol.get(str(row["symbol"]))
            if bars is None:
                continue
            close_ms = int(pd.Timestamp(row["timestamp"]).value // 1_000_000)
            volume = row.get("volume")
            bars[close_ms - interval_ms] = [
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                None if volume is None or pd.isna(volume) else float(volume),
            ]
        for bars in bars_by_symbol.values():
            for stale_open_ms in sorted(bars)[:-_MAX_PRICE_BARS]:
                del bars[stale_open_ms]
        _write_price_cache(
            cache_path,
            interval=str(bar_interval),
            bars_by_symbol=bars_by_symbol,
            venue_by_symbol=venue_by_symbol,
        )
    if not any(bars_by_symbol.values()):
        raise RuntimeError("no completed bars returned by any venue feed")

    series = []
    for symbol in symbols:
        points = [
            {
                "timestamp": pd.Timestamp(
                    open_ms + interval_ms, unit="ms", tz="UTC"
                ).isoformat(),
                "value": close,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
            for open_ms, (open_, high, low, close, volume) in sorted(
                bars_by_symbol[symbol].items()
            )
        ]
        series.append(
            {
                "name": f"{symbol}_price",
                "kind": "market_price",
                "symbol": symbol,
                # The venue whose feed produced these bars — lets the UI swap
                # the static payload for that venue's live streaming chart.
                "venue": venue_by_symbol.get(symbol),
                "points": points,
            }
        )
    return series


def _read_price_cache(
    path: Path, *, interval: str, symbols: list[str]
) -> tuple[dict[str, _Bars], dict[str, str]]:
    """Cached bars per symbol and the venue that produced them, when the file
    matches this interval and symbol set. Anything unreadable or mismatched
    is a miss — one full refetch, never a failed chart."""
    empty: tuple[dict[str, _Bars], dict[str, str]] = (
        {symbol: {} for symbol in symbols},
        {},
    )
    if not path.exists():
        return empty
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
        if cache["interval"] != interval or set(cache["symbols"]) != set(symbols):
            return empty
        bars_by_symbol = {
            str(symbol): {
                int(open_ms): [open_, high, low, close, volume]
                for open_ms, open_, high, low, close, volume in entry["bars"]
            }
            for symbol, entry in cache["symbols"].items()
        }
        venue_by_symbol = {
            str(symbol): str(entry["venue"])
            for symbol, entry in cache["symbols"].items()
            if entry["venue"]
        }
    except Exception as exc:  # noqa: BLE001 — a corrupt cache costs one refetch
        logger.debug("forward price cache unreadable, refetching: {} ({})", path, exc)
        return empty
    return bars_by_symbol, venue_by_symbol


def _write_price_cache(
    path: Path,
    *,
    interval: str,
    bars_by_symbol: dict[str, _Bars],
    venue_by_symbol: dict[str, str],
) -> None:
    payload = {
        "interval": interval,
        "symbols": {
            symbol: {
                "venue": venue_by_symbol.get(symbol),
                "bars": [[open_ms, *bars[open_ms]] for open_ms in sorted(bars)],
            }
            for symbol, bars in bars_by_symbol.items()
        },
    }
    # Compact on purpose: an indented dump of 2000 bars x 4 symbols is 60k lines.
    atomic_write_text(path, json.dumps(payload, separators=(",", ":")))


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
