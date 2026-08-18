"""Application watchdog: no proposal apply may stall a job.

`claim_application` pauses both runner loops, so an application stuck in
"applying" leaves the job dark — its own loops can never self-heal (they are
paused) and `triggers.py` suppresses agent wakes mid-apply. This watchdog is
the independent owner of that state: a runner-registered interval job that
scans every job's proposals and drives any stalled application to a terminal
status, which always resumes the loops.

Recovery policy: an approved candidate the user already signed off on is
worth landing, so a stalled deterministic apply is re-completed as "applied"
(full validation still gates the promotion; failure rolls back and resumes).
Agent-owned applies (no candidate_report — nothing staged to validate) are
failed after a longer window; claim allows retry from "failed".
"""

from __future__ import annotations

import json
import os
import re
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent
from typing import Any

from wayfinder_paths.jobs.application import complete_application
from wayfinder_paths.jobs.failures import cpu_steal_pct
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.runner_bridge import RunnerBridge
from wayfinder_paths.jobs.store import JobStore

WATCHDOG_RUNNER_JOB_NAME = "wayfinder-application-watchdog"
WATCHDOG_INTERVAL_SECONDS = 300
WATCHDOG_TIMEOUT_SECONDS = 2700

# Deterministic applies (candidate_report staged): completer child normally
# finishes in 1-3 minutes; a dead completer past this age is recovered.
DETERMINISTIC_APPLYING_TIMEOUT = timedelta(minutes=15)
# Agent-owned applies (ungated, agent claims itself): sessions legitimately
# take longer, but past this the session is presumed dead.
AGENT_APPLYING_TIMEOUT = timedelta(minutes=60)
# Approved + queued with a staged candidate but no spawn: the approve process
# crashed between queueing and spawning the completer.
QUEUED_TIMEOUT = timedelta(minutes=10)
# A completer pid that is still alive but has made no progress for this long
# is wedged: kill its process group, then recover.
HARD_KILL_TIMEOUT = timedelta(minutes=45)
# A code-change re-stage the agent has not resolved past this age gets the
# restage wake re-fired (once per window) — one truncated prompt or dead
# session must not strand an owner-approved change.
RESTAGE_NAG_TIMEOUT = timedelta(minutes=30)
# Mechanical re-stages are retried across watchdog passes (one per pass) —
# an OOM'd backtest must not strand an owner-approved change — but bounded:
# past this many failed attempts the owner is escalated instead.
RESTAGE_MAX_ATTEMPTS = 5
# A kind=process owner rejection expects a corrected successor proposal.
# None within this window → wake the agent once per entry.
SUCCESSOR_OVERDUE_TIMEOUT = timedelta(hours=12)
_SUCCESSOR_EXPECTED_PATH = "state/successor_expected.json"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _age(now: datetime, stamp: Any) -> timedelta:
    """Age of an ISO timestamp; unparseable/missing stamps count as expired
    so a malformed application record cannot stall forever."""
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return timedelta.max
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return now - parsed


def _recover_applying(
    store: JobStore,
    job_id: str,
    proposal: dict[str, Any],
    now: datetime,
) -> dict[str, Any] | None:
    proposal_id = str(proposal["proposal_id"])
    application = proposal["application"]
    age = _age(now, application.get("started_at") or proposal.get("updated_at"))

    worker = application.get("apply_worker") or {}
    pid = int(worker.get("pid") or 0)
    if _pid_alive(pid):
        if age < HARD_KILL_TIMEOUT:
            return None
        _kill_process_group(pid)

    deterministic = bool(proposal.get("candidate_report"))
    timeout = (
        DETERMINISTIC_APPLYING_TIMEOUT if deterministic else AGENT_APPLYING_TIMEOUT
    )
    if age < timeout:
        return None

    action = "complete_applied" if deterministic else "complete_failed"
    try:
        if deterministic:
            complete_application(store, job_id, proposal_id, status="applied")
        else:
            complete_application(
                store,
                job_id,
                proposal_id,
                status="failed",
                error="watchdog: apply session stalled; loops resumed",
            )
    except ValueError as exc:
        # Already terminal — a live completer or manual recovery won the race.
        store.append_journal(
            job_id,
            {
                "type": "application_watchdog_skipped",
                "proposal_id": proposal_id,
                "reason": str(exc),
            },
        )
        return None
    outcome = store.load_proposal(job_id, proposal_id)["application"]["status"]
    event = {
        "type": "application_watchdog_recovered",
        "proposal_id": proposal_id,
        "stalled_status": "applying",
        "age_seconds": int(age.total_seconds()) if age != timedelta.max else None,
        "action": action,
        "outcome": outcome,
    }
    store.append_journal(job_id, event)
    return event


def _recover_queued(
    store: JobStore,
    job_id: str,
    proposal: dict[str, Any],
    now: datetime,
) -> dict[str, Any] | None:
    if proposal.get("status") != "approved" or not proposal.get("candidate_report"):
        # Ungated queued proposals are agent-owned and queued never pauses
        # loops — no stall to recover.
        return None
    application = proposal["application"]
    age = _age(now, application.get("requested_at") or proposal.get("updated_at"))
    if age < QUEUED_TIMEOUT:
        return None
    proposal_id = str(proposal["proposal_id"])
    from wayfinder_paths.jobs.apply_launcher import start_application

    try:
        start_application(store, job_id, proposal_id)
    except ValueError as exc:
        store.append_journal(
            job_id,
            {
                "type": "application_watchdog_skipped",
                "proposal_id": proposal_id,
                "reason": str(exc),
            },
        )
        return None
    event = {
        "type": "application_watchdog_recovered",
        "proposal_id": proposal_id,
        "stalled_status": "queued",
        "age_seconds": int(age.total_seconds()) if age != timedelta.max else None,
        "action": "start_application",
        "outcome": "applying",
    }
    store.append_journal(job_id, event)
    return event


def _recover_restage(
    store: JobStore,
    job_id: str,
    proposal: dict[str, Any],
    now: datetime,
    *,
    allow_mechanical: bool = True,
) -> dict[str, Any] | None:
    """Autonomous recovery for approval-carryover re-stages.

    Params updates need no authoring — the change is the stored params dict —
    so the watchdog re-stages them mechanically (full gate re-run + auto
    queue) instead of waiting on an agent session. Code changes must be
    re-authored by the agent; if one sits unresolved past the nag window
    (a truncated prompt, a dead session, a wrong turn into propose), the
    watchdog re-fires the restage wake so the pipeline converges without an
    owner touch.
    """
    application = proposal["application"]
    if proposal.get("status") != "approved":
        return None
    if not application.get("restage_requested"):
        return None
    if application.get("status") in {"queued", "applying", "applied"}:
        return None
    proposal_id = str(proposal["proposal_id"])
    params = (proposal.get("proposed_change") or {}).get("execution_params")

    if params:
        if not allow_mechanical:
            return None  # next pass, 5 minutes out
        attempts = int(application.get("restage_attempts") or 0)
        if attempts >= RESTAGE_MAX_ATTEMPTS:
            if application.get("restage_attempts_exhausted"):
                return None
            application["restage_attempts_exhausted"] = True
            store.write_proposal(job_id, proposal)
            event = {
                "type": "application_watchdog_recovered",
                "proposal_id": proposal_id,
                "stalled_status": "restage_requested",
                "action": "restage_attempts_exhausted",
                "outcome": "needs_attention",
                "attempts": attempts,
            }
            store.append_journal(job_id, event)
            return event
        # circular import: proposals → worker → …driver → triggers → watchdog
        from wayfinder_paths.jobs.proposals import restage_proposal

        try:
            result = restage_proposal(store, job_id, proposal_id)
        except Exception as exc:
            from wayfinder_paths.jobs.failures import classify_failure

            failure_kind = classify_failure(str(exc))
            # Persist the bounded retry counter on the CURRENT on-disk
            # proposal — restage_proposal reloads and rewrites it internally,
            # so our in-memory copy may be stale. An infrastructure-class
            # failure keeps the re-stage request alive so later passes retry
            # (the failed restage may have cleared the flag before dying).
            fresh = store.load_proposal(job_id, proposal_id)
            fresh_application = fresh["application"]
            fresh_application["restage_attempts"] = attempts + 1
            fresh_application["restage_last_error"] = str(exc)[:300]
            if failure_kind == "infrastructure":
                fresh_application["restage_requested"] = True
            store.write_proposal(job_id, fresh)
            store.append_journal(
                job_id,
                {
                    "type": "application_watchdog_skipped",
                    "proposal_id": proposal_id,
                    "reason": f"mechanical restage failed: {exc}",
                    "failure_kind": failure_kind,
                    "restage_attempts": attempts + 1,
                },
            )
            return None
        event = {
            "type": "application_watchdog_recovered",
            "proposal_id": proposal_id,
            "stalled_status": "restage_requested",
            "action": "mechanical_restage",
            "outcome": result.get("application", {}).get("status")
            or result.get("status"),
        }
        store.append_journal(job_id, event)
        return event

    age = _age(now, application.get("finished_at") or proposal.get("updated_at"))
    if age < RESTAGE_NAG_TIMEOUT:
        return None
    last_nag = application.get("restage_nag_ts")
    if last_nag and _age(now, last_nag) < RESTAGE_NAG_TIMEOUT:
        return None
    from wayfinder_paths.jobs.triggers import fire_triggers

    fire_triggers(
        store,
        store.load(job_id),
        ["proposal_restage_requested"],
        source=f"watchdog-nag:{proposal_id}",
    )
    application["restage_nag_ts"] = now.isoformat()
    store.write_proposal(job_id, proposal)
    event = {
        "type": "application_watchdog_recovered",
        "proposal_id": proposal_id,
        "stalled_status": "restage_requested",
        "age_seconds": int(age.total_seconds()) if age != timedelta.max else None,
        "action": "restage_wake_nag",
        "outcome": "agent_renotified",
    }
    store.append_journal(job_id, event)
    return event


def _check_successor_overdue(
    store: JobStore,
    job_id: str,
    proposals: list[dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """Wake the agent when a process-rejection's expected successor is late.

    A kind=process owner rejection (superseded draft, re-stage mechanics) is
    an INVITATION, not a veto — the owner expects a corrected successor
    proposal. If none has been created within the window, journal
    `successor_overdue` once per entry and fire a trigger wake so the
    invitation cannot silently rot.
    """
    expected = store.read_json(job_id, _SUCCESSOR_EXPECTED_PATH) or []
    if not isinstance(expected, list):
        return []
    events: list[dict[str, Any]] = []
    changed = False
    for entry in expected:
        if not isinstance(entry, dict) or entry.get("notified"):
            continue
        if _age(now, entry.get("ts")) < SUCCESSOR_OVERDUE_TIMEOUT:
            continue
        entry_ts = str(entry.get("ts") or "")
        rejected_id = str(entry.get("proposal_id") or "")
        # A successor is any OTHER proposal staged after the rejection —
        # candidate_report.generated_at is the propose/restage stamp;
        # updated_at is the fallback for report-less proposals.
        successor_exists = any(
            str(p.get("proposal_id")) != rejected_id
            and str(
                (p.get("candidate_report") or {}).get("generated_at")
                or p.get("updated_at")
                or ""
            )
            > entry_ts
            for p in proposals
        )
        entry["notified"] = True
        changed = True
        if successor_exists:
            continue
        store.append_journal(
            job_id,
            {
                "type": "successor_overdue",
                "proposal_id": rejected_id,
                "rejected_ts": entry_ts,
                "reason": entry.get("reason"),
            },
        )
        from wayfinder_paths.jobs.triggers import fire_triggers

        fire_triggers(
            store, store.load(job_id), ["successor_overdue"], source="watchdog"
        )
        events.append(
            {
                "action": "successor_overdue",
                "proposal_id": rejected_id,
                "outcome": "agent_woken",
            }
        )
    if changed:
        store.write_json(job_id, _SUCCESSOR_EXPECTED_PATH, expected)
    return events


# Matches "backtest is for revision ab12cd34ef56, workspace is 0f1e2d3c4b5a" —
# the gate reason shape for pure stamp staleness (evidence fine, fingerprint
# old). Substantive failures (validation failed, contract, missing dataset)
# do NOT match and are never auto-repaired.
_REVISION_MISMATCH_RE = re.compile(
    r"is for revision [0-9a-f]{6,}, workspace is [0-9a-f]{6,}"
)
_GATE_RESTAMP_MARKER = "state/gate_restamp.json"
# A gate re-stamp is a full 120d backtest. Under heavy hypervisor CPU steal
# (observed 81-93% on the production box) that backtest held the
# heavy-compute lock 45-60 minutes — starving everything else for a repair
# that is merely housekeeping. Deferral is safe-closed: the gate stays red a
# little longer; the next 5-minute pass retries when the box breathes again.
RESTAMP_STEAL_THRESHOLD_PCT = 60.0


def _recover_stale_gate(
    store: JobStore,
    job_id: str,
    proposals: list[dict[str, Any]],
    *,
    allow_restamp: bool = True,
) -> dict[str, Any] | None:
    """Re-stamp gate evidence when it is red PURELY from revision mismatch.

    Config edits and (historically) agent memory writes move the workspace
    revision without touching the stamped artifacts; the gate then blocks
    approvals until someone re-runs backtest -> preflight -> validate. That
    "someone" was a human three times in one week. Re-running the chain only
    regenerates evidence at the current workspace — if the current code is
    genuinely worse, the fresh artifacts say so and the gate stays red for
    REAL reasons. A convergence marker stops the repair from looping: red at
    a revision we already re-stamped escalates once instead of burning a
    backtest every pass.
    """
    if any(p["application"].get("status") in {"queued", "applying"} for p in proposals):
        return None  # promotion re-stamps as a side effect; let the apply run
    # circular import: gating -> store; job/preflight/validation import deeply
    from wayfinder_paths.jobs.gating import (
        compute_workspace_revision,
        evaluate_live_gate,
    )

    root = store.job_dir(job_id)
    marker = store.read_json(job_id, _GATE_RESTAMP_MARKER) or {}
    gate = evaluate_live_gate(job_id, store=store)
    if gate.get("live_ready"):
        if marker:
            store.write_json(job_id, _GATE_RESTAMP_MARKER, {})
        return None
    reasons = [str(r) for r in gate.get("reasons") or []]
    if not reasons or not all(_REVISION_MISMATCH_RE.search(r) for r in reasons):
        return None  # substantive red — must stay visible, never masked
    revision = compute_workspace_revision(root)
    if str(marker.get("revision") or "") == revision:
        # Already re-stamped at this exact revision and the gate is still
        # red: repair does not converge — escalate once, then stand down.
        if not marker.get("escalated"):
            marker["escalated"] = True
            store.write_json(job_id, _GATE_RESTAMP_MARKER, marker)
            event = {
                "type": "application_watchdog_recovered",
                "stalled_status": "stale_gate",
                "action": "gate_restamp_not_converging",
                "outcome": "needs_attention",
                "revision": revision,
                "reasons": reasons[:4],
            }
            store.append_journal(job_id, event)
            return event
        return None
    if not allow_restamp:
        return None  # one re-stamp (a backtest) per pass; next pass retries
    steal = cpu_steal_pct()
    if steal is not None and steal > RESTAMP_STEAL_THRESHOLD_PCT:
        event = {
            "type": "restamp_deferred_load",
            "stalled_status": "stale_gate",
            "action": "restamp_deferred_load",
            "cpu_steal_pct": round(steal, 1),
            "revision": revision,
        }
        store.append_journal(job_id, event)
        return event  # safe-closed: gate stays red; next pass retries

    from wayfinder_paths.jobs.compute_lock import ComputeLockBusy
    from wayfinder_paths.jobs.execution.job import backtest_execution_job
    from wayfinder_paths.jobs.execution.preflight import run_preflight
    from wayfinder_paths.jobs.execution.validation import validate_execution_job

    try:
        backtest_execution_job(job_id, store=store)
        run_preflight(job_id, store=store)
        validate_execution_job(job_id, store=store)
    except ComputeLockBusy:
        return None  # heavy compute in progress; retry next pass
    except Exception as exc:
        store.append_journal(
            job_id,
            {
                "type": "application_watchdog_skipped",
                "reason": f"gate restamp failed: {str(exc)[:250]}",
            },
        )
        return None
    after = evaluate_live_gate(job_id, store=store)
    store.write_json(job_id, _GATE_RESTAMP_MARKER, {"revision": revision})
    event = {
        "type": "application_watchdog_recovered",
        "stalled_status": "stale_gate",
        "action": "gate_restamp",
        "outcome": "green" if after.get("live_ready") else "still_red",
        "revision": revision,
    }
    store.append_journal(job_id, event)
    return event


def _recover_orphaned_pause(
    store: JobStore,
    job_id: str,
    proposals: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Re-issue resumes that failed at apply completion.

    Completion resumes the runner loops best-effort; when the daemon is
    unresponsive (a gate backtest pinning the shared CPU), both resume calls
    time out, the failure is RECORDED on the application — and the job stays
    paused forever, because nothing watches "paused with no apply in
    flight". Seen live: an apply finished `applied` at 18:35 and trading
    stayed dark for 90 minutes. The watchdog now re-issues the recorded
    failed resumes until they succeed; `resume_recovered` marks the terminal
    success so an owner pausing the job later is never overridden.
    """
    in_flight = any(
        p["application"].get("status") in {"queued", "applying"} for p in proposals
    )
    if in_flight:
        return None  # the live apply owns the pause
    candidates = []
    for proposal in proposals:
        application = proposal["application"]
        if application.get("status") not in {"applied", "failed"}:
            continue
        if application.get("resume_recovered"):
            continue
        failed = [
            entry
            for entry in application.get("runner_responses") or []
            if not (entry.get("response") or {}).get("ok")
            and entry.get("runner_job_name")
        ]
        if failed:
            candidates.append(
                (str(application.get("finished_at") or ""), proposal, failed)
            )
    if not candidates:
        return None
    _, proposal, failed = max(candidates, key=lambda item: item[0])
    proposal_id = str(proposal["proposal_id"])
    bridge = RunnerBridge(repo_root=store.repo_root)
    responses = []
    all_ok = True
    for entry in failed:
        name = str(entry["runner_job_name"])
        try:
            response = bridge.resume(name)
        except Exception as exc:  # noqa: BLE001 — retried next pass
            response = {"ok": False, "error": str(exc)}
        responses.append({"runner_job_name": name, "response": response})
        if not (response or {}).get("ok"):
            all_ok = False
    if all_ok:
        # Terminal: never re-resume this application again, so a LATER owner
        # pause of the job is never fought by the watchdog.
        proposal["application"]["resume_recovered"] = True
        store.write_proposal(job_id, proposal)
    event = {
        "type": "application_watchdog_recovered",
        "proposal_id": proposal_id,
        "stalled_status": "orphaned_pause",
        "action": "resume_orphaned_pause",
        "outcome": "resumed" if all_ok else "resume_retry_pending",
        "runner_responses": responses,
    }
    store.append_journal(job_id, event)
    return event


# Runner-gap alerting: during one OOM cascade the LIVE job's script loop went
# dark for 65 minutes with ZERO alerting — the runner daemon was wedged, so
# nothing that depends on the runner could notice. The watchdog is the
# independent observer: it compares the newest forward run/tick timestamp
# against the loop's own interval and journals (and, for live jobs, wakes the
# agent) when the loop has missed several beats.
_LOOP_GAP_ALERT_PATH = "state/loop_gap_alert.json"
LOOP_GAP_INTERVAL_MULTIPLIER = 3
DEFAULT_LOOP_INTERVAL_SECONDS = 300


def _tail_line(path: Path, *, chunk_bytes: int = 2048) -> str | None:
    """Last non-empty line of a possibly multi-MB file, tail-read only."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - chunk_bytes))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = [line for line in chunk.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _last_forward_ts(root: Path) -> datetime | None:
    newest: datetime | None = None
    for name in ("runs.jsonl", "ticks.jsonl"):
        line = _tail_line(root / "results" / "forward" / name)
        if not line:
            continue
        try:
            row = json.loads(line)
            parsed = datetime.fromisoformat(str(row.get("ts")))
        except (ValueError, TypeError, AttributeError):
            continue  # partial/foreign last line — the other file may serve
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        if newest is None or parsed > newest:
            newest = parsed
    return newest


def _check_loop_gap(store: JobStore, job: Any, now: datetime) -> dict[str, Any] | None:
    loop = getattr(job, "script_loop", None)
    if loop is None or not getattr(loop, "enabled", False):
        return None
    interval = int(
        getattr(loop, "interval_seconds", None) or DEFAULT_LOOP_INTERVAL_SECONDS
    )
    last_ts = _last_forward_ts(store.job_dir(job.id))
    if last_ts is None:
        return None  # never ran — startup state, not an outage
    gap = (now - last_ts).total_seconds()
    mode = str(getattr(loop, "mode", "") or "")
    marker = store.read_json(job.id, _LOOP_GAP_ALERT_PATH) or {}
    if gap > LOOP_GAP_INTERVAL_MULTIPLIER * interval:
        if str(marker.get("gap_start_ts") or "") == last_ts.isoformat():
            return None  # already alerted for this outage
        store.write_json(
            job.id,
            _LOOP_GAP_ALERT_PATH,
            {"gap_start_ts": last_ts.isoformat(), "alerted_at": now.isoformat()},
        )
        store.append_journal(
            job.id,
            {
                "type": "runner_loop_gap",
                "gap_seconds": int(gap),
                "interval": interval,
                "mode": mode,
            },
        )
        if mode == "live":
            # circular import: worker → application → … → triggers
            from wayfinder_paths.jobs.triggers import fire_triggers

            fire_triggers(store, job, ["runner_loop_gap"], source="watchdog")
        return {
            "action": "runner_loop_gap",
            "gap_seconds": int(gap),
            "interval": interval,
            "mode": mode,
        }
    if marker:
        store.write_json(job.id, _LOOP_GAP_ALERT_PATH, {})
        store.append_journal(
            job.id,
            {"type": "runner_loop_recovered", "interval": interval, "mode": mode},
        )
        return {"action": "runner_loop_recovered", "mode": mode}
    return None


# Lifecycle evaluation is cheap (json reads + arithmetic) but its decisions
# are consequential — a 6h cadence matches the counterfactual cycle and keeps
# kill/graduate flips out of the 5-minute noise floor.
_LIFECYCLE_MARKER = "state/lifecycle_pass.json"
_LIFECYCLE_INTERVAL_S = 6 * 3600


def _run_lifecycle_pass(
    store: JobStore, job_id: str, now: datetime
) -> list[dict[str, Any]]:
    """Deterministic demotion/retirement: evaluate typed probation predicates
    against measured forward metrics and flip leg status mechanically. The
    agent registers the rules; the controller enforces them — a rule that
    fires only if someone re-reads it is not a rule."""
    from wayfinder_paths.jobs.models import utc_now_iso
    from wayfinder_paths.jobs.predicates import evaluate_predicates, forward_metrics
    from wayfinder_paths.jobs.probation import load_probation, update_probation_leg

    marker = store.read_json(job_id, _LIFECYCLE_MARKER) or {}
    if _age(now, marker.get("ran_at")) < timedelta(seconds=_LIFECYCLE_INTERVAL_S):
        return []
    store.write_json(job_id, _LIFECYCLE_MARKER, {"ran_at": now.isoformat()})

    doc = load_probation(store, job_id)
    active = [leg for leg in doc.get("legs") or [] if leg.get("status") == "active"]
    if not active:
        return []
    trades_path = store.job_dir(job_id) / "results" / "forward" / "trades.jsonl"
    trades: list[dict[str, Any]] = []
    if trades_path.exists():
        for line in trades_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                trades.append(row)

    events: list[dict[str, Any]] = []
    for leg in active:
        name = str(leg.get("name"))
        metrics = forward_metrics(
            trades,
            symbol=str(leg.get("symbol")) or None,
            since=str(leg.get("deployed_at") or "") or None,
            now_iso=utc_now_iso(),
        )
        kill = evaluate_predicates((leg.get("kill") or {}).get("rules"), metrics)
        graduate = evaluate_predicates(
            (leg.get("graduate") or {}).get("rules"), metrics
        )
        update_probation_leg(
            store,
            job_id,
            name,
            progress=(
                f"controller {now.date()}: kill={kill['status']} "
                f"graduate={graduate['status']} "
                f"trades={metrics['closed_trades']} pnl={metrics['net_pnl']}"
            ),
        )
        # Kill outranks graduate: if both fire, the leg dies — pre-registered
        # risk rules are senior to reward rules.
        decision = (
            ("killed", kill)
            if kill["status"] == "met"
            else ("graduated", graduate)
            if graduate["status"] == "met"
            else None
        )
        if decision is None:
            continue
        status, evaluation = decision
        update_probation_leg(store, job_id, name, status=status)
        store.append_journal(
            job_id,
            {
                "type": "lifecycle_decision",
                "leg": name,
                "decision": status,
                "metrics": metrics,
                "checks": evaluation["checks"],
            },
        )
        events.append(
            {"action": f"lifecycle_{status}", "leg": name, "metrics": metrics}
        )
    return events


def recover_stalled_applications(
    *, store: JobStore | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Scan every job for stalled proposal applications and recover them.

    Never raises: a broken job record must not stop recovery of the others.
    """
    store = store or JobStore()
    now = now or datetime.now(UTC)
    scanned = 0
    recovered: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    # A mechanical restage re-runs the full gate suite (a backtest) — bound
    # each watchdog pass to one so the pass stays well inside its timeout;
    # the 5-minute cadence picks up the rest.
    restaged_this_pass = False
    # A gate re-stamp runs a full backtest — same one-per-pass budget rule.
    restamped_this_pass = False
    for job in store.list_jobs():
        try:
            proposals = store.proposals(job.id)
        except Exception as exc:
            errors.append({"job_id": job.id, "error": str(exc)})
            continue
        for proposal in proposals:
            scanned += 1
            status = proposal["application"]["status"]
            try:
                if status == "applying":
                    event = _recover_applying(store, job.id, proposal, now)
                elif status == "queued":
                    event = _recover_queued(store, job.id, proposal, now)
                elif proposal["application"].get("restage_requested"):
                    event = _recover_restage(
                        store,
                        job.id,
                        proposal,
                        now,
                        allow_mechanical=not restaged_this_pass,
                    )
                    if event is not None and event.get("action") == (
                        "mechanical_restage"
                    ):
                        restaged_this_pass = True
                else:
                    event = None
            except Exception as exc:
                errors.append(
                    {
                        "job_id": job.id,
                        "proposal_id": proposal.get("proposal_id"),
                        "error": str(exc),
                    }
                )
                continue
            if event is not None:
                recovered.append({"job_id": job.id, **event})
        try:
            pause_event = _recover_orphaned_pause(store, job.id, proposals)
        except Exception as exc:
            errors.append({"job_id": job.id, "error": str(exc)})
            pause_event = None
        if pause_event is not None:
            recovered.append({"job_id": job.id, **pause_event})
        try:
            for successor_event in _check_successor_overdue(
                store, job.id, proposals, now
            ):
                recovered.append({"job_id": job.id, **successor_event})
        except Exception as exc:
            errors.append({"job_id": job.id, "error": f"successor: {exc}"})
        try:
            gate_event = _recover_stale_gate(
                store, job.id, proposals, allow_restamp=not restamped_this_pass
            )
        except Exception as exc:
            errors.append({"job_id": job.id, "error": str(exc)})
            gate_event = None
        if gate_event is not None:
            recovered.append({"job_id": job.id, **gate_event})
            if gate_event.get("action") == "gate_restamp":
                restamped_this_pass = True
        try:
            loop_gap_event = _check_loop_gap(store, job, now)
        except Exception as exc:
            errors.append({"job_id": job.id, "error": f"loop_gap: {exc}"})
            loop_gap_event = None
        if loop_gap_event is not None:
            recovered.append({"job_id": job.id, **loop_gap_event})
        try:
            for lifecycle_event in _run_lifecycle_pass(store, job.id, now):
                recovered.append({"job_id": job.id, **lifecycle_event})
        except Exception as exc:
            errors.append({"job_id": job.id, "error": f"lifecycle: {exc}"})
        try:
            audit_event = _audit_live_mode(store, job)
        except Exception as exc:  # noqa: BLE001
            errors.append({"job_id": job.id, "error": f"live_audit: {exc}"})
            audit_event = None
        if audit_event is not None:
            recovered.append({"job_id": job.id, **audit_event})
    try:
        _refresh_portfolio_report(store, now)
    except Exception as exc:  # noqa: BLE001 — fleet telemetry never blocks recovery
        errors.append({"job_id": "_portfolio", "error": str(exc)})
    return {"scanned": scanned, "recovered": recovered, "errors": errors}


_LIVE_AUDIT_PATH = "state/live_mode_audit.json"


def _audit_live_mode(store: JobStore, job: Any) -> dict[str, Any] | None:
    """Flag live-mode misconfiguration the guardrails grandfathered in.

    funding-carry-basket ran mode=live for five weeks with the engine-default
    wallet label — set before operator-owned mode existed, so nothing was
    stamped and nothing ever re-audited it. Two conditions, checked every
    pass, journaled only when the flag set CHANGES:
    - live with no owner stamp in state/operator.json (unattributed mode)
    - live with no explicit execution_params.wallet_label (engine default
      'main' rarely exists on a box — the job cannot actually trade)
    """
    loop = getattr(job, "script_loop", None)
    flags: list[str] = []
    if (
        loop is not None
        and getattr(loop, "enabled", False)
        and (str(getattr(loop, "mode", "") or "") == "live")
    ):
        operator = store.read_json(job.id, "state/operator.json") or {}
        set_by = ((operator.get("script_mode") or {}).get("set_by")) or None
        if set_by != "owner":
            flags.append("unstamped_live_mode")
        execution_params = dict(getattr(job, "execution_params", None) or {})
        if not str(execution_params.get("wallet_label") or "").strip():
            flags.append("live_wallet_label_missing")
    previous = store.read_json(job.id, _LIVE_AUDIT_PATH) or {}
    if sorted(previous.get("flags") or []) == sorted(flags):
        return None
    event: dict[str, Any] = {
        "type": "live_mode_audit",
        "flags": flags,
        "cleared": not flags,
    }
    # A clear that rides an operator stamp created AFTER the flag was raised
    # is exactly how an agent would launder the flag (observed live: the
    # worker ran `set-script-mode --by owner` to clear unstamped_live_mode).
    # The clear stands — the stamp may be genuine — but it carries a loud
    # review marker so the owner confirms they actually made it.
    if not flags and "unstamped_live_mode" in (previous.get("flags") or []):
        operator = store.read_json(job.id, "state/operator.json") or {}
        stamp_at = (operator.get("script_mode") or {}).get("set_at")
        flagged_at = previous.get("checked_at")
        if stamp_at and flagged_at and str(stamp_at) > str(flagged_at):
            event["owner_review_required"] = (
                f"cleared by an owner stamp created at {stamp_at}, AFTER the "
                f"flag was raised at {flagged_at} — confirm this stamp was "
                "actually made by the owner, not claimed by the agent"
            )
    store.write_json(
        job.id, _LIVE_AUDIT_PATH, {"flags": flags, "checked_at": utc_now_iso()}
    )
    store.append_journal(job.id, event)
    return {"action": "live_mode_audit", "flags": flags} if flags else None


_PORTFOLIO_REFRESH_S = 1800


def _refresh_portfolio_report(store: JobStore, now: datetime) -> None:
    """Fleet portfolio report on the watchdog cadence, rate-limited to
    ~30min: pure on-disk aggregation, but no reason to re-scan every book
    each 5-minute pass."""
    from wayfinder_paths.jobs.portfolio import (
        portfolio_report_path,
        write_portfolio_report,
    )

    path = portfolio_report_path(store)
    if path.exists():
        try:
            generated = json.loads(path.read_text(encoding="utf-8")).get("generated_at")
            stamp = datetime.fromisoformat(str(generated))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            if (now - stamp).total_seconds() < _PORTFOLIO_REFRESH_S:
                return
        except (ValueError, TypeError):
            pass
    write_portfolio_report(store)


def ensure_application_watchdog(
    *, store: JobStore, bridge: RunnerBridge | None = None
) -> dict[str, Any]:
    """Register the watchdog as a runner interval job (idempotent)."""
    bridge = bridge or RunnerBridge(repo_root=store.repo_root)
    store.runs_jobs_dir.mkdir(parents=True, exist_ok=True)
    driver: Path = store.runs_jobs_dir / "application_watchdog.py"
    driver.write_text(
        dedent(
            """
            from __future__ import annotations

            import json

            from wayfinder_paths.jobs.watchdog import recover_stalled_applications

            if __name__ == "__main__":
                print(json.dumps(recover_stalled_applications(), default=str))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    response = bridge.add_or_update_script_job(
        name=WATCHDOG_RUNNER_JOB_NAME,
        script_path=str(driver.relative_to(store.repo_root)),
        interval_seconds=WATCHDOG_INTERVAL_SECONDS,
        timeout_seconds=WATCHDOG_TIMEOUT_SECONDS,
        env={"WAYFINDER_WATCHDOG": "1"},
    )
    return {"runner_job_name": WATCHDOG_RUNNER_JOB_NAME, "response": response}
