"""Pre-registered decision gates: mechanical rework-vs-retire resolution.

An improver that says "if after 20 trades the win rate is still <= X and net
<= Y, retire this line and pivot" has already made the decision — only the
click was outstanding. Today that criteria lives as prose in agendas, so
resolving it needs an owner even though the criteria and the response were
fixed in advance. This registry makes the gate mechanical: register criteria
plus a scoped successor BEFORE the evidence window closes
(``pre_registered_ts`` proves it), the watchdog evaluates the criteria
against the measured forward summary, and for PAPER jobs executes the
pre-registered response with journaled evidence and a bounded undo.

Live-capital jobs never auto-resolve: a tripped gate flips to
``tripped_needs_owner`` and lands in ``owner_attention.needs_you``.

Retire is deliberately bounded (no deep archive-flow reuse): script loop
disabled + recompiled (runner job deleted), incumbent workspace COPIED to
``versions/`` (the active tree is left in place so reopen is non-destructive),
and the pivot written to ``research/agenda.md`` attributed to ``gate-auto`` —
never to the owner.
"""

from __future__ import annotations

import datetime as dt
import shutil
import uuid
from typing import Any

from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.store import JobStore

DECISION_GATES_PATH = "research/decision_gates.json"
GATE_STATUSES = ("armed", "tripped_needs_owner", "resolved", "reopened")
GATE_ON_MET = ("retire_and_pivot",)
# Criteria are failure thresholds: the gate trips when the evidence window is
# full (min_trades reached) AND every declared ceiling still holds.
CRITERIA_KEYS = ("min_trades", "max_win_rate", "max_net_pnl", "min_runs")
UNDO_WINDOW_DAYS = 7


def load_decision_gates(store: JobStore, job_id: str) -> dict[str, Any]:
    doc = store.read_json(job_id, DECISION_GATES_PATH) or {}
    if not isinstance(doc, dict) or not isinstance(doc.get("gates"), list):
        return {"gates": []}
    return doc


def register_decision_gate(
    store: JobStore,
    job_id: str,
    *,
    criteria: dict[str, Any],
    successor_ref: str,
    on_met: str = "retire_and_pivot",
    gate_id: str | None = None,
    registered_by: str = "improver",
) -> dict[str, Any]:
    if on_met not in GATE_ON_MET:
        raise ValueError(f"on_met must be one of {GATE_ON_MET}, got {on_met!r}")
    if not str(successor_ref).strip():
        raise ValueError(
            "a decision gate requires a scoped successor_ref — retiring a line "
            "with no registered pivot is abandonment, which is owner-only"
        )
    unknown = sorted(set(criteria) - set(CRITERIA_KEYS))
    if unknown:
        raise ValueError(f"unknown criteria keys {unknown}; supported: {CRITERIA_KEYS}")
    if int(criteria.get("min_trades") or 0) < 1:
        raise ValueError("criteria.min_trades must be >= 1 (the evidence window)")
    if not any(key in criteria for key in ("max_win_rate", "max_net_pnl")):
        raise ValueError(
            "criteria must declare at least one ceiling (max_win_rate or "
            "max_net_pnl) — a gate with only a trade count always trips"
        )
    doc = load_decision_gates(store, job_id)
    gid = str(gate_id or f"gate-{uuid.uuid4().hex[:8]}")
    if any(gate.get("gate_id") == gid for gate in doc["gates"]):
        raise ValueError(f"decision gate already registered: {gid}")
    gate = {
        "gate_id": gid,
        "status": "armed",
        "criteria": {key: criteria[key] for key in CRITERIA_KEYS if key in criteria},
        "on_met": on_met,
        "successor_ref": str(successor_ref).strip(),
        "pre_registered_ts": utc_now_iso(),
        "registered_by": registered_by,
    }
    doc["gates"].append(gate)
    store.write_json(job_id, DECISION_GATES_PATH, doc)
    store.append_journal(
        job_id,
        {
            "type": "decision_gate_registered",
            "gate_id": gid,
            "criteria": gate["criteria"],
            "on_met": on_met,
            "successor_ref": gate["successor_ref"],
            "registered_by": registered_by,
        },
    )
    return gate


def evaluate_gate_criteria(
    criteria: dict[str, Any], forward_summary: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    trades = forward_summary.get("trades") or {}
    runs = forward_summary.get("runs") or {}
    closed_trades = int(trades.get("closed_count") or 0)
    win_rate = trades.get("win_rate")
    net_pnl = float(trades.get("net_pnl") or 0.0)
    run_count = int(runs.get("count") or 0)
    measured: dict[str, Any] = {
        "closed_trades": closed_trades,
        "win_rate": win_rate,
        "net_pnl": net_pnl,
        "runs": run_count,
    }
    if closed_trades < int(criteria.get("min_trades") or 0):
        return False, measured
    if "min_runs" in criteria and run_count < int(criteria["min_runs"]):
        return False, measured
    if "max_win_rate" in criteria and (
        win_rate is None or float(win_rate) > float(criteria["max_win_rate"])
    ):
        return False, measured
    if "max_net_pnl" in criteria and net_pnl > float(criteria["max_net_pnl"]):
        return False, measured
    return True, measured


def evaluate_decision_gates(
    store: JobStore,
    job: WayfinderJob,
    *,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Watchdog standing check: evaluate every armed gate against the measured
    forward summary. Paper job -> execute the pre-registered response; live-
    capital job -> trip to needs_you. Returns the journaled events."""
    from wayfinder_paths.jobs.owner_attention import job_live_capital_risk

    doc = load_decision_gates(store, job.id)
    armed = [gate for gate in doc["gates"] if gate.get("status") == "armed"]
    if not armed:
        return []
    now = now or dt.datetime.now(dt.UTC)
    summary = store.read_json(job.id, "results/forward/summary.json") or {}
    events: list[dict[str, Any]] = []
    for gate in armed:
        met, measured = evaluate_gate_criteria(gate.get("criteria") or {}, summary)
        if not met:
            continue
        if job_live_capital_risk(job):
            gate["status"] = "tripped_needs_owner"
            gate["tripped_at"] = now.isoformat()
            gate["measured"] = measured
            event = {
                "type": "decision_gate_tripped",
                "gate_id": gate["gate_id"],
                "criteria": gate.get("criteria"),
                "measured": measured,
                "successor_ref": gate.get("successor_ref"),
            }
        else:
            event = _auto_resolve(store, job, gate, measured, now)
        store.write_json(job.id, DECISION_GATES_PATH, doc)
        store.append_journal(job.id, event)
        events.append(event)
    return events


def _auto_resolve(
    store: JobStore,
    job: WayfinderJob,
    gate: dict[str, Any],
    measured: dict[str, Any],
    now: dt.datetime,
) -> dict[str, Any]:
    gate_id = str(gate["gate_id"])
    root = store.job_dir(job.id)
    archived_to = f"versions/retired-{gate_id}-{now.strftime('%Y%m%dT%H%M%SZ')}"
    workspace = root / "workspace"
    if workspace.exists():
        shutil.copytree(workspace, root / archived_to / "workspace")
    job.script_loop.enabled = False
    job.touch()
    store.save(job)
    compile_error: str | None = None
    try:
        # Compile with the loop disabled deletes the runner script job — the
        # supported way to stop ticking (same path as apply_script_mode).
        from wayfinder_paths.jobs.compiler import JobCompiler

        JobCompiler(store=store).compile(job)
    except Exception as exc:  # noqa: BLE001 — retirement state is saved; the
        # agent-mode drift check reconciles a runner that kept the stale job.
        compile_error = str(exc)[:300]
    undo = {
        "command": f"wayfinder job decision-gate reopen {job.id} {gate_id}",
        "window_expires_ts": (now + dt.timedelta(days=UNDO_WINDOW_DAYS)).isoformat(),
    }
    _append_agenda_note(
        store,
        job.id,
        (
            f"\n## Decision gate auto-resolution (gate-auto) — {now.isoformat()}\n\n"
            f"Gate `{gate_id}` (pre-registered {gate.get('pre_registered_ts')}) "
            f"tripped: {measured['closed_trades']} closed trades, "
            f"win_rate={measured['win_rate']}, net_pnl={measured['net_pnl']}.\n"
            f"Incumbent retired: script loop disabled, workspace archived to "
            f"`{archived_to}`.\n"
            f"Pivot (pre-registered successor): {gate.get('successor_ref')}\n"
            f"Undo within {UNDO_WINDOW_DAYS}d: `{undo['command']}`\n"
        ),
    )
    gate.update(
        {
            "status": "resolved",
            "resolved_at": now.isoformat(),
            "resolution": {
                "by": "gate-auto",
                "action": str(gate.get("on_met") or "retire_and_pivot"),
                "measured": measured,
                "archived_workspace": archived_to,
                **({"compile_error": compile_error} if compile_error else {}),
            },
        }
    )
    return {
        "type": "gate_auto_resolved",
        "gate_id": gate_id,
        "action": str(gate.get("on_met") or "retire_and_pivot"),
        "criteria": gate.get("criteria"),
        "measured": measured,
        "successor_ref": gate.get("successor_ref"),
        "archived_workspace": archived_to,
        "undo": undo,
    }


def resolve_decision_gate(
    store: JobStore,
    job_id: str,
    gate_id: str,
    *,
    by: str,
    note: str | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    """Manual resolution — the owner acting on a tripped (or armed) gate.
    ``execute=True`` runs the same bounded retire flow the paper path uses.

    Acknowledge-only is completable: ``execute=True`` on a gate already
    resolved as ``acknowledged`` runs the pre-registered response now (an FE
    resolve click that landed without execute must not strand the gate).
    Anything else that is already settled — executed, acknowledged again
    without execute, or reopened — returns the gate with ``noop=True`` and
    changes nothing: double-clicks and timeout retries read as "already
    done", never as failure."""
    doc = load_decision_gates(store, job_id)
    gate = _find_gate(doc, gate_id)
    status = str(gate.get("status"))
    resolution = gate.get("resolution") or {}
    acknowledged_only = (
        status == "resolved" and resolution.get("action") == "acknowledged"
    )
    if status not in {"armed", "tripped_needs_owner"} and not (
        acknowledged_only and execute
    ):
        return dict(gate, noop=True)
    if execute:
        prior_ack = (
            {
                "by": resolution.get("by"),
                "note": resolution.get("note"),
                "at": gate.get("resolved_at"),
            }
            if acknowledged_only
            else None
        )
        job = store.load(job_id)
        summary = store.read_json(job_id, "results/forward/summary.json") or {}
        _, measured = evaluate_gate_criteria(gate.get("criteria") or {}, summary)
        event = _auto_resolve(store, job, gate, measured, dt.datetime.now(dt.UTC))
        event["resolved_by"] = by
        gate["resolution"]["by"] = by
        if prior_ack is not None:
            event["type"] = "decision_gate_executed"
            gate["resolution"]["acknowledged"] = prior_ack
    else:
        gate.update(
            {
                "status": "resolved",
                "resolved_at": utc_now_iso(),
                "resolution": {"by": by, "action": "acknowledged", "note": note},
            }
        )
        event = {
            "type": "decision_gate_resolved",
            "gate_id": gate_id,
            "by": by,
            "note": note,
        }
    store.write_json(job_id, DECISION_GATES_PATH, doc)
    store.append_journal(job_id, event)
    return gate


def reopen_decision_gate(
    store: JobStore, job_id: str, gate_id: str, *, by: str = "owner"
) -> dict[str, Any]:
    """Undo: reverse an auto-resolution (re-enable the script loop; the active
    workspace was never destroyed) or dismiss a trip. The gate lands in
    ``reopened`` — terminal until explicitly re-registered, so it cannot
    re-trip on the very next watchdog pass. Reopening an already-reopened
    gate is a retry, not a failure: it returns the gate with ``noop=True``."""
    doc = load_decision_gates(store, job_id)
    gate = _find_gate(doc, gate_id)
    status = str(gate.get("status"))
    if status == "reopened":
        return dict(gate, noop=True)
    if status not in {"resolved", "tripped_needs_owner"}:
        raise ValueError(f"gate {gate_id} is {status}; nothing to reopen")
    was_retired = (gate.get("resolution") or {}).get("action") == "retire_and_pivot"
    if was_retired:
        job = store.load(job_id)
        job.script_loop.enabled = True
        job.touch()
        store.save(job)
        from wayfinder_paths.jobs.compiler import JobCompiler

        JobCompiler(store=store).compile(job)
    gate.update({"status": "reopened", "reopened_at": utc_now_iso(), "reopened_by": by})
    store.write_json(job_id, DECISION_GATES_PATH, doc)
    store.append_journal(
        job_id,
        {
            "type": "decision_gate_reopened",
            "gate_id": gate_id,
            "by": by,
            "script_loop_reenabled": was_retired,
        },
    )
    return gate


def _find_gate(doc: dict[str, Any], gate_id: str) -> dict[str, Any]:
    for gate in doc["gates"]:
        if gate.get("gate_id") == gate_id:
            return gate
    raise ValueError(f"decision gate not found: {gate_id}")


def _append_agenda_note(store: JobStore, job_id: str, text: str) -> None:
    path = store.job_dir(job_id) / "research" / "agenda.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
