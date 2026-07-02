from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.engine import (
    EngineState,
    TickResult,
    flatten_positions,
    run_tick,
)
from wayfinder_paths.jobs.execution.features import (
    feature_staleness,
    load_feature_rows,
    merge_features,
    parse_feature_specs,
)
from wayfinder_paths.jobs.execution.job import _load_job_yaml
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionSpec,
    ExecutionTrace,
    StateSnapshot,
    bar_interval_seconds,
)
from wayfinder_paths.jobs.execution.risk import check_risk_halt
from wayfinder_paths.jobs.execution.simulator import _load_strategy
from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
from wayfinder_paths.jobs.execution.venues import VenueAdapter, build_adapter
from wayfinder_paths.jobs.forward import ForwardRecorder
from wayfinder_paths.jobs.halt import read_halt
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore

ENGINE_STATE_PATH = "state/engine_state.json"
SIZE_TOLERANCE = 1e-6


def run_scheduled_tick(job_dir: str | Path | None = None) -> dict[str, Any]:
    """Sync entrypoint invoked by the compiler wrapper each schedule fire.

    The runner spawns a fresh subprocess per tick, so everything durable is
    loaded from disk here and persisted before exit.
    """
    root = Path(job_dir or os.environ["WAYFINDER_JOB_DIR"])
    mode = os.environ.get("WAYFINDER_JOB_MODE") or "paper"
    store = None
    job = None
    try:
        store = JobStore()
        job = WayfinderJob.from_dict(_load_job_yaml(root))
        payload = asyncio.run(tick_job(job, root, mode, store=store))
    except Exception as exc:
        payload = {"ok": False, "error": str(exc)}
    # Event-driven agent wakes fire ONLY from the scheduled entrypoint —
    # never from tick_job itself, so preflight sandbox ticks and manual
    # `wayfinder job tick` runs cannot wake the advisor.
    if store is not None and job is not None:
        events = _tick_trigger_events(payload)
        if events:
            from wayfinder_paths.jobs.triggers import fire_triggers

            fire_triggers(store, job, events, source="scheduled_tick")
    print(json.dumps(payload, default=str))
    return payload


def _tick_trigger_events(payload: dict[str, Any]) -> list[str]:
    events: list[str] = []
    if payload.get("ok") is not True:
        events.append("script_failure")
    snapshot = payload.get("snapshot") or {}
    if snapshot.get("status") == "ambiguous":
        events.append("reconcile_mismatch")
    guard_kinds = {
        str(event.get("kind")) for event in payload.get("guard_events") or []
    }
    if guard_kinds & {"risk_halt", "manual_halt"}:
        events.append("risk_halt")
    return events


async def tick_job(
    job: WayfinderJob,
    root: Path,
    mode: str,
    *,
    store: JobStore | None = None,
    adapters: Mapping[str, VenueAdapter] | None = None,
    now: pd.Timestamp | None = None,
    recorder: ForwardRecorder | None = None,
    entrypoint: Path | None = None,
) -> dict[str, Any]:
    """One driver tick: load state -> fetch bars -> reconcile -> run_tick ->
    record + persist. `adapters` and `now` are injectable for preflight/tests."""
    job_data = job.to_dict()
    spec_data, _ = resolve_execution_spec(root, job_data)
    if not spec_data:
        raise FileNotFoundError(f"execution_spec missing for job {job.id}")
    spec = ExecutionSpec.from_dict(spec_data)
    params = dict(job.execution_params)
    bar_interval = spec.data_contract.get("bar_interval")
    if not bar_interval_seconds(bar_interval):
        raise ValueError(
            "execution_spec.data_contract.bar_interval is required to run the "
            "jobs_v1 driver"
        )
    symbols = [
        str(symbol)
        for symbol in (params.get("symbols") or spec.data_contract.get("symbols") or [])
    ]
    if not symbols:
        raise ValueError(
            "no symbols configured: set execution_params.symbols or "
            "execution_spec.data_contract.symbols"
        )

    if store is None:
        store = JobStore()
    if entrypoint is None:
        entrypoint = store.resolve_script_entrypoint(job.id, job_data)
    if entrypoint is None or not entrypoint.exists():
        raise FileNotFoundError(f"execution script not found for job {job.id}")
    strategy = _load_strategy(entrypoint, params)

    revision = str(
        os.environ.get("WAYFINDER_JOB_REVISION")
        or (job.versioning or {}).get("active_revision")
        or ""
    )
    state_path = root / ENGINE_STATE_PATH
    state_file_existed = state_path.exists()
    state = EngineState.load(state_path)
    state.revision = revision or state.revision

    if adapters is None:
        adapters = {
            venue: build_adapter(venue, mode=mode, spec=spec, params=params)
            for venue in (spec.venues or ["hyperliquid"])
        }
    brokers = {name: adapter.broker for name, adapter in adapters.items()}
    now = now if now is not None else pd.Timestamp.now(tz="UTC")

    lookback_bars = int(params.get("lookback_bars") or 200)
    rows: list[dict[str, Any]] = []
    for adapter in adapters.values():
        view = await adapter.feed.get_completed_bars(
            symbols, str(bar_interval), lookback_bars=lookback_bars, as_of=now
        )
        rows.extend(view.to_rows())
    if not rows:
        raise RuntimeError("no completed bars returned by any venue feed")
    view = CompletedBarsView.from_rows(rows)

    events = []
    for adapter in adapters.values():
        events.extend(await adapter.feed.get_events(symbols))

    snapshot, reconcile_notes = await _reconcile(
        mode=mode,
        state=state,
        brokers=brokers,
        symbols=symbols,
        state_file_existed=state_file_existed,
    )

    # Account-level circuit breakers (workspace/risk_limits.json, optional).
    # Downgrades only a valid snapshot: an already-ambiguous state is a
    # stronger signal and must not be masked by a risk halt.
    halt_reason, risk_snapshot = check_risk_halt(
        root, state=state, view=view, params=params, now=now
    )
    risk_notes: list[dict[str, Any]] = []
    if halt_reason:
        risk_notes.append(
            {"kind": "risk_halt", "reason": halt_reason, "snapshot": risk_snapshot}
        )
        if snapshot.status == "valid":
            snapshot = StateSnapshot(status="risk_halt", reason=halt_reason)

    # Manual kill switch: outranks every other status (including ambiguous)
    # — reduce-only regardless, and cancel queued OPENs before they can
    # settle at the next bar open inside run_tick.
    manual_halt = read_halt(root)
    if manual_halt is not None:
        halt_note = f"manual halt: {manual_halt.get('reason') or 'unspecified'}"
        risk_notes.append({"kind": "manual_halt", "reason": halt_note})
        snapshot = StateSnapshot(status="risk_halt", reason=halt_note)
        kept_intents = []
        for intent in state.pending_intents:
            if intent.reduce_only:
                kept_intents.append(intent)
                continue
            risk_notes.append(
                {
                    "kind": "pending_intent_canceled_by_halt",
                    "intent": intent.to_dict(),
                }
            )
        state.pending_intents = kept_intents

    for broker in brokers.values():
        if hasattr(broker, "snapshot"):
            broker.snapshot = snapshot

    # Exogenous features (execution_spec.data_contract.features): the DRIVER
    # owns this I/O so decide() stays pure. The merged columns land in the
    # view (and therefore in view_hash + recorded rows), giving the backtest
    # loader identical as-of semantics and the reconciler exact replays.
    feature_specs = parse_feature_specs(spec)
    feature_guards: list[dict[str, Any]] = []
    feature_skip = False
    if feature_specs:
        feature_frames = load_feature_rows([root], feature_specs)
        feature_guards, feature_skip = feature_staleness(
            feature_specs, feature_frames, now
        )
        if not feature_skip:
            view = merge_features(view, feature_frames, feature_specs)

    # Captured before run_tick mutates state: the reconciler replays each tick
    # from exactly this state.
    engine_state_pre = state.to_dict()

    if feature_skip:
        # Mirrors bar staleness with policy "skip": never decide against
        # stale exogenous data when the spec says it must be fresh.
        tick = TickResult(
            skipped=True,
            skip_reason="stale_feature",
            bar_timestamp=(
                view.timestamps[-1].isoformat() if view.timestamps else None
            ),
            snapshot=snapshot,
        )
    else:
        tick = await run_tick(
            strategy,
            view=view,
            brokers=brokers,
            state=state,
            spec=spec,
            params=params,
            timestamp=now,
            snapshot=snapshot,
            capacity=None,
            events=events,
            auto_limits=dict(job.agent_loop.auto_limits or {}) or None,
            client_order_prefix=job.id,
        )
    tick.guard_events.extend(reconcile_notes)
    tick.guard_events.extend(risk_notes)
    tick.guard_events.extend(feature_guards)

    if (
        manual_halt is not None
        and manual_halt.get("flatten")
        and state.ledger.positions
    ):
        # Market-close everything at the latest completed close. Runs even on
        # skipped ticks (no_new_bar): a flatten request must not wait for a
        # fresh bar. Fills land in tick.fills/trade_rows for the recorder.
        fills_before_flatten = len(tick.fills)
        await flatten_positions(
            brokers=brokers,
            state=state,
            view=view,
            timestamp=tick.bar_timestamp or now.isoformat(),
            trace=ExecutionTrace(execution_spec=spec.to_dict()),
            result=tick,
        )
        flatten_fills = [
            fill.to_dict()
            for fill in tick.fills[fills_before_flatten:]
            if fill.successful
        ]
        if flatten_fills:
            store.append_journal(
                job.id,
                {
                    "type": "halt_flattened",
                    "mode": mode,
                    "fills": [
                        {
                            "symbol": row.get("symbol"),
                            "side": row.get("side"),
                            "filled_size": row.get("filled_size"),
                            "avg_price": row.get("avg_price"),
                        }
                        for row in flatten_fills
                    ],
                },
            )

    state.save(state_path)
    _record(
        recorder
        or ForwardRecorder(
            job_id=job.id, job_dir=root, mode=mode, revision=revision or None
        ),
        tick,
        view=view,
        params=params,
        now=now,
        engine_state_pre=engine_state_pre,
    )
    if snapshot.status == "ambiguous":
        store.append_journal(
            job.id,
            {
                "type": "reconcile_mismatch",
                "reasons": [note["reason"] for note in reconcile_notes],
                "mode": mode,
            },
        )
    if halt_reason:
        store.append_journal(
            job.id,
            {"type": "risk_halt", "reason": halt_reason, "mode": mode},
        )
    return {
        "ok": True,
        "job_id": job.id,
        "mode": mode,
        "skipped": tick.skipped,
        "skip_reason": tick.skip_reason,
        "bar_timestamp": tick.bar_timestamp,
        "snapshot": snapshot.to_dict(),
        "intents": [intent.to_dict() for intent in tick.intents],
        "fills": [fill.to_dict() for fill in tick.fills],
        "guard_events": tick.guard_events,
        "positions": tick.ledger_snapshot.get("positions", {}),
    }


async def _reconcile(
    *,
    mode: str,
    state: EngineState,
    brokers: Mapping[str, Any],
    symbols: list[str],
    state_file_existed: bool,
) -> tuple[StateSnapshot, list[dict[str, Any]]]:
    """Compare the recorded ledger against venue ground truth.

    Venue state wins on existence/size/side; recorded fills win on avg_price.
    Any divergence -> ambiguous snapshot and reduce-only ticks (never clear
    local state on a mismatch — an ambiguous fetch clearing state is exactly
    the failure mode that stranded live positions before)."""
    if mode != "live":
        return StateSnapshot(status="valid"), []
    notes: list[dict[str, Any]] = []
    venue_positions: dict[str, Any] = {}
    for name, broker in brokers.items():
        try:
            venue_state = await broker.fetch_state(symbols)
        except Exception as exc:
            return (
                StateSnapshot(
                    status="ambiguous", reason=f"venue state fetch failed: {exc}"
                ),
                [
                    {
                        "kind": "reconcile_fetch_failed",
                        "venue": name,
                        "reason": str(exc),
                    }
                ],
            )
        venue_positions.update(venue_state.positions)

    if not state_file_existed and venue_positions:
        for symbol, record in venue_positions.items():
            record.metadata["adopted_from_venue"] = True
            state.ledger.positions[symbol] = record
            notes.append(
                {
                    "kind": "adopted_from_venue",
                    "symbol": symbol,
                    "reason": "no engine state on disk; adopted venue position",
                }
            )
        return StateSnapshot(status="valid", reason="adopted_from_venue"), notes

    reasons: list[str] = []
    for symbol, venue_record in venue_positions.items():
        local = state.ledger.positions.get(symbol)
        if local is None:
            reasons.append(f"venue holds untracked position in {symbol}")
            continue
        if local.side != venue_record.side:
            reasons.append(
                f"{symbol} side mismatch: ledger={local.side} venue={venue_record.side}"
            )
        elif abs(local.size - venue_record.size) > SIZE_TOLERANCE * max(
            1.0, abs(venue_record.size)
        ):
            reasons.append(
                f"{symbol} size mismatch: ledger={local.size} venue={venue_record.size}"
            )
    for symbol in state.ledger.positions:
        if symbol not in venue_positions:
            reasons.append(f"ledger position {symbol} missing on venue")
    if reasons:
        notes.extend(
            {"kind": "reconcile_mismatch", "reason": reason} for reason in reasons
        )
        return StateSnapshot(status="ambiguous", reason="; ".join(reasons)), notes
    return StateSnapshot(status="valid"), notes


def _record(
    recorder: ForwardRecorder,
    tick: TickResult,
    *,
    view: CompletedBarsView,
    params: Mapping[str, Any],
    now: pd.Timestamp,
    engine_state_pre: Mapping[str, Any] | None = None,
) -> None:
    intents = [intent.to_dict() for intent in tick.intents]
    fills = [fill.to_dict() for fill in tick.fills]
    timestamps = view.timestamps
    recorder.record_tick(
        ts=now.isoformat(),
        bar_ts=tick.bar_timestamp,
        skipped=tick.skipped,
        skip_reason=tick.skip_reason,
        view_hash=view_hash(view),
        view_window={
            "first_ts": timestamps[0].isoformat() if timestamps else None,
            "last_ts": timestamps[-1].isoformat() if timestamps else None,
            "rows": len(view.to_frame()),
        },
        snapshot=tick.snapshot.to_dict(),
        intents=intents,
        fills=fills,
        guard_events=tick.guard_events,
        params_hash=_hash_payload(dict(params)),
        ledger=tick.ledger_snapshot,
        engine_state_pre=dict(engine_state_pre or {}),
    )
    decision = tick.intents[0].action if tick.intents else "hold"
    recorder.record_run(
        status="skipped" if tick.skipped else "ok",
        decision=decision,
        reason=tick.skip_reason,
        metrics={"fill_count": len(fills), "guard_event_count": len(tick.guard_events)},
    )
    for intent in intents:
        recorder.record_order(intent)
    for row in fills:
        recorder.record_fill(row)
    # trade_rows are FillEvent.to_dict() + realized_pnl_delta: fixed shape.
    for row in tick.trade_rows:
        if row["reduce_only"]:
            recorder.record_trade_close(
                symbol=row["symbol"],
                side=row["side"],
                size=row["filled_size"],
                price=row["avg_price"],
                net_pnl=row["realized_pnl_delta"],
                closed_at=row["timestamp"],
            )


def view_hash(view: CompletedBarsView) -> str:
    return _hash_payload(view.to_rows())


def _hash_payload(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]
