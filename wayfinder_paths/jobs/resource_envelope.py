"""Small, durable resource envelope for memory-heavy evolution phases."""

from __future__ import annotations

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
    max_steal = float(os.environ.get("WAYFINDER_EVOLUTION_MAX_STEAL_PCT", "90"))
    available = snapshot.get("mem_available_mb")
    steal = snapshot.get("cpu_steal_pct")
    reasons = []
    if isinstance(available, (int, float)) and available < min_available:
        reasons.append(f"MemAvailable {available:.0f}MB < {min_available:.0f}MB")
    if isinstance(steal, (int, float)) and steal > max_steal:
        reasons.append(f"CPU steal {steal:.1f}% > {max_steal:.1f}%")
    if reasons:
        raise TransientInfrastructureError(
            "evolution resource guard deferred heavy compute: " + "; ".join(reasons)
        )
    return snapshot


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
