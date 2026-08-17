"""Detached job ops from synchronous (non-MCP) call sites.

The MCP server has its own async spawn (+ in-process reaper) in
mcp/tools/jobs.py; this is the CLI/sync counterpart writing the same
status-file shape, so `core_jobs(action="op_status")` polls either. No
reaper here — op_status already resolves reaper-less children (dead pid +
parseable result file = done)."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore


def _pid_alive(pid: Any) -> bool:
    import os

    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def spawn_detached_op(
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
            [sys.executable, "-m", "wayfinder_paths.jobs.execution.op_runner"],
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
    status_path.write_text(json.dumps(status), encoding="utf-8")
    return {"started": True, **status}
