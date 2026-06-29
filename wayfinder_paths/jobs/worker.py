from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from wayfinder_paths.core.clients.OpenCodeClient import OPENCODE_CLIENT
from wayfinder_paths.jobs.models import JOB_WORKER_AGENT_NAME, AgentMode, utc_now_iso
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import snapshot_job, sync_all_jobs

JOB_RESULT_MARKER = "WAYFINDER_JOB_RESULT "


def _read_text(path: Path, *, max_chars: int = 12_000) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _build_worker_prompt(
    *,
    store: JobStore,
    job_id: str,
    mode: str,
    snapshot: dict[str, Any],
) -> str:
    root = store.job_dir(job_id)
    memory_md = _read_text(root / "memory.md", max_chars=6000)
    recent_journal = _read_text(root / "journal.jsonl", max_chars=4000)
    return (
        "Run a Wayfinder job worker wakeup.\n\n"
        f"Mode: {mode}\n"
        "Rules:\n"
        "- Never execute live trades or fund-moving actions.\n"
        "- Monitor mode is read-only except reports/memory.\n"
        "- Improve mode may create candidate proposals under the job bundle, but cannot activate them.\n"
        "- Always write/return a compact structured finding.\n\n"
        "Job snapshot:\n"
        f"{json.dumps(snapshot, indent=2, default=str)[:12000]}\n\n"
        "Memory:\n"
        f"{memory_md}\n\n"
        "Recent journal:\n"
        f"{recent_journal}\n"
    )


def run_job_worker(job_id: str, mode: str = "monitor") -> dict[str, Any]:
    store = JobStore()
    job = store.load(job_id)
    mode = mode if mode in {"monitor", "improve", "decide"} else job.agent_loop.mode
    if mode == "off":
        mode = "monitor"
    mode_typed: AgentMode = mode  # type: ignore[assignment]

    snapshot = snapshot_job(job.id, store=store)
    prompt = _build_worker_prompt(store=store, job_id=job.id, mode=mode_typed, snapshot=snapshot)
    report_dir = store.job_dir(job.id) / "reports" / mode_typed
    report_dir.mkdir(parents=True, exist_ok=True)

    session_id = _ensure_worker_session(job.id, mode_typed)
    queued = False
    error: str | None = None
    if session_id:
        queued = OPENCODE_CLIENT.prompt_async(
            session_id=session_id,
            text=prompt,
            agent=JOB_WORKER_AGENT_NAME,
        )
        if not queued:
            error = "OpenCode prompt_async failed"
    else:
        error = "OpenCode server unavailable"

    status = "green" if queued else "yellow"
    report = {
        "job_id": job.id,
        "mode": mode_typed,
        "status": status,
        "summary": (
            f"{mode_typed} review queued in OpenCode session {session_id}"
            if queued
            else "Worker could not queue an OpenCode review"
        ),
        "session_id": session_id,
        "queued": queued,
        "error": error,
        "created_at": utc_now_iso(),
    }
    (report_dir / "latest.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    store.refresh_scorecard(
        job.id,
        {
            "health": status,
            "last_agent_check_at": report["created_at"],
            "last_agent_mode": mode_typed,
            "last_agent_summary": report["summary"],
        },
    )
    store.append_journal(job.id, {"type": "agent_wakeup", "mode": mode_typed, "report": report})

    try:
        sync_all_jobs(store=store)
    except Exception:
        logger.opt(exception=True).debug("Wayfinder job sync failed after worker wakeup")

    if status != "green":
        print(JOB_RESULT_MARKER + json.dumps({"type": "job_result", "severity": "warning", "summary": report["summary"], "job_id": job.id}))
    return report


def _ensure_worker_session(job_id: str, mode: str) -> str | None:
    if not OPENCODE_CLIENT.healthy():
        return None

    controller_session_id = os.environ.get("OPENCODE_SESSION_ID") or os.environ.get("OPENCODE_SESSIONID")
    try:
        existing = OPENCODE_CLIENT.find_child_session(
            parent_id=controller_session_id,
            title=f"job/{job_id}/{mode}",
        )
        if existing:
            return existing
        return OPENCODE_CLIENT.create_session(
            parent_id=controller_session_id,
            title=f"job/{job_id}/{mode}",
            agent=JOB_WORKER_AGENT_NAME,
        )
    except Exception:
        logger.opt(exception=True).debug("Failed to create/find OpenCode job worker session")
        return None
