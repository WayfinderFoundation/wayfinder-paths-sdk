"""Fork one heavy phase so allocator high-water memory dies with the child."""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.compute_lock import ComputeLockBusy
from wayfinder_paths.jobs.execution.op_process import pid_is_op_runner, proc_start_ticks
from wayfinder_paths.jobs.failures import TransientInfrastructureError, classify_failure
from wayfinder_paths.runner.monitor_state import atomic_write_json

GOVERNOR_STATE_PATH = Path("/tmp/wayfinder-burst-governor.json")
HEAVY_OP_REGISTRY_DIR = Path("/tmp/wayfinder-heavy-ops")
GOVERNOR_STATE_MAX_AGE_SECONDS = 10.0
HEARTBEAT_SECONDS = 60.0


def run_isolated_phase(
    target: Callable[..., dict[str, Any]],
    *args: Any,
    timeout_s: float,
    max_rss_mb: float | None = None,
) -> dict[str, Any]:
    """Run a serializable phase under a wall/RSS supervisor.

    Linux production uses ``fork`` so strategy modules and frozen inputs need
    not be copied through a command line. The compact outcome crosses one
    pipe; every trace, dataframe, Optuna study, and allocator arena disappears
    when the child exits.
    """
    if "fork" not in multiprocessing.get_all_start_methods():
        return target(*args)
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_child_entry, args=(sender, target, args))
    registration = _register_starting_supervisor()
    try:
        process.start()
        if registration is not None and process.pid:
            _update_registration(registration, child_pid=process.pid)
    except Exception:
        _remove_registration(registration)
        raise
    sender.close()
    deadline = time.monotonic() + timeout_s
    rss_limit = max_rss_mb or float(
        os.environ.get("WAYFINDER_EVOLUTION_PHASE_MAX_RSS_MB", "1700")
    )
    payload: dict[str, Any] | None = None
    paused = False
    paused_total_s = 0.0
    stale_pause_s = 0.0
    last_sample = time.monotonic()
    last_heartbeat = last_sample - HEARTBEAT_SECONDS
    try:
        while True:
            now = time.monotonic()
            elapsed = max(0.0, now - last_sample)
            pause_state = _governor_pause_state(process.pid)
            if paused:
                deadline += elapsed
                paused_total_s += elapsed
                stale_pause_s = stale_pause_s + elapsed if pause_state is None else 0.0
                if pause_state is False:
                    paused = False
                elif stale_pause_s > GOVERNOR_STATE_MAX_AGE_SECONDS:
                    _resume(process.pid)
                    _kill(process.pid)
                    raise TransientInfrastructureError(
                        "burst governor state went stale while evolution was paused"
                    )
            elif pause_state is True:
                paused = True
                stale_pause_s = 0.0
            last_sample = now
            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                _heartbeat(
                    process.pid,
                    active_s=max(0.0, timeout_s - (deadline - now)),
                    paused_s=paused_total_s,
                    paused=paused,
                )
                if registration is not None and process.pid:
                    _update_registration(registration, child_pid=process.pid)
                last_heartbeat = now
            if not paused and now >= deadline:
                _kill(process.pid)
                raise TransientInfrastructureError(
                    f"evolution phase timed out after {timeout_s:.0f}s active time"
                )
            if receiver.poll(1.0):
                payload = receiver.recv()
                break
            if not process.is_alive():
                break
            rss = _process_rss_mb(process.pid)
            if rss is not None and rss > rss_limit:
                _kill(process.pid)
                raise TransientInfrastructureError(
                    f"evolution phase RSS {rss:.0f}MB exceeded {rss_limit:.0f}MB"
                )
    finally:
        if paused:
            _resume(process.pid)
        process.join(timeout=5)
        receiver.close()
        _remove_registration(registration)
    if payload is None:
        raise TransientInfrastructureError(
            f"evolution phase exited without a result (exit={process.exitcode})"
        )
    if payload.get("ok") is not True:
        if payload.get("transient"):
            raise TransientInfrastructureError(str(payload.get("error")))
        raise RuntimeError(str(payload.get("error") or "isolated phase failed"))
    return dict(payload["result"])


def _child_entry(
    sender: Any, target: Callable[..., dict[str, Any]], args: tuple[Any, ...]
) -> None:
    try:
        sender.send({"ok": True, "result": target(*args)})
    except Exception as exc:  # noqa: BLE001 - serialize candidate evidence
        sender.send(
            {
                "ok": False,
                "transient": isinstance(
                    exc, (ComputeLockBusy, MemoryError, TransientInfrastructureError)
                )
                or classify_failure(str(exc)) == "infrastructure",
                "error": str(exc)[:1000],
            }
        )
    finally:
        sender.close()


def _process_rss_mb(pid: int | None) -> float | None:
    if not pid:
        return None
    try:
        for line in open(f"/proc/{pid}/status", encoding="utf-8"):
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _kill(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _resume(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGCONT)
    except OSError:
        pass


def _registry_dir() -> Path:
    return Path(
        os.environ.get("WAYFINDER_HEAVY_OP_REGISTRY_DIR", str(HEAVY_OP_REGISTRY_DIR))
    )


def _register_starting_supervisor() -> Path | None:
    supervisor_pid = os.getpid()
    start_ticks = proc_start_ticks(supervisor_pid)
    if start_ticks is None or not pid_is_op_runner(supervisor_pid):
        return None
    path = _registry_dir() / f"{supervisor_pid}-{start_ticks}.json"
    atomic_write_json(
        path,
        {
            "schema_version": "1.0",
            "state": "starting",
            "supervisor_pid": supervisor_pid,
            "supervisor_start_ticks": start_ticks,
            "updated_at": time.time(),
        },
    )
    os.chmod(path, 0o600)
    return path


def _update_registration(path: Path, *, child_pid: int) -> None:
    supervisor_ticks = proc_start_ticks(os.getpid())
    child_ticks = proc_start_ticks(child_pid)
    if supervisor_ticks is None or child_ticks is None:
        return
    atomic_write_json(
        path,
        {
            "schema_version": "1.0",
            "state": "running",
            "supervisor_pid": os.getpid(),
            "supervisor_start_ticks": supervisor_ticks,
            "child_pid": child_pid,
            "child_start_ticks": child_ticks,
            "updated_at": time.time(),
        },
    )
    os.chmod(path, 0o600)


def _remove_registration(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _governor_pause_state(pid: int | None) -> bool | None:
    """Return whether this exact child is paused; ``None`` means stale/absent."""
    if not pid:
        return False
    path = Path(os.environ.get("WAYFINDER_BURST_STATE_PATH", str(GOVERNOR_STATE_PATH)))
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        age = max(0.0, time.time() - float(state["updated_at"]))
    except (OSError, KeyError, TypeError, ValueError):
        return None
    if not isinstance(state, dict) or age > GOVERNOR_STATE_MAX_AGE_SECONDS:
        return None
    affected = state.get("affected_pids")
    return bool(state.get("paused")) and isinstance(affected, list) and pid in affected


def _heartbeat(
    pid: int | None, *, active_s: float, paused_s: float, paused: bool
) -> None:
    print(
        "evolution phase heartbeat "
        f"child={pid or 0} active_s={active_s:.0f} paused_s={paused_s:.0f} "
        f"state={'paused' if paused else 'running'}",
        file=sys.stderr,
        flush=True,
    )
