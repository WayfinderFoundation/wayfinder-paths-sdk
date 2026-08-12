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

import os
import re
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent
from typing import Any

from wayfinder_paths.jobs.application import complete_application
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
        # circular import: proposals → worker → …driver → triggers → watchdog
        from wayfinder_paths.jobs.proposals import restage_proposal

        try:
            result = restage_proposal(store, job_id, proposal_id)
        except Exception as exc:
            store.append_journal(
                job_id,
                {
                    "type": "application_watchdog_skipped",
                    "proposal_id": proposal_id,
                    "reason": f"mechanical restage failed: {exc}",
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


# Matches "backtest is for revision ab12cd34ef56, workspace is 0f1e2d3c4b5a" —
# the gate reason shape for pure stamp staleness (evidence fine, fingerprint
# old). Substantive failures (validation failed, contract, missing dataset)
# do NOT match and are never auto-repaired.
_REVISION_MISMATCH_RE = re.compile(
    r"is for revision [0-9a-f]{6,}, workspace is [0-9a-f]{6,}"
)
_GATE_RESTAMP_MARKER = "state/gate_restamp.json"


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
    return {"scanned": scanned, "recovered": recovered, "errors": errors}


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
