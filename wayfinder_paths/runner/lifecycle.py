from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from wayfinder_paths.runner.client import RunnerControlClient
from wayfinder_paths.runner.paths import RunnerPaths


def daemon_log_path(paths: RunnerPaths) -> Path:
    return paths.runner_dir / "runnerd.log"


def build_daemon_start_cmd(
    *,
    tick_seconds: float,
    max_workers: int,
    max_failures: int,
    default_timeout_seconds: int,
    log_level: str,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "wayfinder_paths.runnerd",
        "start",
        "--tick-seconds",
        str(float(tick_seconds)),
        "--max-workers",
        str(int(max_workers)),
        "--max-failures",
        str(int(max_failures)),
        "--default-timeout-seconds",
        str(int(default_timeout_seconds)),
        "--log-level",
        str(log_level),
    ]


def spawn_detached(
    *,
    cmd: list[str],
    repo_root: Path,
    log_path: Path,
    banner: str = "[runnerctl]",
    env: dict[str, str] | None = None,
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env_out = dict(env) if env is not None else os.environ.copy()

    popen_kwargs: dict[str, Any] = {
        "cwd": str(repo_root),
        "env": env_out,
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
    }

    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    else:
        popen_kwargs["start_new_session"] = True

    with log_path.open("ab", buffering=0) as log_f:
        log_f.write(
            f"{banner} starting runner: {' '.join(cmd)}\n".encode(
                "utf-8", errors="replace"
            )
        )
        popen = subprocess.Popen(cmd, stdout=log_f, **popen_kwargs)  # noqa: S603
        return int(popen.pid)


def try_status(
    client: RunnerControlClient,
) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
    if not client.sock_path.exists():
        return (
            False,
            None,
            {"error": "sock_missing", "sock_path": str(client.sock_path)},
        )
    resp = client.call("status")
    if resp.get("ok"):
        return True, resp.get("result"), None
    return False, None, resp


def wait_for_started(
    client: RunnerControlClient, *, timeout_s: float = 10.0
) -> tuple[bool, dict[str, Any] | None]:
    deadline = time.time() + float(timeout_s)
    last_error: dict[str, Any] | None = None
    while time.time() < deadline:
        started, status, err_obj = try_status(client)
        if started and status is not None:
            return True, status
        last_error = err_obj
        time.sleep(0.1)
    return False, last_error


def ensure_daemon_started(
    *,
    paths: RunnerPaths,
    tick_seconds: float,
    max_workers: int,
    max_failures: int,
    default_timeout_seconds: int,
    log_level: str,
    timeout_s: float = 10.0,
    banner: str = "[runnerctl]",
    env: dict[str, str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    client = RunnerControlClient(sock_path=paths.sock_path)
    started, status, err_obj = try_status(client)
    if started and status is not None:
        return True, {
            "already_running": True,
            "sock_path": str(paths.sock_path),
            "status": status,
        }

    cmd = build_daemon_start_cmd(
        tick_seconds=tick_seconds,
        max_workers=max_workers,
        max_failures=max_failures,
        default_timeout_seconds=default_timeout_seconds,
        log_level=log_level,
    )
    log_path = daemon_log_path(paths)
    pid = spawn_detached(
        cmd=cmd,
        repo_root=paths.repo_root,
        log_path=log_path,
        banner=banner,
        env=env,
    )

    ok_started, info = wait_for_started(client, timeout_s=timeout_s)
    if not ok_started:
        return False, {
            "pid": pid,
            "sock_path": str(paths.sock_path),
            "log_path": str(log_path),
            "last_error": info,
            "previous_error": err_obj,
        }

    return True, {
        "already_running": False,
        "pid": pid,
        "sock_path": str(paths.sock_path),
        "log_path": str(log_path),
        "status": info,
    }
