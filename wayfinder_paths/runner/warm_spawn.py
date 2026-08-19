"""Warm forkserver spawner for jobs_v1 script ticks.

Every scheduled jobs_v1 tick used to `subprocess.Popen` a fresh Python that
cold-imports the full SDK (~30-90 CPU-s under throttle). `WarmSpawner` keeps a
single forkserver process alive with the heavy modules preloaded
(`runner/preload.py`) and forks each tick from that warm image instead. The
fork preserves the daemon's whole bookkeeping contract:

- `WarmChild` duck-types the `subprocess.Popen` surface `_reap` uses
  (`.pid`, `.poll()`, `.returncode` — including `returncode is None` until an
  exit has actually been observed, matching Popen after a `killpg`).
- The child calls `os.setsid()` first, so the daemon's process-group kills
  (timeout SIGKILL, shutdown/stop SIGTERM) land exactly as they do for Popen
  workers started with `start_new_session=True`.
- The child entry mirrors the compiler's jobs_v1 wrapper: log fds redirected
  to the run's log file, env replaced with the run env the daemon built,
  workspace on `sys.path`, `run_scheduled_tick(job_dir)`, exit code from
  `payload["ok"]`.

The daemon treats ANY exception from `WarmSpawner.spawn` as "use the cold
Popen path" — these ticks trade real money, so the fallback must always be
reachable (kill-switch: WAYFINDER_RUNNER_NO_FORK=1).
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import threading
from pathlib import Path
from typing import Any

PRELOAD_MODULES = ("wayfinder_paths.runner.preload",)


def _warm_tick_entry(*, env: dict[str, str], log_path: str, cwd: str) -> None:
    """Runs inside the forked child. Mirrors the compiler's jobs_v1 wrapper
    (jobs/compiler.py) plus the process setup Popen did for free."""
    os.setsid()

    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    if fd > 2:
        os.close(fd)

    # Exact Popen(env=...) parity: the daemon built a full environment
    # (os.environ copy + per-run keys + job payload env); replace, not merge.
    os.environ.clear()
    os.environ.update(env)
    os.chdir(cwd)

    job_dir = env["WAYFINDER_JOB_DIR"]
    workspace = str(Path(job_dir) / "workspace")
    if workspace not in sys.path:
        sys.path.insert(0, workspace)

    from wayfinder_paths.jobs.execution.driver import run_scheduled_tick

    payload = run_scheduled_tick(job_dir)
    raise SystemExit(0 if payload.get("ok") else 1)


class WarmChild:
    """Popen-shaped handle over a forked tick, for the daemon's `_reap`."""

    def __init__(self, process: Any) -> None:
        self._process = process
        self.returncode: int | None = None

    @property
    def pid(self) -> int:
        pid = self._process.pid
        if pid is None:
            raise RuntimeError("warm child has no pid (not started)")
        return int(pid)

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        # is_alive() reaps via the forkserver sentinel; exitcode is negative
        # for signal deaths, same convention as subprocess.Popen.returncode.
        if self._process.is_alive():
            return None
        exitcode = self._process.exitcode
        if exitcode is None:
            return None
        self.returncode = int(exitcode)
        return self.returncode


class WarmSpawner:
    def __init__(self, *, preload_modules: tuple[str, ...] = PRELOAD_MODULES) -> None:
        self._preload_modules = preload_modules
        self._lock = threading.Lock()
        self._ctx: Any | None = None

    def _context(self) -> Any:
        with self._lock:
            if self._ctx is None:
                ctx = multiprocessing.get_context("forkserver")
                ctx.set_forkserver_preload(list(self._preload_modules))
                self._ctx = ctx
            return self._ctx

    def spawn(
        self,
        *,
        job_name: str,
        env: dict[str, str],
        log_path: str | Path,
        cwd: str | Path,
    ) -> WarmChild:
        process = self._context().Process(
            target=_warm_tick_entry,
            kwargs={
                "env": dict(env),
                "log_path": str(log_path),
                "cwd": str(cwd),
            },
            name=f"wayfinder-warm-tick-{job_name}",
            daemon=False,
        )
        process.start()
        return WarmChild(process)
