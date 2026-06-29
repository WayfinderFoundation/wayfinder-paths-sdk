from __future__ import annotations

from pathlib import Path
from typing import Any

from wayfinder_paths.runner.client import RunnerControlClient
from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT
from wayfinder_paths.runner.lifecycle import ensure_daemon_started
from wayfinder_paths.runner.paths import RunnerPaths, get_runner_paths
from wayfinder_paths.runner.schedule import schedule_request_params


class RunnerBridge:
    """Thin bridge from high-level Wayfinder jobs to the existing runner daemon."""

    def __init__(self, *, repo_root: Path | None = None) -> None:
        self.paths: RunnerPaths = get_runner_paths(repo_root=repo_root)
        self.client = RunnerControlClient(sock_path=self.paths.sock_path)

    def ensure_started(self) -> dict[str, Any]:
        started, info = ensure_daemon_started(paths=self.paths)
        return {"ok": bool(started), "result": info if started else None, "error": None if started else info}

    def add_or_update_script_job(
        self,
        *,
        name: str,
        script_path: str,
        interval_seconds: int | None = None,
        cron_expr: str | None = None,
        timezone: str = "UTC",
        timeout_seconds: int | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        schedule = schedule_request_params(
            interval_seconds=interval_seconds,
            cron_expr=cron_expr,
            timezone=timezone,
        )
        payload: dict[str, Any] = {
            "script_path": script_path,
            "args": [],
            "debug": False,
        }
        if timeout_seconds is not None:
            payload["timeout_seconds"] = int(timeout_seconds)
        if env:
            payload["env"] = {str(k): str(v) for k, v in env.items()}

        params: dict[str, Any] = {
            "name": name,
            "type": JOB_TYPE_SCRIPT,
            "payload": payload,
        }
        params.update(schedule)

        response = self.client.call("add_job", params)
        if response.get("ok"):
            return response

        error = str(response.get("error") or "")
        if "UNIQUE constraint failed" not in error and "already" not in error.lower():
            return response

        update_params: dict[str, Any] = {"name": name, "payload": payload}
        update_params.update(schedule)
        return self.client.call("update_job", update_params)

    def pause(self, name: str) -> dict[str, Any]:
        return self.client.call("pause_job", {"name": name})

    def resume(self, name: str) -> dict[str, Any]:
        return self.client.call("resume_job", {"name": name})

    def delete(self, name: str) -> dict[str, Any]:
        return self.client.call("delete_job", {"name": name})

    def run_once(self, name: str) -> dict[str, Any]:
        return self.client.call("run_once", {"name": name})

    def status(self) -> dict[str, Any]:
        return self.client.call("status")
