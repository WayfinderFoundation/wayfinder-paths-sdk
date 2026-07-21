from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.backtest_artifacts import VIEW_KINDS, _in_range, _parse_ts
from wayfinder_paths.jobs.forward import default_forward_summary
from wayfinder_paths.jobs.models import (
    DEFAULT_FORWARD_FILLS,
    DEFAULT_FORWARD_SUMMARY,
    DEFAULT_FORWARD_TICKS,
    DEFAULT_FORWARD_TRADES,
)
from wayfinder_paths.jobs.store import JobStore

# Chart context beyond the first forward tick, and a hard fetch cap — the
# forward window grows unboundedly, the chart payload must not.
_WARMUP_BARS = 24
_MAX_PRICE_BARS = 2000


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
    for row in _read_jsonl(forward_dir / Path(DEFAULT_FORWARD_TRADES).name):
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
    ticks = _tail_jsonl(forward_dir / Path(DEFAULT_FORWARD_TICKS).name, 1)
    if not ticks:
        return None
    tick = ticks[-1]
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
) -> dict[str, Any]:
    """Bounded forward (paper/live) visualization payload for the jobs UI.

    Mirrors `load_backtest_view`'s response shape so the whole chart pipeline
    (backend proxy + FE renderer) is reused, but is built on demand from the
    forward artifacts instead of a pre-written visualization.json:
    - markers from fills.jsonl, each tagged with the MODE it executed under
    - a PnL curve from the tick ledger's realized_pnl progression
    - market_price OHLC series fetched through the same venue feed the driver
      uses (forward ticks don't persist bars); on fetch failure the payload
      degrades to markers + PnL with a `price_note` instead of failing.
    """
    store = store or JobStore()
    forward_dir = store.job_dir(job_id) / "results" / "forward"
    fills = _read_jsonl(forward_dir / Path(DEFAULT_FORWARD_FILLS).name)
    ticks = _read_jsonl(forward_dir / Path(DEFAULT_FORWARD_TICKS).name)
    if not fills and not ticks:
        return {"available": False}

    markers = _fill_markers(fills)
    series: list[dict[str, Any]] = [_pnl_series(job_id, ticks, store=store)]

    price_note: str | None = None
    last_closes: dict[str, float] = {}
    if include_prices:
        try:
            price_series = _fetch_price_series(job_id, ticks, store=store)
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
            "series": selected_series,
            "markers": selected_markers,
            "events": events,
        },
        "trades": _tail_jsonl(forward_dir / Path(DEFAULT_FORWARD_TRADES).name, 50),
    }


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
        raw = fill.get("raw") or {}
        meta = raw.get("intent_metadata") or {}
        reason = meta.get("entry_reason") or meta.get("exit_reason")
        markers.append(
            {
                "timestamp": str(timestamp),
                "symbol": str(fill.get("symbol") or ""),
                "side": fill.get("side"),
                "price": fill.get("avg_price"),
                "kind": kind,
                "mode": mode,
                "label": f"{mode} {kind}" + (f": {reason}" if reason else ""),
            }
        )
    markers.sort(key=lambda marker: str(marker["timestamp"]))
    return markers


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
        points.append(
            {
                "timestamp": str(timestamp),
                "value": initial_capital + realized,
                "equity": initial_capital + realized,
                "realized_pnl": realized,
                "mode": tick.get("mode"),
            }
        )
    return {
        "name": "forward_equity",
        "kind": "equity_curve",
        "symbol": None,
        "points": points,
    }


def _fetch_price_series(
    job_id: str, ticks: list[dict[str, Any]], *, store: JobStore
) -> list[dict[str, Any]]:
    """OHLC market_price series covering the forward window, fetched through
    the same venue feed the live driver uses (imported lazily — the execution
    stack pulls pandas et al., which a markers-only caller never needs)."""
    import pandas as pd

    from wayfinder_paths.jobs.execution.primitives import (
        CompletedBarsView,
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
        rows: list[Mapping[str, Any]] = []
        # mode="paper" builds the read-only market-data side; no signing/keys.
        for venue in spec.venues or ["hyperliquid"]:
            adapter = build_adapter(venue, mode="paper", spec=spec, params=params)
            view = await adapter.feed.get_completed_bars(
                symbols, str(bar_interval), lookback_bars=lookback_bars, as_of=now
            )
            rows.extend(view.to_rows())
        if not rows:
            raise RuntimeError("no completed bars returned by any venue feed")
        return CompletedBarsView.from_rows(rows)

    frame = asyncio.run(_fetch()).to_frame()
    series = []
    for symbol in symbols:
        symbol_frame = frame[frame["symbol"] == symbol]
        points = [
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
        series.append(
            {
                "name": f"{symbol}_price",
                "kind": "market_price",
                "symbol": symbol,
                "points": points,
            }
        )
    return series


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists() or limit <= 0:
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [json.loads(line) for line in lines[-limit:] if line.strip()]


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
