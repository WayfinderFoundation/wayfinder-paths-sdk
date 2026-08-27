"""Daemon workload caps: new registrations over the limit are rejected at
ctl_add_job. Strategies = distinct WAYFINDER_HIGH_LEVEL_JOB_ID in wrapper
env; anything else is a legacy runner job (watchdog exempt). Existing names
and running jobs are untouched — over-cap fleets are grandfathered."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wayfinder_paths.runner import daemon as daemon_mod
from wayfinder_paths.runner.db import RunnerDB
from wayfinder_paths.runner.paths import get_runner_paths


def _daemon(tmp_path: Path) -> daemon_mod.RunnerDaemon:
    daemon = daemon_mod.RunnerDaemon.__new__(daemon_mod.RunnerDaemon)
    daemon._paths = get_runner_paths(repo_root=tmp_path)
    daemon._db = RunnerDB(daemon._paths.db_path)
    daemon._sync_to_backend_async = lambda: None
    daemon._bind_runner_session_async = lambda name: None
    return daemon


def _add(daemon, name: str, env: dict[str, str]) -> dict[str, Any]:
    runs = daemon._paths.repo_root / ".wayfinder_runs"
    runs.mkdir(exist_ok=True)
    (runs / f"{name}.py").write_text("print()\n")
    return daemon.ctl_add_job(
        name=name,
        job_type="script",
        payload={"script_path": f".wayfinder_runs/{name}.py", "env": env},
        interval_seconds=3600,
    )


def _strategy_env(job_id: str) -> dict[str, str]:
    return {"WAYFINDER_HIGH_LEVEL_JOB_ID": job_id}


def test_strategy_cap_blocks_third_strategy(tmp_path: Path):
    daemon = _daemon(tmp_path)
    assert _add(daemon, "a-script", _strategy_env("a"))["ok"]
    assert _add(daemon, "a-agent", _strategy_env("a"))["ok"]  # same strategy
    assert _add(daemon, "b-script", _strategy_env("b"))["ok"]

    refused = _add(daemon, "c-script", _strategy_env("c"))
    assert not refused["ok"]
    assert "active strategy limit" in refused["error"]

    # A second loop of an already-active strategy is never a new slot.
    assert _add(daemon, "b-agent", _strategy_env("b"))["ok"]


def test_pause_frees_a_slot(tmp_path: Path):
    daemon = _daemon(tmp_path)
    assert _add(daemon, "a-script", _strategy_env("a"))["ok"]
    assert _add(daemon, "b-script", _strategy_env("b"))["ok"]
    daemon.ctl_pause_job(name="b-script")
    assert _add(daemon, "c-script", _strategy_env("c"))["ok"]


def test_legacy_cap_and_watchdog_exemption(tmp_path: Path):
    daemon = _daemon(tmp_path)
    for i in range(3):
        assert _add(daemon, f"user-job-{i}", {})["ok"]

    refused = _add(daemon, "user-job-3", {})
    assert not refused["ok"]
    assert "active runner-job limit" in refused["error"]

    # The watchdog and strategy wrappers don't consume legacy slots.
    assert _add(daemon, "watchdog", {"WAYFINDER_WATCHDOG": "1"})["ok"]
    assert _add(daemon, "s-script", _strategy_env("s"))["ok"]

    # Existing names are never capped (recompiles must keep working).
    assert daemon.ctl_update_job(name="user-job-0", payload=None)["ok"]
