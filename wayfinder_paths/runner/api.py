from __future__ import annotations

from typing import Any


def dispatch(daemon, *, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a control-plane method to the daemon implementation.

    This module is intentionally transport-agnostic (Unix socket today; HTTP later).
    """

    if method == "status":
        return daemon.ctl_status()
    if method == "shutdown":
        return daemon.ctl_shutdown()
    if method == "job_runs":
        return daemon.ctl_job_runs(
            name=params.get("name"),
            limit=params.get("limit"),
        )
    if method == "run_report":
        return daemon.ctl_run_report(
            run_id=params.get("run_id"),
            tail_bytes=params.get("tail_bytes"),
        )
    if method == "add_job":
        return daemon.ctl_add_job(
            name=params.get("name"),
            job_type=params.get("type"),
            payload=params.get("payload") or {},
            interval_seconds=params.get("interval_seconds"),
        )
    if method == "update_job":
        return daemon.ctl_update_job(
            name=params.get("name"),
            payload=params.get("payload"),
            interval_seconds=params.get("interval_seconds"),
        )
    if method == "pause_job":
        return daemon.ctl_pause_job(name=params.get("name"))
    if method == "resume_job":
        return daemon.ctl_resume_job(name=params.get("name"))
    if method == "run_once":
        return daemon.ctl_run_once(name=params.get("name"))
    if method == "delete_job":
        return daemon.ctl_delete_job(name=params.get("name"))

    return {"ok": False, "error": f"unknown_method: {method}"}
