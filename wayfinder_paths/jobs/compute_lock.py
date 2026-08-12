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
from pathlib import Path

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
def heavy_compute_lock(
    *,
    repo_root: Path | None = None,
    label: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Iterator[None]:
    if getattr(_local, "held", False):
        # Reentrant: the outermost holder owns the flock.
        yield
        return
    path = _lock_path(repo_root)
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
                        "heavy-compute lock busy after "
                        f"{timeout_s:.0f}s (held by: {holder or 'unknown'}) — "
                        "another heavy computation is running on this box; "
                        "retry when it finishes"
                    ) from None
                time.sleep(_POLL_S)
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} label={label}\n")
        handle.flush()
        _local.held = True
        try:
            yield
        finally:
            _local.held = False
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
