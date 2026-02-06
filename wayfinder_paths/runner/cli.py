from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click
from loguru import logger

from wayfinder_paths.runner.client import RunnerControlClient
from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT, JOB_TYPE_STRATEGY
from wayfinder_paths.runner.daemon import RunnerDaemon
from wayfinder_paths.runner.lifecycle import ensure_daemon_started, try_status
from wayfinder_paths.runner.paths import get_runner_paths


def _echo_json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, default=str))


def _client(sock_path: Path) -> RunnerControlClient:
    return RunnerControlClient(sock_path=sock_path)


@click.group(name="runner", help="Local runner daemon for strategies and scripts.")
def runner_cli() -> None:
    pass


@runner_cli.command(name="start", help="Start the runner daemon.")
@click.option("--tick-seconds", type=float, default=1.0, show_default=True)
@click.option("--max-workers", type=int, default=4, show_default=True)
@click.option("--max-failures", type=int, default=5, show_default=True)
@click.option("--default-timeout-seconds", type=int, default=20 * 60, show_default=True)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default="INFO",
    show_default=True,
)
@click.option(
    "--detach/--no-detach",
    default=False,
    show_default=True,
    help="Run the daemon in the background (recommended for long-lived use).",
)
def start_cmd(
    tick_seconds: float,
    max_workers: int,
    max_failures: int,
    default_timeout_seconds: int,
    log_level: str,
    detach: bool,
) -> None:
    paths = get_runner_paths()

    if detach:
        ok_started, info = ensure_daemon_started(
            paths=paths,
            tick_seconds=tick_seconds,
            max_workers=max_workers,
            max_failures=max_failures,
            default_timeout_seconds=default_timeout_seconds,
            log_level=log_level,
        )
        if ok_started:
            _echo_json({"ok": True, "result": {"started": True, **info}})
        else:
            _echo_json(
                {
                    "ok": False,
                    "error": "runner_start_failed",
                    "details": info,
                }
            )
        return

    running, status, _ = try_status(_client(paths.sock_path))
    if running and status is not None:
        click.echo(f"Runner already running at {paths.sock_path}.")
        return

    logger.remove()
    logger.add(sys.stderr, level=str(log_level).upper())

    daemon = RunnerDaemon(
        paths=paths,
        tick_seconds=tick_seconds,
        max_workers=max_workers,
        max_failures=max_failures,
        default_timeout_seconds=default_timeout_seconds,
        log_level=log_level,
    )
    daemon.start()


@runner_cli.command(
    name="ensure",
    help="Ensure the runner daemon is running (starts detached if needed).",
)
@click.option("--tick-seconds", type=float, default=1.0, show_default=True)
@click.option("--max-workers", type=int, default=4, show_default=True)
@click.option("--max-failures", type=int, default=5, show_default=True)
@click.option("--default-timeout-seconds", type=int, default=20 * 60, show_default=True)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default="INFO",
    show_default=True,
)
def ensure_cmd(
    tick_seconds: float,
    max_workers: int,
    max_failures: int,
    default_timeout_seconds: int,
    log_level: str,
) -> None:
    paths = get_runner_paths()
    ok_started, info = ensure_daemon_started(
        paths=paths,
        tick_seconds=tick_seconds,
        max_workers=max_workers,
        max_failures=max_failures,
        default_timeout_seconds=default_timeout_seconds,
        log_level=log_level,
    )
    if ok_started:
        _echo_json({"ok": True, "result": {"started": True, **info}})
    else:
        _echo_json({"ok": False, "error": "runner_start_failed", "details": info})


@runner_cli.command(name="stop", help="Stop the runner daemon (via local socket).")
def stop_cmd() -> None:
    paths = get_runner_paths()
    resp = _client(paths.sock_path).call("shutdown")
    _echo_json(resp)


@runner_cli.command(name="status", help="Show runner + job status.")
def status_cmd() -> None:
    paths = get_runner_paths()
    resp = _client(paths.sock_path).call("status")
    _echo_json(resp)


@runner_cli.command(name="add-job", help="Add a job without restarting the daemon.")
@click.option("--name", required=True)
@click.option(
    "--type",
    "job_type",
    type=click.Choice([JOB_TYPE_STRATEGY, JOB_TYPE_SCRIPT], case_sensitive=False),
    default=JOB_TYPE_STRATEGY,
    show_default=True,
)
@click.option("--strategy", default=None, help="Strategy name (strategy jobs only).")
@click.option("--action", default="update", show_default=True, help="Strategy action.")
@click.option(
    "--script-path",
    default=None,
    help="Path to a .py script inside .wayfinder_runs/ (script jobs only).",
)
@click.option(
    "--arg",
    "script_args",
    multiple=True,
    help="Script argument (repeatable; script jobs only).",
)
@click.option(
    "--interval",
    "interval_seconds",
    type=int,
    required=True,
    help="Seconds between runs.",
)
@click.option("--config", "config_path", default="config.json", show_default=True)
@click.option("--wallet-label", default=None)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=int,
    default=None,
    help="Per-run timeout seconds.",
)
@click.option(
    "--env-json", default=None, help="JSON object of env vars for the worker."
)
@click.option("--debug/--no-debug", default=False, show_default=True)
def add_job_cmd(
    name: str,
    job_type: str,
    strategy: str | None,
    action: str,
    script_path: str | None,
    script_args: tuple[str, ...],
    interval_seconds: int,
    config_path: str,
    wallet_label: str | None,
    timeout_seconds: int | None,
    env_json: str | None,
    debug: bool,
) -> None:
    paths = get_runner_paths()
    jt = str(job_type).lower().strip()

    env_payload: dict[str, Any] | None = None
    if env_json is not None:
        decoded = json.loads(env_json)
        if not isinstance(decoded, dict):
            raise click.UsageError("--env-json must decode to an object")
        env_payload = {str(k): str(v) for k, v in decoded.items()}

    payload: dict[str, Any]
    if jt == JOB_TYPE_STRATEGY:
        if not strategy:
            raise click.UsageError("--strategy is required for type=strategy")
        payload = {
            "strategy": str(strategy),
            "action": str(action),
            "config": str(config_path),
            "debug": bool(debug),
        }
        if wallet_label:
            payload["wallet_label"] = str(wallet_label)
        if timeout_seconds is not None:
            payload["timeout_seconds"] = int(timeout_seconds)
        if env_payload is not None:
            payload["env"] = env_payload
    elif jt == JOB_TYPE_SCRIPT:
        if not script_path:
            raise click.UsageError("--script-path is required for type=script")
        args_list = [str(a) for a in script_args if str(a).strip()]
        payload = {
            "script_path": str(script_path),
            "args": args_list,
            "debug": bool(debug),
        }
        if wallet_label:
            payload["wallet_label"] = str(wallet_label)
        if timeout_seconds is not None:
            payload["timeout_seconds"] = int(timeout_seconds)
        if env_payload is not None:
            payload["env"] = env_payload
    else:
        raise click.UsageError(f"Unsupported type: {job_type}")

    resp = _client(paths.sock_path).call(
        "add_job",
        {
            "name": str(name),
            "type": str(job_type),
            "payload": payload,
            "interval_seconds": int(interval_seconds),
        },
    )
    _echo_json(resp)


@runner_cli.command(name="update-job", help="Update a job definition.")
@click.option("--name", required=True)
@click.option(
    "--interval",
    "interval_seconds",
    type=int,
    default=None,
    help="New interval seconds.",
)
@click.option("--payload-json", default=None, help="Full replacement payload JSON.")
def update_job_cmd(
    name: str, interval_seconds: int | None, payload_json: str | None
) -> None:
    payload = None
    if payload_json is not None:
        payload = json.loads(payload_json)
        if not isinstance(payload, dict):
            raise click.UsageError("--payload-json must decode to an object")

    paths = get_runner_paths()
    resp = _client(paths.sock_path).call(
        "update_job",
        {"name": str(name), "interval_seconds": interval_seconds, "payload": payload},
    )
    _echo_json(resp)


@runner_cli.command(name="pause", help="Pause a job by name.")
@click.argument("name")
def pause_cmd(name: str) -> None:
    paths = get_runner_paths()
    resp = _client(paths.sock_path).call("pause_job", {"name": str(name)})
    _echo_json(resp)


@runner_cli.command(name="resume", help="Resume a paused/error job by name.")
@click.argument("name")
def resume_cmd(name: str) -> None:
    paths = get_runner_paths()
    resp = _client(paths.sock_path).call("resume_job", {"name": str(name)})
    _echo_json(resp)


@runner_cli.command(name="delete", help="Delete a job by name.")
@click.argument("name")
def delete_cmd(name: str) -> None:
    paths = get_runner_paths()
    resp = _client(paths.sock_path).call("delete_job", {"name": str(name)})
    _echo_json(resp)


@runner_cli.command(name="run-once", help="Trigger a job to run immediately once.")
@click.argument("name")
def run_once_cmd(name: str) -> None:
    paths = get_runner_paths()
    resp = _client(paths.sock_path).call("run_once", {"name": str(name)})
    _echo_json(resp)


@runner_cli.command(name="runs", help="List recent runs for a job.")
@click.argument("name")
@click.option("--limit", type=int, default=20, show_default=True)
def runs_cmd(name: str, limit: int) -> None:
    paths = get_runner_paths()
    resp = _client(paths.sock_path).call(
        "job_runs", {"name": str(name), "limit": int(limit)}
    )
    _echo_json(resp)


@runner_cli.command(name="run-report", help="Show run details and tail the run log.")
@click.argument("run_id", type=int)
@click.option("--tail-bytes", type=int, default=4000, show_default=True)
def run_report_cmd(run_id: int, tail_bytes: int) -> None:
    paths = get_runner_paths()
    resp = _client(paths.sock_path).call(
        "run_report", {"run_id": int(run_id), "tail_bytes": int(tail_bytes)}
    )
    _echo_json(resp)
