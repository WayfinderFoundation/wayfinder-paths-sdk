"""Process contract shared by isolated job-operation launchers and reapers."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.runner.monitor_state import atomic_write_json

if TYPE_CHECKING:
    from wayfinder_paths.jobs.store import JobStore

_RUNNER_MODULE = "wayfinder_paths.jobs.execution.op_runner"
_CONTROL_PLANE_OPS = frozenset(
    {
        "evolution_start",
        "evolution_design",
        "evolution_compose",
        "evolution_prepare",
        "evolution_submit_seed",
    }
)
_CAMPAIGN_OWNED_OPS = frozenset(
    {
        "evolution_design",
        "evolution_compose",
        "evolution_prepare",
        "evolution_evaluate",
        "evolution_finalize",
    }
)


def operation_resource_tier(op: str) -> str:
    """Classify runner work for the image-level CPU burst governor."""
    return "control" if op in _CONTROL_PLANE_OPS else "heavy"


def op_runner_command(op: str) -> list[str]:
    """Return the canonical, process-visible command for an isolated op."""
    return [
        sys.executable,
        "-m",
        _RUNNER_MODULE,
        f"--op-name={op}",
        f"--resource-tier={operation_resource_tier(op)}",
    ]


def proc_start_ticks(pid: int) -> int | None:
    try:
        fields = (
            Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(") ", 1)[1]
        )
        return int(fields.split()[19])
    except (OSError, IndexError, ValueError):
        return None


def proc_parent_pid(pid: int) -> int | None:
    try:
        fields = (
            Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(") ", 1)[1]
        )
        return int(fields.split()[1])
    except (OSError, IndexError, ValueError):
        return None


def process_identity_fields(pid: int) -> dict[str, Any]:
    """Stable identity fields for a detached process status record."""
    fields: dict[str, Any] = {}
    start_ticks = proc_start_ticks(pid)
    if start_ticks is not None:
        fields["process_start_ticks"] = start_ticks
    try:
        boot_id = (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        )
    except OSError:
        boot_id = ""
    if boot_id:
        fields["boot_id"] = boot_id
    return fields


def _linux_booted_at() -> datetime | None:
    try:
        uptime_s = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, IndexError, ValueError):
        return None
    return datetime.fromtimestamp(time.time() - uptime_s, tz=UTC)


def recorded_process_alive(record: dict[str, Any]) -> bool:
    """Validate a recorded pid without trusting reuse across machine boots."""
    pid = record.get("pid")
    if not isinstance(pid, int) or not _pid_alive(pid):
        return False
    current = process_identity_fields(pid)
    recorded_boot = record.get("boot_id")
    if recorded_boot and current.get("boot_id") != recorded_boot:
        return False
    recorded_ticks = record.get("process_start_ticks")
    if isinstance(recorded_ticks, int):
        return current.get("process_start_ticks") == recorded_ticks
    if recorded_boot:
        return True

    # Backward compatibility for status files written before identity fields
    # existed. A process recorded before this Linux boot cannot still own the
    # pid, even if the new machine has already reused that number.
    try:
        started_at = datetime.fromisoformat(str(record["started_at"]))
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        booted_at = _linux_booted_at()
        if booted_at is not None and started_at < booted_at:
            return False
    except (KeyError, TypeError, ValueError):
        pass
    return True


def pid_is_op_runner(pid: int) -> bool:
    try:
        parts = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
    except OSError:
        return False
    return _RUNNER_MODULE.encode() in parts


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _pid_matches_runner(pid: int, op: str, start_ticks: int) -> bool:
    try:
        parts = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
    except OSError:
        return False
    return (
        proc_start_ticks(pid) == start_ticks
        and b"wayfinder_paths.jobs.execution.op_runner" in parts
        and f"--op-name={op}".encode() in parts
    )


@contextmanager
def track_evolution_process(op: str, kwargs: dict[str, Any]) -> Iterator[None]:
    """Register campaign-owned children so expiry can reap them exactly."""
    job_id = str(kwargs.get("job_id") or "").strip()
    if not job_id or op not in _CAMPAIGN_OWNED_OPS:
        yield
        return

    from wayfinder_paths.jobs.evolution_campaign import campaign_status
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore()
    campaign_id = str(campaign_status(store, job_id).get("campaign_id") or "").strip()
    process_start_ticks = proc_start_ticks(os.getpid())
    if not campaign_id or process_start_ticks is None:
        yield
        return

    registry_dir = store.job_dir(job_id) / "state" / "running_ops"
    registry_dir.mkdir(parents=True, exist_ok=True)
    path = registry_dir / f"{os.getpid()}.json"
    record = {
        "schema_version": "1.0",
        "pid": os.getpid(),
        "process_group": os.getpgrp(),
        "process_start_ticks": process_start_ticks,
        "op": op,
        "job_id": job_id,
        "campaign_id": campaign_id,
        "resource_tier": operation_resource_tier(op),
        "started_at": utc_now_iso(),
    }
    atomic_write_json(path, record)
    try:
        yield
    finally:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = None
        if isinstance(current, dict) and current.get("pid") == os.getpid():
            path.unlink(missing_ok=True)


def terminate_campaign_ops(
    store: JobStore, job_id: str, campaign_id: str
) -> list[dict[str, Any]]:
    """SIGKILL only registered children owned by the closing campaign."""
    registry_dir = store.job_dir(job_id) / "state" / "running_ops"
    reaped: list[dict[str, Any]] = []
    for path in sorted(registry_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or str(record.get("campaign_id")) != str(
            campaign_id
        ):
            continue
        pid = record.get("pid")
        op = str(record.get("op") or "")
        process_start_ticks = record.get("process_start_ticks")
        if (
            not isinstance(pid, int)
            or not _pid_alive(pid)
            or not op
            or not isinstance(process_start_ticks, int)
            or not _pid_matches_runner(pid, op, process_start_ticks)
        ):
            path.unlink(missing_ok=True)
            continue
        process_group = record.get("process_group")
        try:
            if (
                isinstance(process_group, int)
                and process_group == pid
                and os.getpgid(pid) == process_group
            ):
                os.killpg(process_group, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
        reaped.append(
            {
                "pid": pid,
                "op": op,
                "resource_tier": record.get("resource_tier"),
            }
        )
        path.unlink(missing_ok=True)
        status_path = registry_dir.parent / "background_ops" / f"{op}.json"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = None
        if (
            isinstance(status, dict)
            and status.get("state") == "running"
            and status.get("pid") == pid
        ):
            status.update(
                {
                    "state": "killed",
                    "exit_code": -signal.SIGKILL,
                    "finished_at": utc_now_iso(),
                    "reason": "owning evolution campaign closed",
                }
            )
            atomic_write_json(status_path, status)
    return reaped
