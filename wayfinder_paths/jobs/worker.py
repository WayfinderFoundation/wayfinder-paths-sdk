from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from wayfinder_paths.core.clients.OpenCodeClient import OPENCODE_CLIENT
from wayfinder_paths.jobs.models import (
    JOB_AUTO_WORKER_AGENT_NAME,
    JOB_WORKER_AGENT_NAME,
    AgentMode,
    normalize_agent_mode,
    utc_now_iso,
)
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
        "- Monitor mode is read-only except reports/memory.\n"
        "- Intervene mode may create candidate proposals under the job bundle, but cannot activate them.\n"
        "- Auto mode may execute live trades only inside the configured auto_limits.\n"
        "- Never move funds, send onchain transactions, or execute contracts.\n"
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
    mode = normalize_agent_mode(mode) if mode else job.agent_loop.mode
    if mode == "off":
        mode = "monitor"
    mode_typed: AgentMode = mode

    snapshot = snapshot_job(job.id, store=store)

    blocked_reason = (
        _auto_limits_error(job.agent_loop.auto_limits)
        if mode_typed == "auto"
        else None
    )
    if blocked_reason:
        report = _write_report(
            store=store,
            job_id=job.id,
            mode=mode_typed,
            status="red",
            summary=f"Auto agent blocked: {blocked_reason}",
            session_id=None,
            queued=False,
            error=blocked_reason,
        )
        print(
            JOB_RESULT_MARKER
            + json.dumps(
                {
                    "type": "job_result",
                    "severity": "warning",
                    "summary": report["summary"],
                    "job_id": job.id,
                }
            )
        )
        return report

    prompt = _build_worker_prompt(
        store=store,
        job_id=job.id,
        mode=mode_typed,
        snapshot=snapshot,
    )

    session_id = _ensure_worker_session(job.id, mode_typed)
    queued = False
    error: str | None = None
    if session_id:
        queued = OPENCODE_CLIENT.prompt_async(
            session_id=session_id,
            text=prompt,
            agent=_agent_name_for_mode(mode_typed),
        )
        if not queued:
            error = "OpenCode prompt_async failed"
    else:
        error = "OpenCode server unavailable"

    report = _write_report(
        store=store,
        job_id=job.id,
        mode=mode_typed,
        status="green" if queued else "yellow",
        summary=(
            f"{mode_typed} wakeup queued in OpenCode session {session_id}"
            if queued
            else "Worker could not queue an OpenCode wakeup"
        ),
        session_id=session_id,
        queued=queued,
        error=error,
    )

    if report["status"] != "green":
        print(
            JOB_RESULT_MARKER
            + json.dumps(
                {
                    "type": "job_result",
                    "severity": "warning",
                    "summary": report["summary"],
                    "job_id": job.id,
                }
            )
        )
    return report


def _ensure_worker_session(job_id: str, mode: str) -> str | None:
    if not OPENCODE_CLIENT.healthy():
        return None

    controller_session_id = os.environ.get("OPENCODE_SESSION_ID") or os.environ.get(
        "OPENCODE_SESSIONID"
    )
    agent_name = _agent_name_for_mode(mode)
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
            agent=agent_name,
        )
    except Exception:
        logger.opt(exception=True).debug("Failed to create/find OpenCode job worker session")
        return None


def _write_report(
    *,
    store: JobStore,
    job_id: str,
    mode: AgentMode,
    status: str,
    summary: str,
    session_id: str | None,
    queued: bool,
    error: str | None,
) -> dict[str, Any]:
    report_dir = store.job_dir(job_id) / "reports" / mode
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "job_id": job_id,
        "mode": mode,
        "status": status,
        "summary": summary,
        "session_id": session_id,
        "queued": queued,
        "error": error,
        "created_at": utc_now_iso(),
    }
    (report_dir / "latest.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    store.refresh_scorecard(
        job_id,
        {
            "health": status,
            "last_agent_check_at": report["created_at"],
            "last_agent_mode": mode,
            "last_agent_summary": report["summary"],
        },
    )
    store.append_journal(job_id, {"type": "agent_wakeup", "mode": mode, "report": report})

    try:
        sync_all_jobs(store=store)
    except Exception:
        logger.opt(exception=True).debug("Wayfinder job sync failed after worker wakeup")
    return report


def _agent_name_for_mode(mode: str) -> str:
    return JOB_AUTO_WORKER_AGENT_NAME if mode == "auto" else JOB_WORKER_AGENT_NAME


def _auto_limits_error(limits: dict[str, Any] | None) -> str | None:
    data = dict(limits or {})
    venues = [str(v).strip() for v in data.get("enabled_venues") or [] if str(v).strip()]
    symbols = [str(v).strip() for v in data.get("allowed_symbols") or [] if str(v).strip()]
    markets = [str(v).strip() for v in data.get("allowed_markets") or [] if str(v).strip()]
    if not venues:
        return "enabled_venues must include at least one venue"
    if not symbols and not markets:
        return "allowed_symbols or allowed_markets must include at least one tradable target"
    for key in (
        "max_notional_per_decision",
        "max_daily_notional",
        "max_open_positions",
        "max_open_orders",
    ):
        try:
            value = float(data.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            return f"{key} must be greater than 0"
    return None
