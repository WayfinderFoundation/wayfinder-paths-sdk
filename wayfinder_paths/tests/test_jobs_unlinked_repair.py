"""Watchdog unlinked-loop repair + compiler wake-interval default.

Motivating incident: three self-built jobs sat 38-40h with hourly script
ticks running but runner_links.json stuck at the init seed `{"jobs": []}` and
their agent loops never scheduled. The compile that should have written the
links crashed between registering the script loop and the agent loop — the
agent loop was enabled by a set-mode with wake_interval_seconds still null,
and registering a schedule-less job raises. Nothing ever retried: create and
set-mode had already returned.

Two fixes under test: the compiler defaults a null wake interval on an
enabled agent loop (so a recompile cannot re-crash the same way), and the
watchdog recompiles any job whose enabled loops are missing from its links.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.watchdog import recover_stalled_applications


def _journal_events(store: JobStore, job_id: str, event_type: str) -> list[dict]:
    path = store.job_dir(job_id) / "journal.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return [row for row in rows if row.get("type") == event_type]


def _make_job(
    store: JobStore,
    job_id: str,
    *,
    agent_mode: str = "monitor",
    age: timedelta = timedelta(hours=1),
) -> WayfinderJob:
    job = WayfinderJob.new(
        job_id,
        script="workspace/src/strategy.py",
        interval_seconds=3600,
        agent_mode=agent_mode,
        execution_contract="jobs_v1",
    )
    job.created_at = (datetime.now(UTC) - age).isoformat()
    store.save(job)
    store.write_json(job.id, "runner_links.json", {"jobs": []})
    return job


@pytest.fixture
def compiles(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    class FakeCompiler:
        def __init__(self, *, store=None):  # noqa: ANN001
            self.store = store

        def compile(self, job, *, start_daemon=True):  # noqa: ANN001
            calls.append(job.id)
            payload = {
                "job_id": job.id,
                "jobs": [{"loop": "script"}, {"loop": "agent"}],
            }
            self.store.write_json(job.id, "runner_links.json", payload)
            return payload

    monkeypatch.setattr("wayfinder_paths.jobs.watchdog.JobCompiler", FakeCompiler)
    return calls


def test_watchdog_recompiles_unlinked_loops(tmp_path: Path, compiles: list[str]):
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "half-compiled")

    report = recover_stalled_applications(store=store)

    assert compiles == [job.id]
    events = [
        event
        for event in report["recovered"]
        if event.get("action") == "unlinked_loops_recompiled"
    ]
    assert events == [
        {
            "job_id": job.id,
            "action": "unlinked_loops_recompiled",
            "missing": ["agent", "script"],
        }
    ]
    assert len(_journal_events(store, job.id, "unlinked_loops_recompiled")) == 1

    # Repaired links present → the next pass leaves the job alone.
    recover_stalled_applications(store=store)
    assert compiles == [job.id]


def test_watchdog_unlinked_repair_skips_fresh_and_paused(
    tmp_path: Path, compiles: list[str]
):
    store = JobStore(repo_root=tmp_path)
    # Fresh job: a create may still be mid-flight and about to compile.
    _make_job(store, "fresh", age=timedelta(minutes=1))
    # Paused before first compile: pause wins, repair must not re-schedule.
    paused = _make_job(store, "paused")
    store.write_json(paused.id, "scorecard.json", {"paused": True})

    recover_stalled_applications(store=store)

    assert compiles == []


def test_watchdog_unlinked_repair_journals_failure_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[str] = []

    class BrokenCompiler:
        def __init__(self, *, store=None):  # noqa: ANN001
            self.store = store

        def compile(self, job, *, start_daemon=True):  # noqa: ANN001
            calls.append(job.id)
            raise RuntimeError("runner daemon unreachable")

    monkeypatch.setattr("wayfinder_paths.jobs.watchdog.JobCompiler", BrokenCompiler)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "still-broken")

    recover_stalled_applications(store=store)
    recover_stalled_applications(store=store)

    # Retried every pass, but the identical failure journals only once.
    assert calls == [job.id, job.id]
    assert len(_journal_events(store, job.id, "unlinked_loops_compile_failed")) == 1


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
