"""Detached local supervisor for the same portable runtime used by Sprites."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.execution.op_process import process_identity_fields
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.sprite_bundle import sha256
from wayfinder_paths.runner.monitor_state import atomic_write_json

LOG_BYTES = 32768
SUMMARY_BYTES = 524288


def run_process(
    command: list[str], directory: Path, timeout: float, *, cancellable: bool = True
) -> dict[str, Any]:
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated: set[str] = set()

    def drain(stream: Any, name: str) -> None:
        with stream:
            while chunk := stream.read(4096):
                buffers[name].extend(chunk)
                if len(buffers[name]) > LOG_BYTES:
                    truncated.add(name)
                    del buffers[name][:-LOG_BYTES]

    proc = subprocess.Popen(
        command,
        cwd=directory,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    threads = [
        threading.Thread(target=drain, args=(stream, name), daemon=True)
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr))
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    timed_out = cancelled = False
    try:
        while proc.poll() is None:
            cancelled = cancellable and (directory / "cancel").exists()
            timed_out = time.monotonic() >= deadline
            if cancelled or timed_out:
                break
            time.sleep(0.05)
    finally:
        # Kill descendants too, including process-grid workers left by a failed
        # computation. The child owns a new process group, never the caller's.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        for thread in threads:
            thread.join(timeout=2)
    return {
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "stdout": buffers["stdout"].decode(errors="replace"),
        "stderr": buffers["stderr"].decode(errors="replace"),
        "logs_truncated": bool(truncated),
    }


def work(directory: Path) -> None:
    status_file = directory / "status.json"
    record = json.loads(status_file.read_text())
    atomic_write_json(
        directory / "supervisor.json",
        {
            "pid": os.getpid(),
            "started_at": utc_now_iso(),
            **process_identity_fields(os.getpid()),
        },
    )

    def cancel(signum: int, frame: Any) -> None:
        (directory / "cancel").touch()

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    artifacts, output = directory / "artifacts.tar.gz", directory / "summary.json"
    command = [
        sys.executable,
        "-m",
        "wayfinder_paths.jobs.sprite_runtime",
        "--bundle",
        str(directory / "workspace.tar.gz"),
        "--root",
        str(directory / "workspace"),
        "--output",
        str(output),
        "--artifacts",
        str(artifacts),
    ]
    try:
        if (directory / "cancel").exists():
            record.update(status="cancelled", error="Cancelled before execution")
            return
        record.update(status="running", started_at=utc_now_iso())
        atomic_write_json(status_file, record)
        result = run_process(command, directory, record["timeout_seconds"])
        record["result"] = result
        if result["cancelled"] or (directory / "cancel").exists():
            state, error = "cancelled", "Backtest cancelled"
        elif result["timed_out"]:
            state, error = "timed_out", "Backtest execution timed out"
        elif result["exit_code"]:
            state, error = "failed", "Backtest computation failed"
        else:
            state, error = "succeeded", ""
        record.update(status=state, error=error)
        if state != "succeeded" or not artifacts.exists():
            artifacts.unlink(missing_ok=True)
            if (directory / "workspace").exists():
                collection = run_process(
                    [*command, "--collect-only"], directory, 60, cancellable=False
                )
                if collection["exit_code"]:
                    artifacts.unlink(missing_ok=True)
        if artifacts.exists():
            record["artifacts"] = {
                "sha256": sha256(artifacts),
                "size": artifacts.stat().st_size,
            }
        if output.exists():
            with output.open("rb") as stream:
                raw = stream.read(SUMMARY_BYTES + 1)
            if len(raw) > SUMMARY_BYTES:
                raise ValueError("Runtime summary exceeded 512 KiB")
            result["output"] = json.loads(raw)
            json.dumps(result, allow_nan=False)
        if record["status"] == "succeeded" and (
            not record["artifacts"] or "output" not in result
        ):
            record.update(
                status="failed",
                error="Runtime did not produce complete results and artifacts",
            )
    except Exception as exc:
        record.update(status="failed", error=str(exc))
    finally:
        record["finished_at"] = utc_now_iso()
        atomic_write_json(status_file, record)


def main() -> None:
    work(Path(sys.argv[1]).resolve())


if __name__ == "__main__":
    main()
