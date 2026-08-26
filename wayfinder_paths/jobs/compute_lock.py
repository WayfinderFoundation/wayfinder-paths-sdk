"""Machine-wide heavy-compute lock: one big simulation at a time.

Measured on the 2GB boxes: a single 120d backtest peaks at ~336MB and runs
~28s — it fits comfortably. Every OOM incident (runnerd deaths 2026-07-27
and 2026-07-31; composition proposals blocked 2026-08-02 after three
attempts) came from heavy computations OVERLAPPING: a wake-path replication
run colliding with a candidate-validation backtest colliding with a scan.
Serializing the heavies machine-wide turns a fatal burst into a few seconds
of queueing.

Cross-process via flock on `.wayfinder/compute.lock` under the repo root;
reentrant within a process (replication -> backtest_execution_job must not
self-deadlock). On timeout the caller gets a clear TimeoutError — agents
see "another heavy computation is running, retry" instead of a silent
OOM-kill, and the wake-path monitors already degrade gracefully on
exceptions.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LOCK_RELATIVE = ".wayfinder/compute.lock"
DEFAULT_TIMEOUT_S = 600.0
_POLL_S = 2.0
_local = threading.local()


class ComputeLockBusy(TimeoutError):
    pass


def _lock_path(repo_root: Path | None) -> Path:
    if repo_root is None:
        from wayfinder_paths.jobs.store import JobStore

        repo_root = JobStore().repo_root
    return Path(repo_root) / LOCK_RELATIVE


@contextmanager
def _exclusive_file_lock(path: Path, *, label: str, timeout_s: float) -> Iterator[None]:
    held_paths: set[str] = getattr(_local, "held_paths", set())
    key = str(path.resolve())
    if key in held_paths:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # "a+" — opening must NOT truncate: a contender that fails to acquire
    # reads the current holder's pid/label out of this file for its error.
    handle = path.open("a+")
    deadline = time.monotonic() + float(timeout_s)
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    holder = ""
                    try:
                        holder = path.read_text(encoding="utf-8").strip()
                    except OSError:
                        pass
                    raise ComputeLockBusy(
                        f"{label or 'exclusive'} lock busy after "
                        f"{timeout_s:.0f}s (held by: {holder or 'unknown'}) — "
                        "another operation is using this resource; "
                        "retry when it finishes"
                    ) from None
                time.sleep(_POLL_S)
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} label={label}\n")
        handle.flush()
        _local.held_paths = {*held_paths, key}
        try:
            yield
        finally:
            _local.held_paths = held_paths
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def heavy_compute_lock(
    *,
    repo_root: Path | None = None,
    label: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Iterator[None]:
    """Serialize memory-heavy work across every job on this machine."""
    with _exclusive_file_lock(_lock_path(repo_root), label=label, timeout_s=timeout_s):
        previously_held = getattr(_local, "held", False)
        _local.held = True
        try:
            yield
        finally:
            _local.held = previously_held


@contextmanager
def job_state_lock(
    repo_root: Path,
    job_id: str,
    *,
    name: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Iterator[None]:
    """Serialize a named read-modify-write state funnel for one job."""
    from wayfinder_paths.jobs.models import safe_job_id

    path = (
        Path(repo_root)
        / ".wayfinder"
        / "jobs"
        / safe_job_id(job_id)
        / "state"
        / f"{name}.lock"
    )
    with _exclusive_file_lock(
        path,
        label=f"{name}:{safe_job_id(job_id)}",
        timeout_s=timeout_s,
    ):
        yield


@contextmanager
def experiment_compute_lock(
    store: Any,
    job_id: str,
    *,
    label: str,
    duty_fraction: float = 0.20,
    window_hours: float = 12.0,
) -> Iterator[None]:
    """Serialize evolution work and enforce its rolling compute-duty budget."""
    relative = "state/evolution_compute_budget.json"
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=window_hours)
    with job_state_lock(store.repo_root, job_id, name="evolution_compute_budget"):
        budget = store.read_json(job_id, relative, default={}) or {}
        events = [
            event
            for event in budget.get("events") or []
            if _event_time(event) >= cutoff
        ]
        used = sum(float(event.get("wall_seconds") or 0.0) for event in events)
        limit = float(duty_fraction) * float(window_hours) * 3600.0
        if used >= limit:
            raise ComputeLockBusy(
                f"evolution compute duty exhausted ({used:.1f}/{limit:.1f}s "
                f"over {window_hours:g}h); retry in the next budget window"
            )
        with heavy_compute_lock(repo_root=store.repo_root, label=label):
            started = time.monotonic()
            try:
                yield
            finally:
                elapsed = max(0.0, time.monotonic() - started)
                current = datetime.now(UTC)
                cutoff = current - timedelta(hours=window_hours)
                events = [
                    event
                    for event in budget.get("events") or []
                    if _event_time(event) >= cutoff
                ]
                events.append(
                    {
                        "ts": current.isoformat(),
                        "label": label,
                        "wall_seconds": round(elapsed, 3),
                    }
                )
                store.write_json(
                    job_id,
                    relative,
                    {
                        "window_hours": window_hours,
                        "duty_fraction": duty_fraction,
                        "total_wall_seconds": round(
                            float(budget.get("total_wall_seconds") or 0.0) + elapsed,
                            3,
                        ),
                        "events": events,
                    },
                )


def _event_time(event: dict[str, Any]) -> datetime:
    try:
        value = datetime.fromisoformat(str(event.get("ts")))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
