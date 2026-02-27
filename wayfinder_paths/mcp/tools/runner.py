from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Literal

from wayfinder_paths.mcp.utils import err, ok, read_text_excerpt, repo_root
from wayfinder_paths.runner.client import RunnerControlClient
from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT, JOB_TYPE_STRATEGY
from wayfinder_paths.runner.lifecycle import ensure_daemon_started, try_status
from wayfinder_paths.runner.paths import RunnerPaths, get_runner_paths


def _default_sock_path() -> Path:
    paths = get_runner_paths(repo_root=repo_root())
    return paths.sock_path


def _client(sock_path: str | None) -> RunnerControlClient:
    path = Path(sock_path) if sock_path else _default_sock_path()
    return RunnerControlClient(sock_path=path)


def _paths_for_client(*, root: Path, client: RunnerControlClient) -> RunnerPaths:
    runner_dir = client.sock_path.parent
    return RunnerPaths(
        repo_root=root,
        runner_dir=runner_dir,
        db_path=runner_dir / "state.db",
        logs_dir=runner_dir / "logs",
        sock_path=client.sock_path,
    )


async def runner(
    action: Literal[
        "daemon_status",
        "daemon_start",
        "daemon_stop",
        "ensure_started",
        "status",
        "add_job",
        "update_job",
        "pause_job",
        "resume_job",
        "delete_job",
        "run_once",
        "job_runs",
        "run_report",
    ],
    *,
    sock_path: str | None = None,
    # daemon start options
    tick_seconds: float | None = None,
    max_workers: int | None = None,
    max_failures: int | None = None,
    default_timeout_seconds: int | None = None,
    log_level: str | None = None,
    # Job fields
    name: str | None = None,
    type: str | None = None,  # noqa: A002 (matches CLI/API)
    payload: dict[str, Any] | None = None,
    interval_seconds: int | None = None,
    limit: int | None = None,
    run_id: int | None = None,
    tail_bytes: int | None = None,
    # Strategy payload fields
    strategy: str | None = None,
    strategy_action: str | None = None,
    config: str | None = None,
    wallet_label: str | None = None,
    timeout_seconds: int | None = None,
    # Script payload fields
    script_path: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    """Control the local runner daemon via its Unix socket.

    Notes:
      - Use `daemon_status`/`ensure_started`/`daemon_start`/`daemon_stop` to manage the daemon lifecycle.
      - `add_job` creates a new job; `run_once` triggers an immediate execution.
    """

    client = _client(sock_path)

    try:
        if action == "daemon_status":
            started, status, err_obj = try_status(client)
            return ok(
                {
                    "started": bool(started),
                    "sock_path": str(client.sock_path),
                    "status": status,
                    "error": err_obj,
                }
            )

        if action == "daemon_start":
            started, status, _ = try_status(client)
            if started and status is not None:
                return ok(
                    {
                        "started": True,
                        "already_running": True,
                        "sock_path": str(client.sock_path),
                        "status": status,
                    }
                )

            root = repo_root()
            paths = _paths_for_client(root=root, client=client)
            env_out = os.environ.copy()
            env_out["WAYFINDER_RUNNER_DIR"] = str(paths.runner_dir)

            ok_started, info = ensure_daemon_started(
                paths=paths,
                tick_seconds=float(tick_seconds) if tick_seconds is not None else 1.0,
                max_workers=int(max_workers) if max_workers is not None else 4,
                max_failures=int(max_failures) if max_failures is not None else 5,
                default_timeout_seconds=int(default_timeout_seconds)
                if default_timeout_seconds is not None
                else 20 * 60,
                log_level=str(log_level) if log_level is not None else "INFO",
                banner="[mcp]",
                env=env_out,
            )
            if not ok_started:
                log_path_s = str(info.get("log_path") or "")
                log_path = Path(log_path_s) if log_path_s else None
                return err(
                    "runner_start_failed",
                    "Runner daemon did not become ready in time",
                    details={
                        **info,
                        "log_tail": read_text_excerpt(log_path, max_chars=1200)
                        if log_path is not None
                        else None,
                    },
                )

            status = info.get("status")
            daemon_pid = info.get("pid")
            if daemon_pid is None and isinstance(status, dict):
                daemon_pid = status.get("pid")

            return ok(
                {
                    "started": True,
                    "already_running": bool(info.get("already_running")),
                    "pid": daemon_pid,
                    "spawn_pid": daemon_pid,
                    "sock_path": str(paths.sock_path),
                    "log_path": info.get("log_path"),
                    "status": status,
                }
            )

        if action == "daemon_stop":
            started, _, _ = try_status(client)
            if not started:
                return ok(
                    {
                        "stopped": True,
                        "already_stopped": True,
                        "sock_path": str(client.sock_path),
                    }
                )

            resp = client.call("shutdown")
            if not resp.get("ok"):
                return err(
                    "runner_error", str(resp.get("error") or "unknown"), details=resp
                )

            deadline = time.time() + 10.0
            while time.time() < deadline:
                if not client.sock_path.exists():
                    return ok({"stopped": True, "sock_path": str(client.sock_path)})
                ping = client.call("status")
                if not ping.get("ok"):
                    return ok(
                        {
                            "stopped": True,
                            "sock_path": str(client.sock_path),
                            "note": "daemon unreachable",
                        }
                    )
                time.sleep(0.1)

            return ok(
                {
                    "stopped": False,
                    "sock_path": str(client.sock_path),
                    "note": "timeout waiting for shutdown",
                }
            )

        if action == "ensure_started":
            started, status, _ = try_status(client)
            if started and status is not None:
                return ok(
                    {
                        "started": True,
                        "sock_path": str(client.sock_path),
                        "status": status,
                    }
                )

            started_resp = await runner(  # type: ignore[misc]
                action="daemon_start",
                sock_path=sock_path,
                tick_seconds=tick_seconds,
                max_workers=max_workers,
                max_failures=max_failures,
                default_timeout_seconds=default_timeout_seconds,
                log_level=log_level,
            )
            if not started_resp.get("ok"):
                return started_resp

            # Re-fetch full status after start.
            resp = _client(sock_path).call("status")
            if resp.get("ok"):
                return ok(
                    {
                        "started": True,
                        "sock_path": str(_client(sock_path).sock_path),
                        "status": resp.get("result"),
                    }
                )
            return err(
                "runner_error", str(resp.get("error") or "unknown"), details=resp
            )

        # Remaining actions require an already-running daemon.
        if not client.sock_path.exists():
            return err(
                "runner_not_running",
                "Runner daemon socket not found. Start it with: poetry run wayfinder runner ensure",
                details={"sock_path": str(client.sock_path)},
            )

        if action == "status":
            resp = client.call("status")
            if resp.get("ok"):
                return ok(resp.get("result"))
            return err(
                "runner_error", str(resp.get("error") or "unknown"), details=resp
            )

        if action == "job_runs":
            if not name:
                return err("invalid_request", "name is required for job_runs")
            lim = limit if isinstance(limit, int) else 50
            resp = client.call("job_runs", {"name": str(name).strip(), "limit": lim})
            if resp.get("ok"):
                return ok(resp.get("result"))
            return err(
                "runner_error", str(resp.get("error") or "unknown"), details=resp
            )

        if action == "run_report":
            if run_id is None:
                return err("invalid_request", "run_id is required for run_report")
            tb = tail_bytes if isinstance(tail_bytes, int) else 4000
            resp = client.call("run_report", {"run_id": int(run_id), "tail_bytes": tb})
            if resp.get("ok"):
                return ok(resp.get("result"))
            return err(
                "runner_error", str(resp.get("error") or "unknown"), details=resp
            )

        if action == "add_job":
            if not name:
                return err("invalid_request", "name is required for add_job")
            interval = interval_seconds
            if interval is None:
                return err(
                    "invalid_request", "interval_seconds is required for add_job"
                )
            if not isinstance(interval, int) or interval <= 0:
                return err(
                    "invalid_request", "interval_seconds must be a positive integer"
                )

            job_type = str(type or JOB_TYPE_STRATEGY).strip().lower()
            if job_type not in {JOB_TYPE_STRATEGY, JOB_TYPE_SCRIPT}:
                return err("invalid_request", f"unsupported job type: {job_type}")

            job_payload: dict[str, Any] = {"debug": bool(debug)}
            if wallet_label:
                job_payload["wallet_label"] = str(wallet_label).strip()
            if timeout_seconds is not None:
                job_payload["timeout_seconds"] = int(timeout_seconds)
            if env is not None:
                if not isinstance(env, dict):
                    return err("invalid_request", "env must be an object")
                job_payload["env"] = {str(k): str(v) for k, v in env.items()}

            if job_type == JOB_TYPE_STRATEGY:
                strat = (strategy or "").strip()
                if not strat:
                    return err("invalid_request", "strategy is required for add_job")
                job_payload.update(
                    {
                        "strategy": strat,
                        "action": (strategy_action or "update").strip(),
                        "config": (config or "config.json").strip(),
                    }
                )
            else:
                sp = (script_path or "").strip()
                if not sp:
                    return err(
                        "invalid_request",
                        "script_path is required for add_job when type=script",
                    )
                argv = [str(a) for a in (args or []) if str(a).strip()]
                job_payload.update(
                    {
                        "script_path": sp,
                        "args": argv,
                    }
                )

            resp = client.call(
                "add_job",
                {
                    "name": str(name).strip(),
                    "type": job_type,
                    "payload": job_payload,
                    "interval_seconds": int(interval),
                },
            )
            if resp.get("ok"):
                return ok(resp.get("result"))
            return err(
                "runner_error", str(resp.get("error") or "unknown"), details=resp
            )

        if action == "update_job":
            if not name:
                return err("invalid_request", "name is required for update_job")
            if payload is not None and not isinstance(payload, dict):
                return err("invalid_request", "payload must be an object")
            if interval_seconds is not None and (
                not isinstance(interval_seconds, int) or interval_seconds <= 0
            ):
                return err(
                    "invalid_request", "interval_seconds must be a positive integer"
                )
            resp = client.call(
                "update_job",
                {
                    "name": str(name).strip(),
                    "payload": payload,
                    "interval_seconds": interval_seconds,
                },
            )
            if resp.get("ok"):
                return ok(resp.get("result"))
            return err(
                "runner_error", str(resp.get("error") or "unknown"), details=resp
            )

        if action == "pause_job":
            if not name:
                return err("invalid_request", "name is required for pause_job")
            resp = client.call("pause_job", {"name": str(name).strip()})
            if resp.get("ok"):
                return ok(resp.get("result"))
            return err(
                "runner_error", str(resp.get("error") or "unknown"), details=resp
            )

        if action == "resume_job":
            if not name:
                return err("invalid_request", "name is required for resume_job")
            resp = client.call("resume_job", {"name": str(name).strip()})
            if resp.get("ok"):
                return ok(resp.get("result"))
            return err(
                "runner_error", str(resp.get("error") or "unknown"), details=resp
            )

        if action == "delete_job":
            if not name:
                return err("invalid_request", "name is required for delete_job")
            resp = client.call("delete_job", {"name": str(name).strip()})
            if resp.get("ok"):
                return ok(resp.get("result"))
            return err(
                "runner_error", str(resp.get("error") or "unknown"), details=resp
            )

        if action == "run_once":
            if not name:
                return err("invalid_request", "name is required for run_once")
            resp = client.call("run_once", {"name": str(name).strip()})
            if resp.get("ok"):
                return ok(resp.get("result"))
            return err(
                "runner_error", str(resp.get("error") or "unknown"), details=resp
            )

        return err("invalid_request", f"unknown action: {action}")
    except Exception as exc:  # noqa: BLE001
        return err(
            "runner_unreachable",
            str(exc),
            details={"sock_path": str(client.sock_path)},
        )
