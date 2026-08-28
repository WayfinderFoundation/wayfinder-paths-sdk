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
import shutil
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent
from typing import Any, TypedDict

from wayfinder_paths.jobs.application import complete_application
from wayfinder_paths.jobs.failures import cpu_steal_pct
from wayfinder_paths.jobs.lifecycle import lifecycle_sweep
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.runner_bridge import RunnerBridge
from wayfinder_paths.jobs.store import JobStore

WATCHDOG_RUNNER_JOB_NAME = "wayfinder-application-watchdog"
WATCHDOG_INTERVAL_SECONDS = 300
WATCHDOG_TIMEOUT_SECONDS = 2700

# Deterministic applies (candidate_report staged): completer child normally
# finishes in 1-3 minutes; a dead completer past this age is recovered.
DETERMINISTIC_APPLYING_TIMEOUT = timedelta(minutes=15)
# Orphaned deterministic applies (status "applying", stale started_at, no
# live completer process — daemon RSS-exit or box reboot mid-apply): a dead
# completer can never finish on its own, so waiting out the full applying
# window just prolongs the loop pause. Short grace covers the claim→spawn
# window where apply_worker.pid is not yet recorded.
ORPHANED_APPLYING_TIMEOUT = timedelta(minutes=5)
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
# None within this window → wake the agent once per entry ARM.
SUCCESSOR_OVERDUE_TIMEOUT = timedelta(hours=12)
_SUCCESSOR_EXPECTED_PATH = "state/successor_expected.json"
# A successor the agent itself rejected did NOT deliver the invitation — it
# re-arms the expectation (window restarts at the self-rejection). Bounded:
# past this many re-arms the thread is journaled as abandoned for the owner.
SUCCESSOR_MAX_REARMS = 3


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
    pid_alive = _pid_alive(pid)
    if pid_alive:
        if age < HARD_KILL_TIMEOUT:
            return None
        _kill_process_group(pid)

    deterministic = bool(proposal.get("candidate_report"))
    # Orphan signature: deterministic apply stuck "applying" with no live
    # completer (recorded pid dead, or claim never reached spawn — e.g. the
    # daemon RSS-exited mid-apply) past a short grace. Recovered NOW instead
    # of waiting out the full applying window — a dead completer can never
    # finish. Agent-owned applies record no pid, so the fast path never
    # applies to them. Recovery re-enters the apply from the TOP of the
    # completer pipeline: complete_application(status="applied") re-runs the
    # baseline-drift guard and the FULL candidate validation before
    # promoting — it never resumes a torn apply mid-stage.
    orphaned = deterministic and not pid_alive and age >= ORPHANED_APPLYING_TIMEOUT
    timeout = (
        DETERMINISTIC_APPLYING_TIMEOUT if deterministic else AGENT_APPLYING_TIMEOUT
    )
    if age < timeout and not orphaned:
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
        "orphaned": orphaned,
        "worker_pid": pid or None,
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


def _staged_after(proposal: dict[str, Any], entry_ts: str) -> bool:
    """Whether a proposal was staged after the rejection —
    candidate_report.generated_at is the propose/restage stamp; updated_at is
    the fallback for report-less proposals."""
    return (
        str(
            (proposal.get("candidate_report") or {}).get("generated_at")
            or proposal.get("updated_at")
            or ""
        )
        > entry_ts
    )


def _check_successor_overdue(
    store: JobStore,
    job_id: str,
    proposals: list[dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """Wake the agent when a process-rejection's expected successor is late.

    A kind=process owner rejection (superseded draft, re-stage mechanics) is
    an INVITATION, not a veto — the owner expects a corrected successor that
    PROCEEDS TO AUDIT. "Delivered" means: a successor proposal that is alive
    (or that the owner adjudicated), a staged experiment run, an opened
    probation leg, or a filed exhaustion claim. A successor the agent itself
    rejected delivered nothing — one such self-rejection silently terminated
    an owner-invited thread in production, because the old check counted ANY
    staged proposal and notified exactly once. Self-rejections now RE-ARM the
    expectation (bounded at SUCCESSOR_MAX_REARMS, then the thread is
    journaled `successor_abandoned` for owner review).
    """
    expected = store.read_json(job_id, _SUCCESSOR_EXPECTED_PATH) or []
    if not isinstance(expected, list):
        return []
    events: list[dict[str, Any]] = []
    changed = False
    for entry in expected:
        if (
            not isinstance(entry, dict)
            or entry.get("delivered")
            or entry.get("abandoned")
        ):
            continue
        entry_ts = str(entry.get("ts") or "")
        rejected_id = str(entry.get("proposal_id") or "")
        successors = [
            p
            for p in proposals
            if str(p.get("proposal_id")) != rejected_id and _staged_after(p, entry_ts)
        ]
        alive = [p for p in successors if p.get("status") != "rejected"]
        owner_closed = [
            p
            for p in successors
            if p.get("status") == "rejected"
            and str((p.get("rejection") or {}).get("by") or "")
            in {"owner", "user", "human"}
        ]
        audit_progress = _research_progress_since(store.job_dir(job_id), entry_ts)
        if (
            alive
            or owner_closed
            or audit_progress["valid_learning"]
            or audit_progress["activity"]
            or audit_progress["adjudicated"]
        ):
            entry["delivered"] = True
            entry["notified"] = True
            changed = True
            continue
        self_rejected = [
            p
            for p in successors
            if p.get("status") == "rejected"
            and str((p.get("rejection") or {}).get("by") or "") == "agent"
            and str(p.get("proposal_id")) not in (entry.get("rearmed_for") or [])
        ]
        if self_rejected:
            rearms = int(entry.get("rearms") or 0)
            if rearms >= SUCCESSOR_MAX_REARMS:
                entry["abandoned"] = True
                changed = True
                store.append_journal(
                    job_id,
                    {
                        "type": "successor_abandoned",
                        "proposal_id": rejected_id,
                        "rearms": rearms,
                        "owner_review_required": (
                            f"the successor invited by the process rejection of "
                            f"{rejected_id} was agent-self-rejected "
                            f"{rearms + 1} times — the agent cannot close this "
                            "thread itself; file a mechanically auditable "
                            "exhaustion claim or ask the owner to reject the ask)"
                        ),
                    },
                )
                events.append(
                    {
                        "action": "successor_abandoned",
                        "proposal_id": rejected_id,
                        "outcome": "owner_review_required",
                    }
                )
                continue
            newest = max(
                self_rejected,
                key=lambda p: str((p.get("rejection") or {}).get("ts") or ""),
            )
            rearm_ts = str((newest.get("rejection") or {}).get("ts") or now.isoformat())
            entry["ts"] = rearm_ts
            entry["notified"] = False
            entry["rearms"] = rearms + 1
            entry.setdefault("rearmed_for", []).extend(
                str(p.get("proposal_id")) for p in self_rejected
            )
            changed = True
            store.append_journal(
                job_id,
                {
                    "type": "successor_rearmed",
                    "proposal_id": rejected_id,
                    "self_rejected_successor": str(newest.get("proposal_id")),
                    "rearms": rearms + 1,
                },
            )
            events.append(
                {
                    "action": "successor_rearmed",
                    "proposal_id": rejected_id,
                    "outcome": "expectation_rearmed",
                }
            )
            continue
        if entry.get("notified"):
            continue
        if _age(now, entry.get("ts")) < SUCCESSOR_OVERDUE_TIMEOUT:
            continue
        entry["notified"] = True
        changed = True
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


# Disk-pressure alerting: the production box's 2GB /wf volume silently filled
# to 100% — boot rsync died half-way, opencode serve crash-looped, runnerd
# could not start, and live trading loops went dark ~25 minutes with ZERO
# alerting at 90/95/100%. The watchdog samples the filesystem holding the
# jobs repo root every pass and journals (and, for live jobs, wakes the
# agent) on breach — a full disk kills trading exactly like a stalled loop.
_DISK_ALERT_PATH = "state/disk_pressure_alert.json"
DISK_ALERT_PCT_ENV = "WAYFINDER_DISK_ALERT_PCT"
DEFAULT_DISK_ALERT_PCT = 85
# One journal entry per pressure episode: re-alert only after this window,
# or immediately when usage has climbed this much further.
DISK_REALERT_WINDOW = timedelta(hours=6)
DISK_REALERT_RISE_PCT = 5.0


def _check_disk_usage(
    store: JobStore, job: Any, now: datetime
) -> dict[str, Any] | None:
    usage = shutil.disk_usage(store.repo_root)
    if usage.total <= 0:
        return None
    pct_used = 100.0 * usage.used / usage.total
    threshold = int(os.environ.get(DISK_ALERT_PCT_ENV) or DEFAULT_DISK_ALERT_PCT)
    loop = getattr(job, "script_loop", None)
    mode = (
        str(getattr(loop, "mode", "") or "")
        if loop is not None and getattr(loop, "enabled", False)
        else ""
    )
    marker = store.read_json(job.id, _DISK_ALERT_PATH) or {}
    if pct_used >= threshold:
        alerted_pct = float(marker.get("pct_used") or 0.0)
        if (
            marker
            and _age(now, marker.get("alerted_at")) < DISK_REALERT_WINDOW
            and pct_used < alerted_pct + DISK_REALERT_RISE_PCT
        ):
            return None  # already alerted for this pressure episode
        store.write_json(
            job.id,
            _DISK_ALERT_PATH,
            {"pct_used": round(pct_used, 1), "alerted_at": now.isoformat()},
        )
        store.append_journal(
            job.id,
            {
                "type": "disk_pressure",
                "pct_used": round(pct_used, 1),
                "free_mb": usage.free // (1024 * 1024),
                "total_mb": usage.total // (1024 * 1024),
                "threshold_pct": threshold,
                "mode": mode,
            },
        )
        if mode == "live":
            # circular import: worker → application → … → triggers
            from wayfinder_paths.jobs.triggers import fire_triggers

            fire_triggers(store, job, ["disk_pressure"], source="watchdog")
        return {
            "action": "disk_pressure",
            "pct_used": round(pct_used, 1),
            "threshold_pct": threshold,
            "mode": mode,
        }
    if marker:
        store.write_json(job.id, _DISK_ALERT_PATH, {})
        store.append_journal(
            job.id,
            {
                "type": "disk_pressure_recovered",
                "pct_used": round(pct_used, 1),
                "threshold_pct": threshold,
                "mode": mode,
            },
        )
        return {"action": "disk_pressure_recovered", "mode": mode}
    return None


# Agent-mode drift alerting: `set_agent_mode` edits job.yaml and recompiles,
# which re-bakes WAYFINDER_JOB_AGENT_MODE into the runner env. A failed or
# skipped recompile leaves the runner waking under the OLD mode while
# job.yaml — and everything that reads it, including the synced
# agent_mode_actual — claims the new one: the agent-loop twin of the
# paper/live split-brain that once had a live-trading job reading "paper".
# The watchdog compares the baked env against job.yaml every pass and
# journals once per drift episode (re-alert window mirrors disk_pressure).
_AGENT_MODE_DRIFT_PATH = "state/agent_mode_drift_alert.json"
AGENT_MODE_DRIFT_REALERT_WINDOW = timedelta(hours=6)


def _check_agent_mode_drift(
    store: JobStore, job: Any, now: datetime, runner_states: dict[str, Any]
) -> dict[str, Any] | None:
    loop = getattr(job, "agent_loop", None)
    name = (
        str(getattr(loop, "runner_job_name", "") or "")
        if loop is not None and getattr(loop, "enabled", False)
        else ""
    )
    state = runner_states.get(name) if name else None
    env = ((state or {}).get("payload") or {}).get("env") or {}
    compiled = str(env.get("WAYFINDER_JOB_AGENT_MODE") or "")
    if not compiled:
        # Daemon down or loop not compiled: nothing to compare — hold the
        # marker so a transient outage neither re-alerts nor fake-"recovers".
        return None
    declared = str(getattr(loop, "mode", "") or "")
    marker = store.read_json(job.id, _AGENT_MODE_DRIFT_PATH) or {}
    if compiled != declared:
        if (
            marker.get("compiled") == compiled
            and marker.get("declared") == declared
            and _age(now, marker.get("alerted_at")) < AGENT_MODE_DRIFT_REALERT_WINDOW
        ):
            return None  # already alerted for this drift episode
        store.write_json(
            job.id,
            _AGENT_MODE_DRIFT_PATH,
            {
                "declared": declared,
                "compiled": compiled,
                "alerted_at": now.isoformat(),
            },
        )
        store.append_journal(
            job.id,
            {"type": "agent_mode_drift", "declared": declared, "compiled": compiled},
        )
        return {
            "action": "agent_mode_drift",
            "declared": declared,
            "compiled": compiled,
        }
    if marker:
        store.write_json(job.id, _AGENT_MODE_DRIFT_PATH, {})
        store.append_journal(
            job.id, {"type": "agent_mode_drift_recovered", "mode": declared}
        )
        return {"action": "agent_mode_drift_recovered", "mode": declared}
    return None


# Research-impasse alerting: all three production research jobs froze the
# same way — staleness computed correctly and the wake mandate fired every
# wake, but the mandate's "state why research is not warranted" hatch let the
# agent close every stale wake with prose ("all sub-lanes settled on merit",
# verbatim across wakes) while staging nothing. Staleness was never a
# watchdog trigger, so nothing escalated. This check is the escalation: when
# a research job is stale AND the last K wakes produced zero progress
# artifacts (experiments, probation outcomes, staged proposals, exhaustion
# claims), journal `research_impasse`, write the marker the worker prompt
# renders as a HATCH-STRIPPED mandate, and fire a trigger wake. Debounced by
# a re-alert window (mirrors disk_pressure); the marker clears — and journals
# `research_impasse_resolved` — only when valid learning or a trusted
# owner/coverage adjudication appears. Activity remains visible without
# laundering it into learning.
_IMPASSE_PATH = "state/research_impasse.json"
IMPASSE_WAKES_ENV = "WAYFINDER_IMPASSE_WAKES"
DEFAULT_IMPASSE_WAKES = 3
IMPASSE_REALERT_WINDOW = timedelta(hours=24)
IMPASSE_ADJUDICATION_WINDOW = timedelta(hours=48)
_LEARNING_JOURNAL_TYPES = {
    "probation_leg_graduated",
    "probation_leg_killed",
}
_ACTIVITY_JOURNAL_TYPES = {
    "probation_leg_opened",
    "paper_probation_opened",
    "exhaustion_claim_filed",
}
_OWNER_ADJUDICATION_TYPES = {
    "exhaustion_claim_accepted",
    "exhaustion_claim_rejected",
}
_AUDIT_ADJUDICATION_TYPES = {"exhaustion_claim_audit_passed"}


class ResearchProgressClassification(TypedDict):
    advanced: bool
    valid_learning: bool
    learning_signals: list[str]
    activity: bool
    activity_signals: list[str]
    adjudicated: bool
    adjudication_signals: list[str]


def _journal_rows(root: Path, *, max_lines: int = 20_000) -> list[dict[str, Any]]:
    path = root / "journal.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-max_lines:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _research_progress_since(
    root: Path,
    since_ts: str,
    *,
    rows: list[dict[str, Any]] | None = None,
    proposals: list[dict[str, Any]] | None = None,
) -> ResearchProgressClassification:
    """Classify post-boundary research artifacts as learning or activity.

    Valid learning is a previously unseen semantic experiment or a matured
    probation outcome. Opening work, staging a proposal, filing a claim, and
    repeating an experiment are activity only. Owner claim adjudication is a
    separate resolution path. Prose never counts.
    """
    learning: set[str] = set()
    activity: set[str] = set()
    adjudications: set[str] = set()
    for row in rows if rows is not None else _journal_rows(root):
        if str(row.get("ts") or "") <= since_ts:
            continue
        event_type = str(row.get("type") or "")
        if event_type in _LEARNING_JOURNAL_TYPES:
            learning.add(event_type)
        elif event_type in _ACTIVITY_JOURNAL_TYPES:
            activity.add(event_type)
        elif event_type in _OWNER_ADJUDICATION_TYPES and str(row.get("by") or "") in {
            "owner",
            "user",
            "human",
        }:
            adjudications.add(event_type)
        elif (
            event_type in _AUDIT_ADJUDICATION_TYPES
            and str(row.get("by") or "") == "coverage-audit"
        ):
            adjudications.add(event_type)

    from wayfinder_paths.jobs.execution.experiments import experiment_semantic_hash

    seen_hashes: set[str] = set()
    experiments_path = root / "results" / "backtest" / "experiments.jsonl"
    if experiments_path.exists():
        for line in experiments_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                experiment = json.loads(line)
            except ValueError:
                continue
            if not isinstance(experiment, dict):
                continue
            semantic_hash = experiment_semantic_hash(experiment)
            if str(experiment.get("ts") or "") <= since_ts:
                seen_hashes.add(semantic_hash)
                continue
            if semantic_hash in seen_hashes:
                activity.add("duplicate_experiment")
            else:
                learning.add("experiment_run")
                seen_hashes.add(semantic_hash)

    for proposal in proposals or []:
        if not _staged_after(proposal, since_ts):
            continue
        if (
            proposal.get("status") == "rejected"
            and str((proposal.get("rejection") or {}).get("by") or "") == "agent"
        ):
            continue  # a self-rejected staging delivered nothing
        activity.add("staged_proposal")
    return {
        # Compatibility for existing mechanical consumers: "advanced" now
        # has the strict meaning the name promised.
        "advanced": bool(learning),
        "valid_learning": bool(learning),
        "learning_signals": sorted(learning),
        "activity": bool(activity),
        "activity_signals": sorted(activity),
        "adjudicated": bool(adjudications),
        "adjudication_signals": sorted(adjudications),
    }


def _check_research_impasse(
    store: JobStore,
    job: Any,
    proposals: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    loop = getattr(job, "agent_loop", None)
    if (
        loop is None
        or not getattr(loop, "enabled", False)
        or str(getattr(loop, "mode", "") or "off") == "off"
    ):
        return None
    root = store.job_dir(job.id)
    rows = _journal_rows(root)
    wake_ts = [
        str(row.get("ts") or "") for row in rows if row.get("type") == "agent_wakeup"
    ]
    stale_wakes = int(os.environ.get(IMPASSE_WAKES_ENV) or DEFAULT_IMPASSE_WAKES)
    marker = store.read_json(job.id, _IMPASSE_PATH) or {}
    if len(wake_ts) < stale_wakes:
        return None  # startup — not enough wakes to judge
    basis_ts = sorted(wake_ts)[-stale_wakes]
    progress = _research_progress_since(root, basis_ts, rows=rows, proposals=proposals)
    mandated_work = marker.get("status") == "mandated_work"
    # An adjudication created a carryover mandate; that same event cannot
    # erase the named work on the next pass. Only learning may resolve it.
    if progress["valid_learning"] or (progress["adjudicated"] and not mandated_work):
        adjudication_signals = [] if mandated_work else progress["adjudication_signals"]
        if marker.get("alerted_at"):
            store.write_json(job.id, _IMPASSE_PATH, {})
            store.append_journal(
                job.id,
                {
                    "type": "research_impasse_resolved",
                    "learning_signals": progress["learning_signals"],
                    "adjudication_signals": adjudication_signals,
                },
            )
            return {
                "action": "research_impasse_resolved",
                "learning_signals": progress["learning_signals"],
                "adjudication_signals": adjudication_signals,
            }
        return None
    if mandated_work:
        if _age(now, marker.get("alerted_at")) < IMPASSE_REALERT_WINDOW:
            return None
        marker["alerted_at"] = now.isoformat()
        store.write_json(job.id, _IMPASSE_PATH, marker)
        mandate = marker.get("mandate") or {}
        store.append_journal(
            job.id,
            {
                "type": "research_impasse",
                "status": "mandated_work",
                "claim_id": mandate.get("claim_id"),
                "required_next_experiments": mandate.get("required_next_experiments")
                or [],
            },
        )
        from wayfinder_paths.jobs.triggers import fire_triggers

        fire_triggers(store, job, ["research_impasse"], source="watchdog")
        return {
            "action": "research_impasse",
            "status": "mandated_work",
            "claim_id": mandate.get("claim_id"),
            "outcome": "agent_woken",
        }
    if marker.get("status") == "awaiting_adjudication":
        if _age(now, marker.get("awaiting_since")) < IMPASSE_ADJUDICATION_WINDOW:
            return None
        # The 48-hour adjudication window expired. Do not re-consume the old
        # filing as fresh activity; fall through to the normal impasse re-fire.
    elif "exhaustion_claim_filed" in progress["activity_signals"]:
        filed = [
            row
            for row in rows
            if row.get("type") == "exhaustion_claim_filed"
            and str(row.get("ts") or "") > basis_ts
        ]
        filed_at = max(str(row.get("ts") or "") for row in filed)
        awaiting = {
            **marker,
            "status": "awaiting_adjudication",
            "awaiting_since": filed_at,
            "claim_ids": sorted(
                {str(row.get("claim_id")) for row in filed if row.get("claim_id")}
            ),
        }
        if not marker.get("alerted_at"):
            awaiting["alerted_at"] = now.isoformat()
        store.write_json(job.id, _IMPASSE_PATH, awaiting)
        if marker.get("status") != "awaiting_adjudication":
            store.append_journal(
                job.id,
                {
                    "type": "research_impasse_awaiting_adjudication",
                    "claim_ids": awaiting["claim_ids"],
                },
            )
        return {
            "action": "research_impasse_awaiting_adjudication",
            "claim_ids": awaiting["claim_ids"],
        }
    from wayfinder_paths.jobs.evolution_ledger import research_staleness_report

    staleness = research_staleness_report(store, job.id)
    if not staleness.get("stale"):
        return None  # quiet but within policy thresholds — not an impasse
    if marker.get("alerted_at") and (
        _age(now, marker.get("alerted_at")) < IMPASSE_REALERT_WINDOW
    ):
        return None  # already alerted for this episode
    store.write_json(
        job.id,
        _IMPASSE_PATH,
        {
            "alerted_at": now.isoformat(),
            "basis_wake_ts": basis_ts,
            "stale_wakes": stale_wakes,
        },
    )
    store.append_journal(
        job.id,
        {
            "type": "research_impasse",
            "stale_wakes": stale_wakes,
            "basis_wake_ts": basis_ts,
            "days_since_last_experiment": staleness.get("days_since_last_experiment"),
            "wakes_since_last_proposal": staleness.get("wakes_since_last_proposal"),
        },
    )
    # circular import: worker → application → … → triggers
    from wayfinder_paths.jobs.triggers import fire_triggers

    fire_triggers(store, job, ["research_impasse"], source="watchdog")
    return {
        "action": "research_impasse",
        "stale_wakes": stale_wakes,
        "outcome": "agent_woken",
    }


# Pending claims are controller work: every watchdog pass runs the structured
# coverage audit and applies pass/narrow/reject without waiting on an owner.
def _check_decision_gates(
    store: JobStore, job: Any, now: datetime
) -> list[dict[str, Any]]:
    """Pre-registered rework-vs-retire gates: paper jobs auto-resolve when
    their criteria are met; live-capital jobs trip to owner_attention."""
    from wayfinder_paths.jobs.decision_gates import evaluate_decision_gates

    return evaluate_decision_gates(store, job, now=now)


def _audit_pending_claims(store: JobStore, job_id: str) -> dict[str, Any] | None:
    from wayfinder_paths.jobs.exhaustion import (
        audit_and_adjudicate_exhaustion_claim,
        list_exhaustion_claims,
    )

    pending = list_exhaustion_claims(store, job_id, status="pending")
    if not pending:
        return None
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for claim in pending:
        claim_id = str(claim["claim_id"])
        try:
            results.append(
                audit_and_adjudicate_exhaustion_claim(store, job_id, claim_id)
            )
        except Exception as exc:  # noqa: BLE001 — other claims must still audit
            failures.append({"claim_id": claim_id, "error": str(exc)})
    return {
        "action": "exhaustion_claims_audited",
        "count": len(results),
        "failures": failures,
        "verdicts": [
            {
                "claim_id": claim["claim_id"],
                "status": claim["status"],
                "audit_verdict": (claim.get("audit") or {}).get("verdict"),
            }
            for claim in results
        ],
    }


# Lifecycle evaluation is cheap (json reads + arithmetic) but its decisions
# are consequential — a 6h cadence matches the counterfactual cycle and keeps
# kill/graduate flips out of the 5-minute noise floor.
_LIFECYCLE_MARKER = "state/lifecycle_pass.json"
_LIFECYCLE_INTERVAL_S = 6 * 3600
_CAMPAIGN_FINALIZE_RETRY_S = (0, 5 * 60, 15 * 60, 30 * 60, 60 * 60)
_CAMPAIGN_FINALIZE_GRACE = timedelta(minutes=60)
_CAMPAIGN_FINALIZE_MAX_AGE = timedelta(hours=24)
# A finalize op still writing output past the grace is extended, not expired:
# expiry once fired mid-Optuna at exactly the grace boundary and the orphaned
# finalize ran 8+ hours holding the replication lock. Freshness = op log
# mtime — the simulator's per-bar progress lines land there continuously
# during finalize backtests.
_FINALIZE_LOG_FRESH = timedelta(minutes=10)
_FINALIZE_EXTEND_MARKER = "state/evolution_finalize_extend.json"
_BURST_STATE_PATH = "/tmp/wayfinder-burst-governor.json"
_HEAVY_OP_REGISTRY_DIR = "/tmp/wayfinder-heavy-ops"
_BURST_STATE_MAX_AGE_S = 10.0


def _op_governor_paused(job_dir: Path, op: str, now: datetime) -> bool:
    """True only when fresh governor state names this op or its heavy child."""
    from wayfinder_paths.jobs.execution.op_process import (
        proc_parent_pid,
        proc_start_ticks,
    )

    try:
        status = json.loads(
            (job_dir / "state" / "background_ops" / f"{op}.json").read_text(
                encoding="utf-8"
            )
        )
        op_pid = status.get("pid")
        if not isinstance(op_pid, int) or status.get("state") != "running":
            return False
        state_path = Path(
            os.environ.get("WAYFINDER_BURST_STATE_PATH", _BURST_STATE_PATH)
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        age = max(0.0, now.timestamp() - float(state["updated_at"]))
        affected = state.get("affected_pids")
        if (
            age > _BURST_STATE_MAX_AGE_S
            or not bool(state.get("paused"))
            or not isinstance(affected, list)
        ):
            return False
        if op_pid in affected:
            return True
        registry_dir = Path(
            os.environ.get("WAYFINDER_HEAVY_OP_REGISTRY_DIR", _HEAVY_OP_REGISTRY_DIR)
        )
        for path in registry_dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                continue
            child_pid = record.get("child_pid") if isinstance(record, dict) else None
            if (
                isinstance(record, dict)
                and record.get("supervisor_pid") == op_pid
                and record.get("supervisor_start_ticks") == proc_start_ticks(op_pid)
                and isinstance(child_pid, int)
                and child_pid in affected
                and record.get("child_start_ticks") == proc_start_ticks(child_pid)
                and proc_parent_pid(child_pid) == op_pid
            ):
                return True
    except (OSError, TypeError, ValueError, KeyError):
        return False
    return False


def _finalize_op_health(job_dir: Path, now: datetime) -> tuple[int | None, bool]:
    """(pid, healthy) for the evolution_finalize detached op: healthy means
    the recorded pid is alive AND the op log was written recently."""
    ops_dir = job_dir / "state" / "background_ops"
    try:
        status = json.loads(
            (ops_dir / "evolution_finalize.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None, False
    pid = status.get("pid") if isinstance(status, dict) else None
    if not isinstance(pid, int) or not _pid_alive(pid):
        return pid if isinstance(pid, int) else None, False
    try:
        log_age = now.timestamp() - (ops_dir / "evolution_finalize.log").stat().st_mtime
    except OSError:
        return pid, _op_governor_paused(job_dir, "evolution_finalize", now)
    return pid, log_age < _FINALIZE_LOG_FRESH.total_seconds() or _op_governor_paused(
        job_dir, "evolution_finalize", now
    )


def _reap_finalize_group(pid: int | None) -> bool:
    """SIGKILL the finalize op's process group; pgid == pid because
    spawn_detached_op starts the op with start_new_session=True. Signaling
    the recorded pid AS a pgid also reaps surviving children when the session
    leader already exited (os.getpgid would fail there). True when a live
    group was killed."""
    if not pid or pid <= 0:
        return False
    try:
        os.killpg(pid, 0)
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        return False
    return True


def _expire_evolution_campaign(
    store: JobStore, job_id: str, now: datetime
) -> dict[str, Any] | None:
    """Enforce one hard terminal horizon across every campaign stage."""
    from wayfinder_paths.jobs.compute_lock import job_state_lock
    from wayfinder_paths.jobs.evolution_campaign import CAMPAIGN_STATE_PATH
    from wayfinder_paths.jobs.execution.op_process import terminate_campaign_ops
    from wayfinder_paths.jobs.worker import retire_evolution_session

    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = store.read_json(job_id, CAMPAIGN_STATE_PATH, default={}) or {}
        if state.get("status") not in {"active", "finalizing"}:
            return None
        attempts = int(state.get("finalize_attempts") or 0)
        state.update(
            {
                "status": "failed",
                "stage": "failed",
                "failed_at": now.isoformat(),
                "failure_reason": "campaign did not reach a terminal verdict within 24 hours",
            }
        )
        store.write_json(job_id, CAMPAIGN_STATE_PATH, state)
        store.append_journal(
            job_id,
            {
                "type": "evolution_campaign_failed",
                "campaign_id": state.get("campaign_id"),
                "finalize_attempts": attempts,
            },
        )
    campaign_id = str(state.get("campaign_id") or "")
    reaped = terminate_campaign_ops(store, job_id, campaign_id)
    retire_evolution_session(store, job_id, campaign_id)
    result: dict[str, Any] = {
        "action": "evolution_campaign_failed",
        "campaign_id": state.get("campaign_id"),
    }
    if reaped:
        result["reaped_ops"] = reaped
    return result


def _run_evolution_campaign_pass(
    store: JobStore, job_id: str, now: datetime
) -> dict[str, Any] | None:
    """Close deadline-bound campaigns independently of agent wake delivery."""
    from wayfinder_paths.jobs.background import op_status_summary, spawn_detached_op
    from wayfinder_paths.jobs.compute_lock import job_state_lock
    from wayfinder_paths.jobs.evolution_campaign import (
        CAMPAIGN_DRAIN,
        CAMPAIGN_STATE_PATH,
        recover_lost_candidate_evaluations,
    )
    from wayfinder_paths.jobs.execution.op_process import terminate_campaign_ops
    from wayfinder_paths.jobs.worker import (
        recover_evolution_stage_session,
        retire_evolution_session,
    )

    state = store.read_json(job_id, CAMPAIGN_STATE_PATH, default={}) or {}
    if state.get("status") not in {"active", "finalizing"}:
        if state.get("status") in {"complete", "expired", "failed"} and state.get(
            "campaign_id"
        ):
            terminate_campaign_ops(store, job_id, str(state["campaign_id"]))
            retired = retire_evolution_session(store, job_id, str(state["campaign_id"]))
            if (
                retired
                and retired.get("retired")
                and not retired.get("already_retired")
            ):
                return {
                    "action": "evolution_session_retired",
                    "campaign_id": state.get("campaign_id"),
                }
        return None
    try:
        deadline = datetime.fromisoformat(str(state["deadline_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    if now - deadline >= _CAMPAIGN_FINALIZE_MAX_AGE:
        return _expire_evolution_campaign(store, job_id, now)
    evaluating = op_status_summary(store.job_dir(job_id), "evolution_evaluate")
    if (evaluating or {}).get("status") == "running":
        return {
            "action": "evolution_campaign_waiting_for_evaluation",
            "campaign_id": state.get("campaign_id"),
            "governor_paused": _op_governor_paused(
                store.job_dir(job_id), "evolution_evaluate", now
            ),
        }
    recovered = recover_lost_candidate_evaluations(
        store,
        job_id,
        reason=(
            "detached evaluator is no longer running"
            if evaluating is None
            else f"detached evaluator status is {evaluating.get('status')}"
        ),
        now=now,
    )
    if recovered:
        return {
            "action": "evolution_candidate_evaluation_recovered",
            "campaign_id": state.get("campaign_id"),
            "candidate_ids": recovered,
        }
    state = store.read_json(job_id, CAMPAIGN_STATE_PATH, default={}) or {}
    pending_evaluation = [
        candidate
        for candidate in state.get("candidates") or []
        if candidate.get("status") in {"prepared", "quick_failed"}
    ]
    if now < deadline and state.get("status") == "active":
        if now < deadline - CAMPAIGN_DRAIN or pending_evaluation:
            recovery = recover_evolution_stage_session(store, job_id, now=now)
            if recovery:
                return {
                    "action": "evolution_stage_session_recovered",
                    "campaign_id": state.get("campaign_id"),
                    **recovery,
                }
            if pending_evaluation:
                return {
                    "action": "evolution_campaign_waiting_for_candidate_stage",
                    "campaign_id": state.get("campaign_id"),
                    "candidate_id": pending_evaluation[0].get("candidate_id"),
                }
            return None
        with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
            state = store.read_json(job_id, CAMPAIGN_STATE_PATH, default={}) or {}
            if state.get("status") != "active":
                return None
            if not state.get("drain_started_at"):
                state["stage"] = "draining"
                state["drain_started_at"] = now.isoformat()
                store.write_json(job_id, CAMPAIGN_STATE_PATH, state)
                store.append_journal(
                    job_id,
                    {
                        "type": "evolution_campaign_draining",
                        "campaign_id": state.get("campaign_id"),
                    },
                )
        retired = retire_evolution_session(
            store, job_id, str(state.get("campaign_id") or "")
        )
        return {
            "action": "evolution_campaign_draining",
            "campaign_id": state.get("campaign_id"),
            "session_retired": bool(retired and retired.get("retired")),
        }

    retirement = retire_evolution_session(
        store, job_id, str(state.get("campaign_id") or "")
    )
    if retirement and not retirement.get("retired"):
        return {
            "action": "evolution_campaign_waiting_for_session_retirement",
            "campaign_id": state.get("campaign_id"),
            "error": retirement.get("error"),
        }

    if pending_evaluation:
        candidate_id = str(pending_evaluation[0].get("candidate_id") or "")
        spawned = spawn_detached_op(
            store,
            job_id,
            "evolution_evaluate",
            {"job_id": job_id, "candidate_id": candidate_id},
        )
        return {
            "action": "evolution_candidate_evaluation_started",
            "campaign_id": state.get("campaign_id"),
            "candidate_id": candidate_id,
            "pid": spawned.get("pid"),
        }

    op = op_status_summary(store.job_dir(job_id), "evolution_finalize")
    past_grace = now - deadline >= _CAMPAIGN_FINALIZE_GRACE
    if past_grace:
        # A healthy finalizer may outlive the four-hour generation window.
        # A stale process is reaped and relaunched from its durable candidate
        # claims instead of expiring the whole campaign at the old 60m wall.
        pid, healthy = _finalize_op_health(store.job_dir(job_id), now)
        if healthy:
            marker = store.read_json(job_id, _FINALIZE_EXTEND_MARKER) or {}
            if marker.get("campaign_id") == state.get("campaign_id"):
                return None
            store.write_json(
                job_id,
                _FINALIZE_EXTEND_MARKER,
                {
                    "campaign_id": state.get("campaign_id"),
                    "pid": pid,
                    "noted_at": now.isoformat(),
                },
            )
            store.append_journal(
                job_id,
                {
                    "type": "evolution_finalize_still_running",
                    "campaign_id": state.get("campaign_id"),
                    "pid": pid,
                },
            )
            return {
                "action": "evolution_finalize_still_running",
                "campaign_id": state.get("campaign_id"),
                "pid": pid,
            }
        if _reap_finalize_group(pid):
            store.append_journal(
                job_id,
                {
                    "type": "evolution_finalize_reaped",
                    "campaign_id": state.get("campaign_id"),
                    "pid": pid,
                },
            )
    elif state.get("status") == "finalizing" and (op or {}).get("status") == "running":
        return None

    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = store.read_json(job_id, CAMPAIGN_STATE_PATH, default={}) or {}
        if state.get("status") not in {"active", "finalizing"}:
            return None
        attempts = int(state.get("finalize_attempts") or 0)
        last_attempt = state.get("finalize_last_attempt_at")
        if attempts and _age(now, last_attempt) < timedelta(
            seconds=_CAMPAIGN_FINALIZE_RETRY_S[
                min(attempts, len(_CAMPAIGN_FINALIZE_RETRY_S) - 1)
            ]
        ):
            return None
        state.update(
            {
                "status": "finalizing",
                "stage": "finalizing",
                "finalize_attempts": attempts + 1,
                "finalize_last_attempt_at": now.isoformat(),
            }
        )
        store.write_json(job_id, CAMPAIGN_STATE_PATH, state)

    spawned = spawn_detached_op(store, job_id, "evolution_finalize", {"job_id": job_id})
    return {
        "action": "evolution_campaign_finalize_started",
        "campaign_id": state.get("campaign_id"),
        "attempt": attempts + 1,
        "pid": spawned.get("pid"),
    }


def _run_paper_experiment_pass(
    store: JobStore, job_id: str, now: datetime
) -> dict[str, Any] | None:
    from wayfinder_paths.jobs.paper_experiment import (
        harvest_hourly_control_candidates,
        maybe_adjudicate_proposals,
        maybe_finalize_experiment,
    )

    adjudicated = maybe_adjudicate_proposals(store, job_id, now=now)
    if adjudicated:
        return {
            "action": "evolution_proposal_adjudicated",
            "outcomes": adjudicated,
        }
    verdict = maybe_finalize_experiment(store, job_id, now=now)
    if verdict is not None:
        return {"action": "evolution_experiment_completed", **verdict}
    staged = harvest_hourly_control_candidates(store, job_id, now=now)
    if staged is not None:
        return {
            "action": "evolution_control_proposal_staged",
            "candidate_id": staged.get("candidate_id"),
            "revision": staged.get("revision"),
        }
    return None


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
    from wayfinder_paths.jobs.improver.spec import ImproverSpec

    spec = ImproverSpec.load(store.job_dir(job_id))
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
        # Paper-tier legs carry a mechanical no-strategy-baseline floor on
        # top of their registered kill rules: a leg that underperforms
        # flat-zero over its window is retired regardless of what rules the
        # agent registered — a forced-entry tier without a retirement floor
        # turns a stall fix into a junk flood.
        paper_floor: dict[str, Any] | None = None
        if (
            leg.get("tier") == "paper"
            and int(metrics["closed_trades"]) >= spec.paper_floor_min_trades
            and float(metrics["net_pnl"]) < 0
        ):
            paper_floor = {
                "rule": "paper_flat_zero_floor",
                "net_pnl": metrics["net_pnl"],
                "closed_trades": metrics["closed_trades"],
                "min_trades": spec.paper_floor_min_trades,
            }
        # Kill outranks graduate: if both fire, the leg dies — pre-registered
        # risk rules are senior to reward rules.
        decision = (
            ("killed", kill)
            if kill["status"] == "met"
            else ("killed", {"checks": [paper_floor]})
            if paper_floor is not None
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
    # One daemon RPC per pass, shared by the per-job agent-mode drift check.
    # job_states() itself returns {} on an unreachable daemon; the guard is
    # for everything else — a broken bridge must not stop recovery of the
    # jobs, and the drift check degrades to a no-op exactly like a down
    # daemon (no compare, marker held).
    try:
        runner_states = RunnerBridge(repo_root=store.repo_root).job_states()
    except Exception:  # noqa: BLE001
        runner_states = {}
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
            disk_event = _check_disk_usage(store, job, now)
        except Exception as exc:
            errors.append({"job_id": job.id, "error": f"disk: {exc}"})
            disk_event = None
        if disk_event is not None:
            recovered.append({"job_id": job.id, **disk_event})
        try:
            drift_event = _check_agent_mode_drift(store, job, now, runner_states)
        except Exception as exc:
            errors.append({"job_id": job.id, "error": f"agent_mode: {exc}"})
            drift_event = None
        if drift_event is not None:
            recovered.append({"job_id": job.id, **drift_event})
        try:
            impasse_event = _check_research_impasse(store, job, proposals, now)
        except Exception as exc:
            errors.append({"job_id": job.id, "error": f"impasse: {exc}"})
            impasse_event = None
        if impasse_event is not None:
            recovered.append({"job_id": job.id, **impasse_event})
        try:
            claims_event = _audit_pending_claims(store, job.id)
        except Exception as exc:
            errors.append({"job_id": job.id, "error": f"claims: {exc}"})
            claims_event = None
        if claims_event is not None:
            recovered.append({"job_id": job.id, **claims_event})
        try:
            campaign_event = _run_evolution_campaign_pass(store, job.id, now)
        except Exception as exc:  # noqa: BLE001
            errors.append({"job_id": job.id, "error": f"evolution: {exc}"})
            campaign_event = None
        if campaign_event is not None:
            recovered.append({"job_id": job.id, **campaign_event})
        try:
            experiment_event = _run_paper_experiment_pass(store, job.id, now)
        except Exception as exc:  # noqa: BLE001
            errors.append({"job_id": job.id, "error": f"paper_experiment: {exc}"})
            experiment_event = None
        if experiment_event is not None:
            recovered.append({"job_id": job.id, **experiment_event})
        try:
            for lifecycle_event in _run_lifecycle_pass(store, job.id, now):
                recovered.append({"job_id": job.id, **lifecycle_event})
        except Exception as exc:
            errors.append({"job_id": job.id, "error": f"lifecycle: {exc}"})
        try:
            for gate_event in _check_decision_gates(store, job, now):
                recovered.append({"job_id": job.id, **gate_event})
        except Exception as exc:  # noqa: BLE001
            errors.append({"job_id": job.id, "error": f"decision_gates: {exc}"})
        try:
            audit_event = _audit_live_mode(store, job)
        except Exception as exc:  # noqa: BLE001
            errors.append({"job_id": job.id, "error": f"live_audit: {exc}"})
            audit_event = None
        if audit_event is not None:
            recovered.append({"job_id": job.id, **audit_event})
    # Fleet lifecycle sweep rides the watchdog tick so bootstrap/park policy
    # fires even when a rotting job's own loops never run. Internally daily-
    # throttled; the 5-minute cadence just keeps the clock honest.
    try:
        sweep = lifecycle_sweep(store, now=now)
        recovered.extend(dict(action) for action in sweep.get("actions") or [])
        errors.extend(sweep.get("errors") or [])
    except Exception as exc:  # noqa: BLE001 — lifecycle never blocks recovery
        errors.append({"job_id": "_lifecycle", "error": str(exc)})
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
