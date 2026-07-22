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
