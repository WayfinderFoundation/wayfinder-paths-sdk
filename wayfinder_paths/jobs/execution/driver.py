from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from wayfinder_paths.jobs.execution.engine import (
    EngineState,
    TickResult,
    flatten_positions,
    run_tick,
)
from wayfinder_paths.jobs.execution.features import (
    apply_precompute,
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
from wayfinder_paths.jobs.execution.protection import monitor_native_protection
from wayfinder_paths.jobs.execution.risk import RISK_STATE_PATH, check_risk_halt
from wayfinder_paths.jobs.execution.simulator import _load_strategy
from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
from wayfinder_paths.jobs.execution.venues import (
    VenueAdapter,
    VenueState,
    build_adapter,
)
from wayfinder_paths.jobs.forward import ForwardRecorder
from wayfinder_paths.jobs.gating import clamp_leverage, governance_hard_constraints
from wayfinder_paths.jobs.halt import read_halt, request_halt
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.triggers import fire_triggers

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
    divergence: dict[str, Any] | None = None
    try:
        store = JobStore()
        job = WayfinderJob.from_dict(_load_job_yaml(root))
        # Fail-safe: never trade LIVE while the approved config (job.yaml) says
        # paper. The runner env WAYFINDER_JOB_MODE can be flipped to live
        # without updating job.yaml or recompiling — that split-brain once left
        # a job live-trading real funds under a paper gate. Downgrade to paper
        # and surface the divergence loudly so it can't happen silently.
        declared_mode = str(job.script_loop.mode or "paper")
        if mode == "live" and declared_mode != "live":
            divergence = {
                "kind": "mode_divergence",
                "runner_mode": "live",
                "declared_mode": declared_mode,
                "action": "downgraded_to_paper",
            }
            mode = "paper"
        payload = asyncio.run(tick_job(job, root, mode, store=store))
    except Exception as exc:
        payload = {"ok": False, "error": str(exc)}
    # Deterministic incumbent/regime health runs after recording this tick so
    # a just-closed loss participates immediately. It is raise-free here: an
    # observability failure must never turn a successful trading tick into a
    # failed one. Owner-governed pause/flatten responses latch for next tick.
    if store is not None and job is not None and payload.get("ok") is True:
        try:
            from wayfinder_paths.jobs.regime_health import (
                compact_regime_health,
                regime_health_job,
            )

            health = regime_health_job(job.id, store=store)
            compact_health = compact_regime_health(health)
            payload["regime_health"] = compact_health
            try:
                from wayfinder_paths.jobs.remediation import (
                    sync_remediation_with_health,
                )

                remediation_event = sync_remediation_with_health(store, job.id, health)
                if remediation_event:
                    compact_health["remediation_event"] = remediation_event
            except Exception as exc:  # noqa: BLE001
                store.append_journal(
                    job.id,
                    {"type": "regime_remediation_failed", "error": str(exc)[:300]},
                )
        except Exception as exc:  # noqa: BLE001
            store.append_journal(
                job.id,
                {"type": "regime_health_failed", "error": str(exc)[:300]},
            )
    if divergence is not None:
        payload.setdefault("guard_events", []).append(divergence)
    # Event-driven agent wakes fire ONLY from the scheduled entrypoint —
    # never from tick_job itself, so preflight sandbox ticks and manual
    # `wayfinder job tick` runs cannot wake the advisor.
    if store is not None and job is not None:
        events = _tick_trigger_events(payload)
        if events:
            fire_triggers(store, job, events, source="scheduled_tick")
    print(json.dumps(payload, default=str))
    return payload


def _tick_trigger_events(payload: dict[str, Any]) -> list[str]:
    events: list[str] = []
    if payload.get("ok") is not True and not _is_retryable_data_failure(payload):
        events.append("script_failure")
    snapshot = payload.get("snapshot") or {}
    if snapshot.get("status") == "ambiguous":
        events.append("reconcile_mismatch")
    guard_kinds = {
        str(event.get("kind")) for event in payload.get("guard_events") or []
    }
    if guard_kinds & {
        "risk_halt",
        "manual_halt",
        "native_protection_failed",
        "native_protection_breach",
        "native_protection_cancel_unconfirmed",
    }:
        events.append("risk_halt")
    if "mode_divergence" in guard_kinds:
        # Declared vs executed mode disagree — wake the advisor to reconcile
        # job.yaml (reuses the reconcile_mismatch trigger).
        events.append("reconcile_mismatch")
    health = payload.get("regime_health") or {}
    remediation_event = (health.get("remediation_event") or {}).get("event")
    if remediation_event in {"regime_shift", "regime_remediation_due"}:
        events.append(str(remediation_event))
    elif (health.get("transition") or {}).get("alert"):
        # Compatibility with reports produced before durable remediation cases.
        events.append("regime_shift")
    return events


def _is_retryable_data_failure(payload: Mapping[str, Any]) -> bool:
    """Keep transient public-data throttling on the mechanical retry path.

    Derived-feature refresh already journals persistent feed degradation and
    recovery. Waking an LLM for each 429 cannot repair the provider and was a
    major source of duplicate canary sessions; non-rate-limit failures retain
    the existing immediate ``script_failure`` trigger.
    """
    error = str(payload.get("error") or "").lower()
    return any(
        marker in error
        for marker in (
            "rate_limited",
            "rate limited",
            "too many requests",
            "http 429",
            "status code 429",
        )
    )


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

    # Owner-owned ceiling over the agent-writable leverage knob. Clamped
    # BEFORE the strategy is constructed and before _apply_engine_leverage
    # consumes params, so both engine-scaled and compound-mode strategies see
    # the governed value. No governance ceiling -> params untouched.
    leverage_notes: list[dict[str, Any]] = []
    requested_leverage = params.get("leverage")
    hard_constraints = governance_hard_constraints(root)
    effective_leverage, leverage_ceiling = clamp_leverage(
        requested_leverage, hard_constraints
    )
    if leverage_ceiling is not None:
        params["leverage"] = effective_leverage
        clamp_payload = {
            "requested": requested_leverage,
            "max_leverage": leverage_ceiling,
            "effective": effective_leverage,
        }
        leverage_notes.append({"kind": "leverage_clamped", **clamp_payload})
        store.append_journal(
            job.id, {"type": "leverage_clamped", "mode": mode, **clamp_payload}
        )

    # Optional owner-governed response to a warning/critical portfolio regime
    # report. This is a runtime cap only: it does not edit the user's leverage
    # dial or invalidate the strategy revision, and releases when health clears.
    from wayfinder_paths.jobs.regime_health import (
        active_regime_leverage_cap,
        regime_health_job,
    )

    try:
        # Recompute before consulting a mutating policy. regime_health.json is
        # agent-visible state and therefore cannot itself authorize a clamp.
        pre_tick_health = regime_health_job(job.id, store=store, force=True)
        regime_cap = active_regime_leverage_cap(pre_tick_health)
    except Exception as exc:  # noqa: BLE001 — monitor failures do not stop trading
        regime_cap = None
        store.append_journal(
            job.id, {"type": "regime_health_failed", "error": str(exc)[:300]}
        )
    if regime_cap is not None and effective_leverage > regime_cap:
        params["leverage"] = regime_cap
        regime_payload = {
            "requested": requested_leverage,
            "max_leverage": regime_cap,
            "effective": regime_cap,
            "source": "regime_health",
        }
        leverage_notes.append({"kind": "regime_leverage_clamped", **regime_payload})
        store.append_journal(
            job.id,
            {"type": "regime_leverage_clamped", "mode": mode, **regime_payload},
        )

    strategy = _load_strategy(entrypoint, params)

    revision = str(
        os.environ.get("WAYFINDER_JOB_REVISION")
        or (job.versioning or {}).get("active_revision")
        or ""
    )
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    state_path = root / ENGINE_STATE_PATH
    state_file_existed = state_path.exists()
    state = EngineState.load(state_path)
    mode_notes: list[dict[str, Any]] = []
    if state_file_existed and state.mode and state.mode != mode:
        # A paper test tick otherwise pollutes live (consumed bars, stale
        # bar_count, paper positions). Archive and start fresh; clearing
        # state_file_existed re-arms first-run venue adoption in _reconcile.
        archive_path = state_path.with_name(
            f"engine_state.{state.mode}.{now.strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        state_path.rename(archive_path)
        # Peak equity is source-scoped (venue vs modelled): a paper-modelled
        # peak surviving into live would make every live tick cross-source,
        # permanently skipping the drawdown check. Archive it with the mode.
        risk_state_path = root / RISK_STATE_PATH
        if risk_state_path.exists():
            risk_state_path.rename(
                risk_state_path.with_name(
                    f"risk_state.{state.mode}.{now.strftime('%Y%m%dT%H%M%SZ')}.json"
                )
            )
        mode_notes.append(
            {
                "kind": "mode_flip_state_reset",
                "from_mode": state.mode,
                "to_mode": mode,
                "archived": str(archive_path),
            }
        )
        state = EngineState()
        state_file_existed = False
    state.mode = mode
    state.revision = revision or state.revision

    if adapters is None:
        adapters = {
            venue: build_adapter(venue, mode=mode, spec=spec, params=params)
            for venue in (spec.venues or ["hyperliquid"])
        }
    brokers = {name: adapter.broker for name, adapter in adapters.items()}

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

    venue_states: dict[str, VenueState] = {}
    snapshot, reconcile_notes = await _reconcile(
        mode=mode,
        state=state,
        brokers=brokers,
        symbols=symbols,
        state_file_existed=state_file_existed,
        venue_state_sink=venue_states,
    )

    (
        protection_notes,
        protection_fills,
        protection_rows,
        protection_halt,
    ) = await monitor_native_protection(
        mode=mode,
        state=state,
        brokers=brokers,
        venue_states=venue_states,
        now=now,
    )
    if protection_halt:
        request_halt(
            store,
            job.id,
            reason=protection_halt,
            flatten=False,
            source="native_protection",
        )
        snapshot = StateSnapshot(
            status="risk_halt", reason=protection_halt, data=snapshot.data
        )

    # Account-level circuit breakers (workspace/risk_limits.json, optional).
    # Downgrades only a valid snapshot: an already-ambiguous state is a
    # stronger signal and must not be masked by a risk halt.
    halt_reason, risk_snapshot = check_risk_halt(
        root,
        state=state,
        view=view,
        params=params,
        now=now,
        account_equity=(snapshot.data or {}).get("account_value"),
    )
    risk_notes: list[dict[str, Any]] = []
    if halt_reason:
        risk_notes.append(
            {"kind": "risk_halt", "reason": halt_reason, "snapshot": risk_snapshot}
        )
        if snapshot.status == "valid":
            snapshot = StateSnapshot(
                status="risk_halt", reason=halt_reason, data=snapshot.data
            )
        request_halt(
            store,
            job.id,
            reason=halt_reason,
            flatten=False,
            source="risk_limits",
        )

    # Manual kill switch: outranks every other status (including ambiguous)
    # — reduce-only regardless, and cancel queued OPENs before they can
    # settle at the next bar open inside run_tick.
    manual_halt = read_halt(root)
    if manual_halt is not None:
        halt_note = f"manual halt: {manual_halt.get('reason') or 'unspecified'}"
        risk_notes.append({"kind": "manual_halt", "reason": halt_note})
        snapshot = StateSnapshot(
            status="risk_halt", reason=halt_note, data=snapshot.data
        )
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
        stamps = view.timestamps
        feature_window = (stamps[0], stamps[-1]) if stamps else None
        feature_frames = load_feature_rows([root], feature_specs, window=feature_window)
        feature_guards, feature_skip = feature_staleness(
            feature_specs, feature_frames, now
        )
        if not feature_skip:
            view = merge_features(view, feature_frames, feature_specs)

    # Candidate shadows receive the same completed bars and exogenous feature
    # snapshot, but apply their own precompute hook below in an isolated paper
    # engine. Keep this reference before the incumbent adds derived columns.
    shadow_view = view

    # Strategy-precomputed indicator columns (optional `precompute` hook): one
    # vectorized pass over the bounded window, after the exogenous feature
    # merge so precompute() can consume those columns. The backtest applies
    # the same hook over full history — parity by construction, and the
    # derived columns land in view_hash/recorded rows for exact replays.
    view = apply_precompute(strategy, view)

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
    tick.guard_events.extend(mode_notes)
    tick.guard_events.extend(leverage_notes)
    tick.guard_events.extend(reconcile_notes)
    tick.guard_events.extend(protection_notes)
    tick.guard_events.extend(risk_notes)
    tick.guard_events.extend(feature_guards)
    tick.fills = protection_fills + tick.fills
    tick.trade_rows = protection_rows + tick.trade_rows

    protection_failure = next(
        (event for event in tick.guard_events if event.get("halt_required")),
        None,
    )
    if protection_failure is not None:
        reason = str(
            protection_failure.get("reason")
            or protection_failure.get("error")
            or "native protection failed"
        )
        request_halt(
            store,
            job.id,
            reason=reason,
            flatten=False,
            source="native_protection",
        )
        snapshot = StateSnapshot(status="risk_halt", reason=reason, data=snapshot.data)
        tick.snapshot = snapshot

    if (
        manual_halt is not None
        and manual_halt.get("flatten")
        and (state.ledger.positions or state.resting_orders)
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
    funding_rows = await _collect_funding(brokers, root, mode, now)
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
        funding_rows=funding_rows,
        root=root,
        mode=mode,
    )
    # Evolution probation is a true parallel A/B lane: same incoming bars,
    # separate state/telemetry, PaperBroker only. It is deliberately
    # best-effort so candidate computation cannot delay or fail the incumbent.
    try:
        from wayfinder_paths.jobs.background import spawn_detached_op
        from wayfinder_paths.jobs.candidate_shadow import active_candidate_shadows
        from wayfinder_paths.jobs.paper_experiment import enqueue_experiment_view

        if active_candidate_shadows(store, job.id):
            rows = [
                {**row, "timestamp": row["timestamp"].isoformat()}
                for row in shadow_view.to_rows()
            ]
            enqueue_experiment_view(store, job.id, rows=rows, now=now)
            spawn_detached_op(
                store,
                job.id,
                "candidate_shadows",
                {"job_id": job.id},
            )
    except Exception as exc:  # noqa: BLE001 - never blocks incumbent execution
        logger.debug(f"candidate shadows skipped: {exc}")
    try:
        _record_pending_trade_forensics(root, view)
    except Exception as exc:  # noqa: BLE001 — telemetry must never fail a tick
        logger.debug(f"trade forensics skipped: {exc}")
    for note in mode_notes:
        store.append_journal(
            job.id,
            {
                "type": "mode_flip_state_reset",
                "from_mode": note["from_mode"],
                "to_mode": note["to_mode"],
                "archived": note["archived"],
            },
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
        "gates": tick.gates,
    }


async def _reconcile(
    *,
    mode: str,
    state: EngineState,
    brokers: Mapping[str, Any],
    symbols: list[str],
    state_file_existed: bool,
    venue_state_sink: dict[str, VenueState] | None = None,
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
    account_values: dict[str, float] = {}
    for name, broker in brokers.items():
        # A transient backend blip must not become a reconcile_mismatch wake:
        # every ambiguous snapshot journals + triggers the (expensive) agent,
        # and 13 of 13 recent mismatches on the live canary were one-shot
        # fetch failures, not state divergence. Retry briefly; a PERSISTENT
        # failure still goes ambiguous -> reduce-only, unchanged.
        venue_state = None
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                venue_state = await broker.fetch_state(symbols)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
        if venue_state is None:
            return (
                StateSnapshot(
                    status="ambiguous",
                    reason=f"venue state fetch failed: {last_error}",
                ),
                [
                    {
                        "kind": "reconcile_fetch_failed",
                        "venue": name,
                        "reason": str(last_error),
                    }
                ],
            )
        if venue_state_sink is not None:
            venue_state_sink[name] = venue_state
        venue_positions.update(venue_state.positions)
        account_value = (venue_state.balances or {}).get("accountValue")
        if account_value is not None:
            account_values[name] = float(account_value)

    # Venue-marked equity rides the snapshot (NOT params): the drift
    # reconciler replays ticks with raw job.yaml params, so params-borne
    # equity would replay wrong and flag false drift. mark_to_market_equity
    # treats snapshot.data["account_value"] as authoritative in live mode.
    data: dict[str, Any] = (
        {
            "account_value": sum(account_values.values()),
            "account_value_by_venue": dict(account_values),
        }
        if account_values
        else {}
    )

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
        return (
            StateSnapshot(status="valid", reason="adopted_from_venue", data=data),
            notes,
        )

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
        return (
            StateSnapshot(status="ambiguous", reason="; ".join(reasons), data=data),
            notes,
        )
    return StateSnapshot(status="valid", data=data), notes


_FUNDING_STATE_PATH = "state/funding_state.json"
_EQUITY_RECON_PATH = "state/equity_recon.json"


async def _collect_funding(
    brokers: Mapping[str, Any],
    root: Path,
    mode: str,
    now: pd.Timestamp,
) -> list[dict[str, Any]]:
    """Best-effort venue funding rows since the persisted cursor. Funding is
    real PnL that never appears in trade rows; it is recorded ONLY on the
    forward side (never the engine ledger — the drift reconciler replays
    ticks offline and network-injected PnL would flag false drift). The
    cursor seeds at now on first run: pre-go-live funding is not this
    job's."""
    if mode != "live":
        return []
    state_path = root / _FUNDING_STATE_PATH
    now_ms = int(now.timestamp() * 1000)
    try:
        cursor_doc = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cursor_doc = {}
    cursor = cursor_doc.get("cursor_ms")
    if not isinstance(cursor, (int, float)):
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"cursor_ms": now_ms}), encoding="utf-8")
        return []
    rows: list[dict[str, Any]] = []
    max_seen = int(cursor)
    for broker in brokers.values():
        fetch = getattr(broker, "get_funding_payments", None)
        if fetch is None:
            continue
        try:
            for row in await fetch(int(cursor) + 1):
                time_ms = row.get("time_ms")
                if not isinstance(time_ms, (int, float)) or time_ms <= cursor:
                    continue
                rows.append(dict(row))
                max_seen = max(max_seen, int(time_ms))
        except Exception:  # noqa: BLE001 — telemetry must never fail a tick
            continue
    if max_seen > cursor:
        state_path.write_text(json.dumps({"cursor_ms": max_seen}), encoding="utf-8")
    return rows


def _reconciliation_block(
    tick: TickResult,
    *,
    root: Path,
    recorder: ForwardRecorder,
    mode: str,
    view: CompletedBarsView,
) -> dict[str, Any] | None:
    """Per-tick decomposition of venue equity vs what the books explain:
    expected = equity_start + ledger_realized_delta + funding + unrealized.
    Drift is the unexplained remainder (deposits/withdrawals land here by
    design). Uses ledger realized — NOT trades net — because entry fees sit
    in non-reduce-only rows the trade-close recorder skips."""
    if mode != "live":
        return None
    account_value = (tick.snapshot.data or {}).get("account_value")
    if account_value is None:
        return None
    try:
        recon_path = root / _EQUITY_RECON_PATH
        ledger = tick.ledger_snapshot or {}
        realized = float(ledger.get("realized_pnl") or 0.0)
        reset_fired = any(
            event.get("kind") == "mode_flip_state_reset"
            for event in tick.guard_events or []
        )
        try:
            seed = json.loads(recon_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            seed = {}
        if reset_fired or "venue_equity_start" not in seed:
            # Mode flips archive the engine state — the realized baseline
            # restarts with it.
            seed = {
                "venue_equity_start": float(account_value),
                "ledger_realized_at_seed": realized,
                "seeded_at": pd.Timestamp.now(tz="UTC").isoformat(),
            }
            recon_path.parent.mkdir(parents=True, exist_ok=True)
            recon_path.write_text(json.dumps(seed), encoding="utf-8")

        unrealized = 0.0
        for symbol, position in (ledger.get("positions") or {}).items():
            latest = view.latest(symbol)
            close = (
                float(latest.get("close"))
                if latest and latest.get("close") is not None
                else None
            )
            if close is None:
                continue
            avg = float(position.get("avg_price") or 0.0)
            size = float(position.get("size") or 0.0)
            direction = 1.0 if str(position.get("side")) == "long" else -1.0
            unrealized += direction * (close - avg) * size

        summary = recorder.summary()
        funding_total = float((summary.get("funding") or {}).get("total_usd") or 0.0)
        trades_net = float((summary.get("trades") or {}).get("net_pnl") or 0.0)
        fees_total = float((summary.get("fills") or {}).get("fees_total") or 0.0)
        realized_delta = realized - float(seed.get("ledger_realized_at_seed") or 0.0)
        expected = (
            float(seed.get("venue_equity_start") or 0.0)
            + realized_delta
            + funding_total
            + unrealized
        )
        return {
            "venue_equity_start": seed.get("venue_equity_start"),
            "venue_equity_now": float(account_value),
            "ledger_realized_delta": round(realized_delta, 6),
            "unrealized": round(unrealized, 6),
            "funding_total": round(funding_total, 6),
            "trades_net": round(trades_net, 6),
            "fees_total": round(fees_total, 6),
            "expected_equity": round(expected, 6),
            "drift": round(float(account_value) - expected, 6),
            "_basis": (
                "expected = equity_start + ledger_realized_delta + funding + "
                "unrealized; drift = venue - expected (deposits/withdrawals "
                "surface as drift by design). trades_net/fees_total are "
                "reported for visibility, not used in drift."
            ),
        }
    except Exception:  # noqa: BLE001 — telemetry must never fail a tick
        return None


def _record(
    recorder: ForwardRecorder,
    tick: TickResult,
    *,
    view: CompletedBarsView,
    params: Mapping[str, Any],
    now: pd.Timestamp,
    engine_state_pre: Mapping[str, Any] | None = None,
    funding_rows: list[dict[str, Any]] | None = None,
    root: Path | None = None,
    mode: str | None = None,
) -> None:
    intents = [intent.to_dict() for intent in tick.intents]
    fills = [fill.to_dict() for fill in tick.fills]
    timestamps = view.timestamps
    for intent in intents:
        recorder.record_order(intent)
    for row in fills:
        recorder.record_fill(row)
    # trade_rows are FillEvent.to_dict() + realized_pnl_delta: fixed shape.
    for row in tick.trade_rows:
        if row["reduce_only"]:
            recorder.record_trade_close(_trade_close_payload(row, params=params))
    for row in funding_rows or []:
        recorder.record_funding(row)
    # Reconciliation runs AFTER the rows above so summary totals include
    # this tick's fees/funding.
    reconciliation = (
        _reconciliation_block(tick, root=root, recorder=recorder, mode=mode, view=view)
        if root is not None and mode is not None
        else None
    )
    recorder.record_tick(
        reconciliation=reconciliation,
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
        gates=tick.gates,
        params_hash=hashlib.sha256(
            json.dumps(dict(params), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16],
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


def _trade_close_payload(
    row: Mapping[str, Any], *, params: Mapping[str, Any]
) -> dict[str, Any]:
    """Preserve the execution facts needed to diagnose a live stop-out."""
    raw = dict(row.get("raw") or {})
    metadata = dict(raw.get("intent_metadata") or {})
    action = str(raw.get("intent_action") or "").upper()
    bracket = dict(metadata.get("bracket") or {})
    exit_reason = metadata.get("exit_reason")
    if not exit_reason and action == "STOP_LOSS":
        exit_reason = "bracket_stop"
    elif not exit_reason and action == "TAKE_PROFIT":
        exit_reason = "bracket_take_profit"
    trigger_price = bracket.get("trigger_price")
    fill_price = row.get("avg_price")
    stop_slippage_bps = None
    if action == "STOP_LOSS" and trigger_price and fill_price:
        exit_side = str(row.get("side") or "").lower()
        adverse_move = (
            float(fill_price) - float(trigger_price)
            if exit_side in {"buy", "long"}
            else float(trigger_price) - float(fill_price)
        )
        stop_slippage_bps = round(adverse_move / float(trigger_price) * 10_000, 1)
    venue = str(row.get("venue") or "")
    payload = {
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "size": row.get("filled_size"),
        "price": fill_price,
        "net_pnl": row.get("realized_pnl_delta"),
        "closed_at": row.get("timestamp"),
        "venue": venue,
        "fee": row.get("fee"),
        "order_id": row.get("order_id"),
        "client_order_id": row.get("client_order_id"),
        "exit_reason": exit_reason,
        "effective_leverage": params.get("leverage") or 1.0,
    }
    if action == "STOP_LOSS":
        payload.update(
            {
                "stop_trigger_price": trigger_price,
                "stop_reference_price": bracket.get("price")
                or raw.get("reference_price"),
                "stop_gap_at_open": bracket.get("gap_at_open"),
                "stop_slippage_bps": stop_slippage_bps,
                "stop_slippage_bps_applied": raw.get("slippage_bps_applied"),
                "protection_type": "trigger_market",
                "venue_stop_slippage_tolerance_bps": (
                    1_000 if venue == "hyperliquid" else None
                ),
            }
        )
    return payload


def view_hash(view: CompletedBarsView) -> str:
    encoded = json.dumps(view.to_rows(), sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


FORENSICS_POST_BARS = 16
_FORENSICS_SCAN_TRADES = 40
_FORENSICS_SCAN_FILLS = 400


def _read_jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _record_pending_trade_forensics(root: Path, view: CompletedBarsView) -> int:
    """Lazily append path forensics for closed trades once the bars window
    covers their post-exit horizon.

    Post-exit excursion needs FUTURE bars, so forensics for a trade land
    ~FORENSICS_POST_BARS bars after its close — computed from the live view
    already in memory (no extra fetches), keyed by (symbol, exit ts) so each
    trade is written exactly once.
    """
    from wayfinder_paths.jobs.trade_forensics import forensics_for_closed_trades

    trades_path = root / "results" / "forward" / "trades.jsonl"
    out_path = root / "results" / "forward" / "trade_forensics.jsonl"
    trades = _read_jsonl_tail(trades_path, _FORENSICS_SCAN_TRADES)
    if not trades:
        return 0
    done = {
        (str(row.get("symbol")), str(row.get("exit_ts")))
        for row in _read_jsonl_tail(out_path, _FORENSICS_SCAN_TRADES * 2)
    }
    timestamps = view.timestamps
    if not timestamps:
        return 0

    pending: list[dict[str, Any]] = []
    for trade in trades:
        exit_ts = pd.Timestamp(str(trade.get("closed_at") or trade.get("timestamp")))
        if exit_ts.tzinfo is None:
            exit_ts = exit_ts.tz_localize("UTC")
        if (str(trade.get("symbol")), exit_ts.isoformat()) in done:
            continue
        post_available = sum(1 for ts in timestamps if ts > exit_ts)
        if post_available < FORENSICS_POST_BARS:
            continue
        pending.append(trade)
    if not pending:
        return 0

    fills = _read_jsonl_tail(
        root / "results" / "forward" / "fills.jsonl", _FORENSICS_SCAN_FILLS
    )
    bars_by_symbol = {symbol: view.symbol_frame(symbol) for symbol in view.symbols}
    rows = forensics_for_closed_trades(bars_by_symbol, pending, fills)
    if not rows:
        return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return len(rows)
