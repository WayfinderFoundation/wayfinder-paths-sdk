from __future__ import annotations

from pathlib import Path
from typing import Any

from wayfinder_paths.runner.client import RunnerControlClient
from wayfinder_paths.runner.constants import DEFAULT_MAX_WORKERS, JOB_TYPE_SCRIPT
from wayfinder_paths.runner.lifecycle import ensure_daemon_started
from wayfinder_paths.runner.paths import RunnerPaths, get_runner_paths
from wayfinder_paths.runner.schedule import schedule_request_params


class RunnerBridge:
    """Thin bridge from high-level Wayfinder jobs to the existing runner daemon."""

    def __init__(self, *, repo_root: Path | None = None) -> None:
        self.paths: RunnerPaths = get_runner_paths(repo_root=repo_root)
        self.client = RunnerControlClient(sock_path=self.paths.sock_path)

    def ensure_started(self) -> dict[str, Any]:
        started, info = ensure_daemon_started(
            paths=self.paths,
            tick_seconds=1.0,
            max_workers=DEFAULT_MAX_WORKERS,
            max_failures=5,
            default_timeout_seconds=20 * 60,
            log_level="INFO",
        )
        return {
            "ok": bool(started),
            "result": info if started else None,
            "error": None if started else info,
        }

    def add_or_update_script_job(
        self,
        *,
        name: str,
        script_path: str,
        interval_seconds: int | None = None,
        cron_expr: str | None = None,
        timezone: str = "UTC",
        timeout_seconds: int | None = None,
        env: dict[str, str],
    ) -> dict[str, Any]:
        if not env:
            raise ValueError(
                "add_or_update_script_job requires the full job env: update_job "
                "replaces the runner payload wholesale, and a missing env "
                "silently reverts WAYFINDER_JOB_MODE to paper (this flipped a "
                "live job in production). Regenerate via JobCompiler.compile — "
                "job.yaml is the source of truth for schedules and env."
            )
        schedule = schedule_request_params(
            interval_seconds=interval_seconds,
            cron_expr=cron_expr,
            timezone=timezone,
        )
        payload: dict[str, Any] = {
            "script_path": script_path,
            "args": [],
            "debug": False,
            "env": {str(k): str(v) for k, v in env.items()},
        }
        if timeout_seconds is not None:
            payload["timeout_seconds"] = int(timeout_seconds)

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

    def job_states(self) -> dict[str, dict[str, Any]]:
        """Full live runner state per job name — status, next_run_at,
        last_run_at, last_ok_at, consecutive_failures, last_error, and the
        baked `payload.env` (WAYFINDER_JOB_MODE / WAYFINDER_JOB_AGENT_MODE /
        WAYFINDER_JOB_REVISION). This is the runtime source of truth: the
        driver executes the mode in the env, not job.yaml. Empty when the
        daemon is unreachable — callers degrade to the declared config rather
        than raise, so a down runner never breaks a sync."""
        try:
            resp = self.client.call("status")
        except Exception:
            return {}
        jobs = ((resp or {}).get("result") or {}).get("jobs") or []
        return {str(job.get("name")): job for job in jobs if job.get("name")}

    def job_statuses(self) -> dict[str, str]:
        """Live runner status per job name, e.g. {"foo-script": "PAUSED"}."""
        return {
            name: str(state.get("status") or "")
            for name, state in self.job_states().items()
        }

    def pause(self, name: str) -> dict[str, Any]:
        return self.client.call("pause_job", {"name": name})

    def resume(self, name: str) -> dict[str, Any]:
        return self.client.call("resume_job", {"name": name})

    def delete(self, name: str) -> dict[str, Any]:
        return self.client.call("delete_job", {"name": name})
