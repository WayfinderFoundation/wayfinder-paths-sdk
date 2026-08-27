"""Fork one heavy phase so allocator high-water memory dies with the child."""

from __future__ import annotations

import multiprocessing
import os
import signal
import time
from collections.abc import Callable
from typing import Any

from wayfinder_paths.jobs.compute_lock import ComputeLockBusy
from wayfinder_paths.jobs.failures import TransientInfrastructureError, classify_failure


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
    process.start()
    sender.close()
    deadline = time.monotonic() + timeout_s
    rss_limit = max_rss_mb or float(
        os.environ.get("WAYFINDER_EVOLUTION_PHASE_MAX_RSS_MB", "1700")
    )
    payload: dict[str, Any] | None = None
    try:
        while time.monotonic() < deadline:
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
        if payload is None and process.is_alive():
            _kill(process.pid)
            raise TransientInfrastructureError(
                f"evolution phase timed out after {timeout_s:.0f}s"
            )
    finally:
        process.join(timeout=5)
        receiver.close()
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
