"""Detached job ops from synchronous (non-MCP) call sites.

The MCP server has its own async spawn (+ in-process reaper) in
mcp/tools/jobs.py; this is the CLI/sync counterpart writing the same
status-file shape, so `core_jobs(action="op_status")` polls either. No
reaper here — op_status already resolves reaper-less children (dead pid +
parseable result file = done)."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.compute_lock import job_state_lock
from wayfinder_paths.jobs.execution.op_process import op_runner_command
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.monitor_state import atomic_write_json


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def op_running(job_dir: Path, op: str) -> bool:
    """True when `op` has a live detached child recorded for the job at
    `job_dir` — a `running` status file whose pid is still alive. A stale
    status file from a dead child does not count."""
    status_path = job_dir / "state" / "background_ops" / f"{op}.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(status, dict)
        and status.get("state") == "running"
        and _pid_alive(status.get("pid"))
    )


def op_status_summary(job_dir: Path, op: str) -> dict[str, Any] | None:
    """Compact snapshot view of a detached op: status collapsed to
    running/done/failed plus timestamps. None when no run is recorded.

    Read-only (safe from the fork-per-request view server). A `running`
    status whose pid is dead is resolved the way op_status does — a
    parseable result file means the detached child finished anyway,
    otherwise the run is lost and reported failed."""
    ops_dir = job_dir / "state" / "background_ops"
    try:
        status = json.loads((ops_dir / f"{op}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(status, dict):
        return None
    state = status.get("state")
    stale_running = state == "running" and not _pid_alive(status.get("pid"))
    reconciled_failure = state == "failed" and status.get("error") == (
        "detached operation exited without a result"
    )
    if stale_running or reconciled_failure:
        try:
            json.loads((ops_dir / f"{op}.result.json").read_text(encoding="utf-8"))
            state = "done"
        except (OSError, ValueError):
            state = "failed"
        status["state"] = state
        status.setdefault("reconciled_at", datetime.now(UTC).isoformat())
        if state == "failed":
            status.setdefault("error", "detached operation exited without a result")
        else:
            status.pop("error", None)
        atomic_write_json(ops_dir / f"{op}.json", status)
    if state not in ("running", "done"):
        state = "failed"
    summary: dict[str, Any] = {"status": state}
    for key in ("started_at", "finished_at"):
        if status.get(key):
            summary[key] = status[key]
    return summary


def spawn_detached_op(
    store: JobStore, job_id: str, op: str, kwargs: dict[str, Any]
) -> dict[str, Any]:
    with job_state_lock(store.repo_root, job_id, name=f"background_{op}"):
        return _spawn_detached_op(store, job_id, op, kwargs)


def _spawn_detached_op(
    store: JobStore, job_id: str, op: str, kwargs: dict[str, Any]
) -> dict[str, Any]:
    ops_dir = store.job_dir(job_id) / "state" / "background_ops"
    ops_dir.mkdir(parents=True, exist_ok=True)
    status_path = ops_dir / f"{op}.json"
    try:
        existing = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = None
    if (
        isinstance(existing, dict)
        and existing.get("state") == "running"
        and _pid_alive(existing.get("pid"))
    ):
        return {"already_running": True, **existing}

    log_path = ops_dir / f"{op}.log"
    result_path = ops_dir / f"{op}.result.json"
    result_path.unlink(missing_ok=True)
    with log_path.open("wb") as log_handle, result_path.open("wb") as result_handle:
        proc = subprocess.Popen(
            op_runner_command(op),
            stdin=subprocess.PIPE,
            stdout=result_handle,
            stderr=log_handle,
            start_new_session=True,
        )
    assert proc.stdin is not None
    proc.stdin.write(json.dumps({"op": op, "kwargs": kwargs}).encode())
    proc.stdin.close()
    status = {
        "op": op,
        "job_id": job_id,
        "state": "running",
        "pid": proc.pid,
        "started_at": utc_now_iso(),
    }
    atomic_write_json(status_path, status)
    return {"started": True, **status}
