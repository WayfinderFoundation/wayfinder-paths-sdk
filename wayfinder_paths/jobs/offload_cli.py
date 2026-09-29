"""Turn remote (Sprite) offloading of backtest compute on or off in the SDK config.

Off is ``backtest_runner.provider: local``; on is ``provider: sprites`` with
``offload_operations`` deciding whether standalone operations (backtests, scans,
experiments) go too or only evolution campaigns. Every other ``backtest_runner``
setting is kept, so turning offloading back on restores it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import click

from wayfinder_paths.core.config import write_config_json
from wayfinder_paths.jobs.backtest_runner import (
    load_runner_config,
    offload_switched_off,
    sdk_config_path,
)
from wayfinder_paths.jobs.store import JobStore

PROVIDER_ENV = "WAYFINDER_BACKTEST_RUNNER"


def offload_status(root: Path, env: Mapping[str, str]) -> dict[str, Any]:
    config = load_runner_config(repo_root=root, environ=env)
    remote = config.configured and config.provider != "local"
    # The backend can switch offloading off too; this node then computes locally.
    backend_off = offload_switched_off(config) if remote else None
    effective = remote and backend_off is None
    status: dict[str, Any] = {
        "offloading": remote,
        "provider": config.provider,
        "evolution_campaigns": "remote" if effective else "local",
        "standalone_operations": (
            "remote" if effective and config.offload_operations else "local"
        ),
        "backend_switched_off": backend_off,
        "fallback": config.fallback,
        "config_path": str(sdk_config_path(root, env)),
    }
    if config.provider == "sprites":
        status["sprites"] = {
            "backend": config.backend,
            "app_name": config.app_name or None,
            "preset": config.preset,
        }
    if PROVIDER_ENV in env:
        status["overridden_by"] = (
            f"{PROVIDER_ENV}={env[PROVIDER_ENV]} takes precedence over the config file"
        )
    return status


def set_offloading(
    root: Path, env: Mapping[str, str], *, enabled: bool, operations: bool = True
) -> Path | None:
    """Write the switch into the SDK config; None when off and nothing is configured."""
    path = sdk_config_path(root, env)
    # Strict: a config that fails to parse must never be rewritten from scratch.
    config = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(config, dict):
        raise click.ClickException(f"{path} must contain a JSON object")
    if not enabled and "backtest_runner" not in config:
        return None
    section = dict(config.get("backtest_runner") or {})
    section["provider"] = "sprites" if enabled else "local"
    if enabled:
        section["offload_operations"] = operations
    updated = {**config, "backtest_runner": section}
    # The file's own settings must hold up without the environment's provider override.
    file_env = {key: value for key, value in env.items() if key != PROVIDER_ENV}
    try:
        load_runner_config(repo_root=root, config=updated, environ=file_env)
    except ValueError as exc:
        raise click.ClickException(f"Not saved: {exc}") from exc
    return write_config_json(path, updated)


def _echo(status: dict[str, Any]) -> None:
    click.echo(json.dumps(status, indent=2))
    if "overridden_by" in status:
        click.echo(f"Warning: {status['overridden_by']}.", err=True)


@click.group(
    name="offload",
    help="Turn remote (Sprite) offloading of backtest compute on or off.",
)
def offload_cli() -> None:
    pass


@offload_cli.command(name="status", help="Show where backtest compute runs.")
def status_cmd() -> None:
    _echo(offload_status(JobStore().repo_root, os.environ))


@offload_cli.command(
    name="on",
    help="Offload evolution campaigns and standalone operations to Sprites.",
)
@click.option(
    "--campaigns-only",
    is_flag=True,
    default=False,
    help="Offload evolution campaigns only; standalone backtests, scans and "
    "experiments stay on this machine.",
)
def on_cmd(campaigns_only: bool) -> None:
    root = JobStore().repo_root
    set_offloading(root, os.environ, enabled=True, operations=not campaigns_only)
    _echo(offload_status(root, os.environ))


@offload_cli.command(
    name="off",
    help="Compute everything on this machine. Open leases end at their idle timeout.",
)
def off_cmd() -> None:
    root = JobStore().repo_root
    set_offloading(root, os.environ, enabled=False)
    _echo(offload_status(root, os.environ))
