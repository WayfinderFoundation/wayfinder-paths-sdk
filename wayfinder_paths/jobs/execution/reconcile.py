from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.driver import view_hash
from wayfinder_paths.jobs.execution.engine import EngineState, run_tick
from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
from wayfinder_paths.jobs.execution.paper import PaperBroker
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionSpec,
    StateSnapshot,
)
from wayfinder_paths.jobs.execution.simulator import (
    REDUCE_ONLY_ACTIONS,
    _load_strategy,
)
from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.triggers import fire_triggers

INTENT_MATCH_YELLOW = 0.98
INTENT_MATCH_RED = 0.90
INTENT_FIELDS = (
    "action",
    "venue",
    "symbol",
    "side",
    "size",
    "notional",
    "reduce_only",
    "bracket",
)


def reconcile_job(
    job_id: str,
    *,
    store: JobStore | None = None,
    history: CompletedBarsView | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Replay recorded live/paper ticks through the SAME engine and diff the
    decisions — the ongoing answer to 'is live still doing what the backtest
    said'. Each tick row carries its pre-tick engine state and view window, so
    the replay is exact: same state, same bars, same decide().

    `history` supplies the bar history to reconstruct views from (tests inject
    it; callers can pass a freshly fetched view to detect upstream data drift
    via the recorded view_hash).
    """
    store = store or JobStore()
    root = store.job_dir(job_id)
    job_data = _load_job_yaml(root)
    spec_data, _ = resolve_execution_spec(root, job_data)
    spec = ExecutionSpec.from_dict(spec_data)
    params = dict(job_data.get("execution_params") or {})
    entrypoint = store.resolve_script_entrypoint(job_id, job_data)
    if entrypoint is None or not entrypoint.exists():
        raise FileNotFoundError(f"execution script not found for job {job_id}")

    rows: list[dict[str, Any]] = []
    ticks_path = root / "results" / "forward" / "ticks.jsonl"
    if ticks_path.exists():
        lines = ticks_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-limit:]:
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            match parsed:
                case dict():
                    rows.append(parsed)
    if history is None:
        try:
            history = _load_dataset(root, spec, job_data).bars
        except FileNotFoundError:
            history = None

    outcome = asyncio.run(
        _replay_rows(
            rows,
            strategy_loader=lambda: _load_strategy(entrypoint, params),
            history=history,
            spec=spec,
            params=params,
        )
    )
    report = _build_report(job_id, rows, outcome)
    store.write_json(job_id, "reports/reconcile/latest.json", report)
    match_rate = report["intent_match_rate"]
    health = None
    if match_rate is not None:
        if match_rate < INTENT_MATCH_RED or report["missing_exit_intents"]:
            health = "red"
        elif match_rate < INTENT_MATCH_YELLOW:
            health = "yellow"
    summary = {
        "intent_match_rate": match_rate,
        "ticks_compared": report["ticks_compared"],
        "data_drift_ticks": report["data_drift_ticks"],
        "missing_exit_intents": len(report["missing_exit_intents"]),
    }
    if health:
        store.refresh_scorecard(job_id, {"health": health, "reconcile": summary})
        store.append_journal(
            job_id,
            {"type": "drift_warning", "source": "reconcile", **summary},
        )
        fire_triggers(
            store,
            WayfinderJob.from_dict(job_data),
            ["drift_warning"] + (["health_red"] if health == "red" else []),
            source="reconcile",
        )
    else:
        store.refresh_scorecard(job_id, {"reconcile": summary})
    return report


async def _replay_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    strategy_loader: Any,
    history: CompletedBarsView | None,
    spec: ExecutionSpec,
    params: dict[str, Any],
) -> dict[str, Any]:
    matched = 0
    compared = 0
    data_drift = 0
    missing: list[dict[str, Any]] = []
    extra: list[dict[str, Any]] = []
    slippage_bps: list[float] = []
    for row in rows:
        if row.get("skipped"):
            continue
        window = row.get("view_window") or {}
        first = window.get("first_ts")
        last = window.get("last_ts")
        view = None
        if history is not None and first and last:
            frame = history.to_frame()
            sliced = frame[
                (frame["timestamp"] >= pd.Timestamp(first))
                & (frame["timestamp"] <= pd.Timestamp(last))
            ]
            if not sliced.empty:
                view = CompletedBarsView(sliced)
        if view is None:
            data_drift += 1
            continue
        if row.get("view_hash") and view_hash(view) != row["view_hash"]:
            data_drift += 1
            continue
        state = EngineState.from_dict(row.get("engine_state_pre") or {})
        snapshot_data = row.get("snapshot") or {}
        strategy = strategy_loader()
        tick = await run_tick(
            strategy,
            view=view,
            brokers={"*": PaperBroker()},
            state=state,
            spec=spec,
            params=params,
            timestamp=pd.Timestamp(row.get("bar_ts") or view.timestamps[-1]),
            snapshot=StateSnapshot(
                status=str(snapshot_data.get("status") or "valid"),
                reason=snapshot_data.get("reason"),
            ),
        )
        recorded = [
            {field: item.get(field) for field in INTENT_FIELDS}
            for item in row.get("intents") or []
        ]
        replayed = [
            {field: data.get(field) for field in INTENT_FIELDS}
            for data in (intent.to_dict() for intent in tick.intents)
        ]
        compared += 1
        if recorded == replayed:
            matched += 1
        else:
            recorded_set = {json.dumps(k, sort_keys=True) for k in recorded}
            replayed_set = {json.dumps(k, sort_keys=True) for k in replayed}
            for key in recorded_set - replayed_set:
                missing.append({"bar_ts": row.get("bar_ts"), "intent": json.loads(key)})
            for key in replayed_set - recorded_set:
                extra.append({"bar_ts": row.get("bar_ts"), "intent": json.loads(key)})
        slippage_bps.extend(_fill_slippage_bps(row, view))
    return {
        "compared": compared,
        "matched": matched,
        "data_drift": data_drift,
        "missing": missing,
        "extra": extra,
        "slippage_bps": slippage_bps,
    }


def _build_report(
    job_id: str, rows: Sequence[Mapping[str, Any]], outcome: dict[str, Any]
) -> dict[str, Any]:
    compared = outcome["compared"]
    match_rate = (outcome["matched"] / compared) if compared else None
    slippage = sorted(outcome["slippage_bps"])
    missing_exits = [
        item
        for item in outcome["missing"]
        if str(item["intent"]["action"] or "").upper() in REDUCE_ONLY_ACTIONS
    ]
    return {
        "job_id": job_id,
        "generated_at": utc_now_iso(),
        "ticks_total": len(rows),
        "ticks_compared": compared,
        "intent_match_rate": match_rate,
        "missing_intents": outcome["missing"],
        "extra_intents": outcome["extra"],
        "missing_exit_intents": missing_exits,
        "data_drift_ticks": outcome["data_drift"],
        "fill_slippage_bps_p50": (slippage[len(slippage) // 2] if slippage else None),
        "fill_slippage_samples": len(slippage),
    }


def _fill_slippage_bps(row: Mapping[str, Any], view: CompletedBarsView) -> list[float]:
    """Recorded fill price vs the bar open the model assumes (next_bar_open):
    the live-vs-model execution cost, in bps."""
    samples: list[float] = []
    for fill in row.get("fills") or []:
        if fill.get("status") != "filled" or not fill.get("avg_price"):
            continue
        symbol = fill.get("symbol")
        # Parses recorded external data: junk bar_ts -> TypeError from
        # pd.Timestamp; absent bar -> ValueError from row_at. Not cast guards.
        try:
            bar = view.row_at(pd.Timestamp(row.get("bar_ts")), symbol=symbol)
        except (ValueError, TypeError):
            continue
        reference = bar.open
        if not reference:
            continue
        samples.append(abs(float(fill["avg_price"]) - reference) / reference * 10_000)
    return samples
