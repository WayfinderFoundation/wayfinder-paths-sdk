"""Small, durable resource envelope for memory-heavy evolution phases."""

from __future__ import annotations

import json
import os
import resource
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.compute_lock import job_state_lock
from wayfinder_paths.jobs.failures import TransientInfrastructureError, cpu_steal_pct
from wayfinder_paths.jobs.store import JobStore

RESOURCE_REPORT_PATH = "reports/evolution/resources.json"
BURST_STATE_PATH = "/tmp/wayfinder-burst-governor.json"
BURST_STATE_MAX_AGE_SECONDS = 10.0


def resource_snapshot(*, sample_cpu: bool = False) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "pid": os.getpid(),
        "rss_mb": _proc_value_mb("/proc/self/status", "VmRSS:"),
        "peak_rss_mb": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2
        ),
        "mem_available_mb": _proc_value_mb("/proc/meminfo", "MemAvailable:"),
        "cgroup_memory_mb": _integer_file_mb("/sys/fs/cgroup/memory.current"),
        "cgroup_limit_mb": _integer_file_mb("/sys/fs/cgroup/memory.max"),
    }
    if sample_cpu:
        snapshot["cpu_steal_pct"] = cpu_steal_pct()
    return snapshot


def require_evolution_headroom() -> dict[str, Any]:
    """Defer, rather than reject evidence, when the shared box is saturated."""
    snapshot = resource_snapshot(sample_cpu=True)
    min_available = float(
        os.environ.get("WAYFINDER_EVOLUTION_MIN_AVAILABLE_MB", "1100")
    )
    available = snapshot.get("mem_available_mb")
    reasons = []
    if isinstance(available, (int, float)) and available < min_available:
        reasons.append(f"MemAvailable {available:.0f}MB < {min_available:.0f}MB")
    if reasons:
        raise TransientInfrastructureError(
            "evolution resource guard deferred heavy compute: " + "; ".join(reasons)
        )
    return snapshot


def evolution_launch_readiness(*, now: datetime | None = None) -> dict[str, Any]:
    """Read the image governor's bounded handoff without requiring Fly APIs."""
    path = Path(os.environ.get("WAYFINDER_BURST_STATE_PATH", BURST_STATE_PATH))
    if not path.exists():
        return {"ready": True, "source": "governor_unavailable"}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        updated_at = datetime.fromtimestamp(float(state["updated_at"]), tz=UTC)
    except (OSError, TypeError, ValueError, KeyError) as exc:
        return {
            "ready": False,
            "source": "governor_invalid",
            "reason": f"burst governor state is unreadable: {str(exc)[:160]}",
        }
    age = max(0.0, ((now or datetime.now(UTC)) - updated_at).total_seconds())
    if age > BURST_STATE_MAX_AGE_SECONDS:
        return {
            "ready": False,
            "source": "governor_stale",
            "age_seconds": round(age, 1),
            "reason": f"burst governor state is stale ({age:.1f}s)",
        }
    ready = bool(state.get("allow_new_heavy")) and not bool(state.get("paused"))
    result = {
        "ready": ready,
        "source": "governor",
        "balance_pct": state.get("balance_pct"),
        "balance_cpu_seconds": state.get("balance_cpu_seconds"),
        "capacity_cpu_seconds": state.get("capacity_cpu_seconds"),
        "budget_source": state.get("source"),
        "paused": bool(state.get("paused")),
        "age_seconds": round(age, 1),
    }
    if not ready:
        result["reason"] = "CPU burst reserve is below the campaign launch threshold"
    return result


def require_evolution_launch_headroom() -> dict[str, Any]:
    readiness = evolution_launch_readiness()
    if not readiness["ready"]:
        raise TransientInfrastructureError(str(readiness["reason"]))
    return readiness


@contextmanager
def evolution_resource_phase(
    store: JobStore, job_id: str, *, phase: str, candidate_id: str
) -> Iterator[None]:
    started = resource_snapshot()
    try:
        yield
    finally:
        finished = resource_snapshot()
        event = {
            "phase": phase,
            "candidate_id": candidate_id,
            "started": started,
            "finished": finished,
        }
        with job_state_lock(store.repo_root, job_id, name="evolution_resources"):
            report = store.read_json(job_id, RESOURCE_REPORT_PATH, default={}) or {}
            events = list(report.get("events") or [])[-99:]
            events.append(event)
            store.write_json(
                job_id,
                RESOURCE_REPORT_PATH,
                {"schema_version": "1.0", "latest": event, "events": events},
            )


def _proc_value_mb(path: str, prefix: str) -> float | None:
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.startswith(prefix):
                return round(float(line.split()[1]) / 1024, 2)
    except (OSError, ValueError, IndexError):
        pass
    return None


def _integer_file_mb(path: str) -> float | None:
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
        if raw == "max":
            return None
        return round(int(raw) / (1024 * 1024), 2)
    except (OSError, ValueError):
        return None
