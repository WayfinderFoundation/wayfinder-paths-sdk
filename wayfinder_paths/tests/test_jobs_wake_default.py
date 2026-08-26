"""Enabled agent loops always compile with a schedule.

Motivating incident: jobs created with agent_mode off carry
wake_interval_seconds=null; a later `agent set-mode monitor` (no --wake)
enabled the loop with no schedule, and registering it raised "provide
exactly one of interval_seconds or cron_expr" AFTER the script loop had
registered but BEFORE runner_links.json was written — a half-scheduled job
nothing ever retried. The compiler now defaults the null wake (900s auto /
3600s otherwise, matching WayfinderJob.new), and both set-mode paths
backfill the default into job.yaml when enabling a wake-less, cron-less
loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.models import WayfinderJob, default_wake_seconds
from wayfinder_paths.jobs.store import JobStore


def test_compiler_defaults_null_wake_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    recorded: list[dict[str, Any]] = []

    class FakeBridge:
        def __init__(self, *, repo_root=None):  # noqa: ANN001
            self.repo_root = repo_root

        def add_or_update_script_job(self, **kwargs):  # noqa: ANN003
            recorded.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr("wayfinder_paths.jobs.compiler.RunnerBridge", FakeBridge)
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "wake-null",
        script="workspace/src/strategy.py",
        interval_seconds=3600,
        agent_mode="off",
        execution_contract="jobs_v1",
    )
    # A set-mode without --wake used to leave exactly this shape behind.
    job.agent_loop.enabled = True
    job.agent_loop.mode = "monitor"
    store.save(job)

    payload = JobCompiler(store=store).compile(job, start_daemon=False)

    agent = next(k for k in recorded if k["name"].endswith("-agent"))
    assert agent["interval_seconds"] == 3600
    assert {item["loop"] for item in payload["jobs"]} == {"script", "agent"}


def test_default_wake_seconds_by_mode():
    assert default_wake_seconds("auto") == 900
    assert default_wake_seconds("monitor") == 3600
    assert default_wake_seconds("intervene") == 3600
