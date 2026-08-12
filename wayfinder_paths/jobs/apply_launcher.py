"""Deterministic proposal application.

An approved proposal must never be able to stall its job: `claim_application`
pauses both runner loops, so whatever completes the application has to be
guaranteed to run. This module owns that guarantee — the approval path claims
in-process (fast) and spawns a detached completer child that runs the full
deterministic validation via `complete_application(status="applied")`, which
promotes on pass and rolls back + resumes loops on fail. No LLM session sits
in the critical path; the application watchdog backstops a dead completer.

Only proposals carrying a propose-time `candidate_report` take this path —
their candidate is already staged and validated, so completion is pure
mechanics. Ungated proposals (CLI `--allow-ungated`) have no staged change and
still need an agent wake to author the candidate; that wake claims for itself
and is watchdog-bounded.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any

from wayfinder_paths.jobs.application import claim_application, complete_application
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore


def _apply_worker_cmd(job_id: str, proposal_id: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "wayfinder_paths.jobs.apply_launcher",
        job_id,
        proposal_id,
    ]


def _default_spawn(*, job_id: str, proposal_id: str, store: JobStore) -> int:
    from wayfinder_paths.runner.lifecycle import spawn_detached

    log_path = store.job_dir(job_id) / "applications" / proposal_id / "apply.log"
    return spawn_detached(
        cmd=_apply_worker_cmd(job_id, proposal_id),
        repo_root=store.repo_root,
        log_path=log_path,
        banner="[apply-worker]",
        env=os.environ.copy(),
    )


def start_application(
    store: JobStore,
    job_id: str,
    proposal_id: str,
    *,
    spawn: Callable[..., int] | None = None,
) -> dict[str, Any]:
    """Claim the application and hand completion to a detached worker.

    A spawn failure is completed as failed synchronously — every exit from
    this function leaves the runner loops either running or owned by a live
    completer process.
    """
    claim = claim_application(store, job_id, proposal_id)
    spawn_fn = spawn or _default_spawn
    try:
        pid = spawn_fn(job_id=job_id, proposal_id=proposal_id, store=store)
    except Exception as exc:
        failed = complete_application(
            store,
            job_id,
            proposal_id,
            status="failed",
            error=f"apply worker spawn failed: {exc}",
        )
        return {"mode": "deterministic", "spawned": False, **failed}

    proposal = store.load_proposal(job_id, proposal_id)
    proposal["application"]["apply_worker"] = {
        "pid": int(pid),
        "spawned_at": utc_now_iso(),
        "log": str(store.job_dir(job_id) / "applications" / proposal_id / "apply.log"),
    }
    store.write_proposal(job_id, proposal)
    store.append_journal(
        job_id,
        {
            "type": "application_apply_spawned",
            "proposal_id": proposal_id,
            "pid": int(pid),
        },
    )
    return {
        "mode": "deterministic",
        "spawned": True,
        "proposal": proposal,
        "apply_worker": proposal["application"]["apply_worker"],
        "paused_runner_jobs": claim.get("paused_runner_jobs"),
    }


def launch_application(
    store: JobStore, job_id: str, proposal_id: str
) -> dict[str, Any]:
    """Route an approved proposal to its apply path.

    candidate_report present → deterministic claim + detached completion.
    Absent (ungated/legacy) → agent wake; the agent claims for itself, so a
    dropped wake leaves the application queued with loops still running.
    """
    proposal = store.load_proposal(job_id, proposal_id)
    if proposal.get("candidate_report"):
        return start_application(store, job_id, proposal_id)
    from wayfinder_paths.jobs.worker import run_job_worker

    wakeup = run_job_worker(job_id, mode="intervene", apply_proposal_id=proposal_id)
    return {"mode": "agent_wake", "wakeup": wakeup}


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m wayfinder_paths.jobs.apply_launcher <job> <proposal>")
        return 2
    job_id, proposal_id = argv
    store = JobStore()
    try:
        result = complete_application(store, job_id, proposal_id, status="applied")
    except ValueError as exc:
        # Lost the race to the watchdog or a manual completion — the
        # application is already terminal, which is the outcome we wanted.
        print(f"[apply-worker] already completed: {exc}")
        return 0
    status = (result.get("proposal") or {}).get("application", {}).get("status")
    print(f"[apply-worker] completed {proposal_id}: {status}")
    return 0 if status == "applied" else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
