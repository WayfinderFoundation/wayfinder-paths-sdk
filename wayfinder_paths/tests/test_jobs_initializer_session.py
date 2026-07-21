"""The creating agent's opencode session is captured on the job spec.

The Strategies UI re-enters "the conversation that built this strategy" from
spec.controller.initializer_session_id — synced to the backend as part of the
whole spec, so this is the single place the linkage is born.
"""

from __future__ import annotations

from pathlib import Path

from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def test_new_captures_initializer_session_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_abc123")
    job = WayfinderJob.new("init-capture", interval_seconds=3600)
    assert job.controller["initializer_session_id"] == "ses_abc123"
    assert job.controller["created_at"]


def test_new_accepts_legacy_env_spelling(monkeypatch) -> None:
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    monkeypatch.setenv("OPENCODE_SESSIONID", "ses_legacy9")
    job = WayfinderJob.new("init-legacy", interval_seconds=3600)
    assert job.controller["initializer_session_id"] == "ses_legacy9"


def test_new_leaves_controller_empty_without_env(monkeypatch) -> None:
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    monkeypatch.delenv("OPENCODE_SESSIONID", raising=False)
    job = WayfinderJob.new("init-none", interval_seconds=3600)
    assert job.controller == {}


def test_new_sanitizes_session_id_charset(monkeypatch) -> None:
    # A hostile parent-process env can't smuggle newlines/quotes/spaces into
    # the spec that syncs downstream — only [A-Za-z0-9_-] survives.
    monkeypatch.setenv("OPENCODE_SESSION_ID", 'ses_abc\ninjected: "x" /../')
    job = WayfinderJob.new("init-dirty", interval_seconds=3600)
    assert job.controller["initializer_session_id"] == "ses_abcinjectedx"


def test_new_drops_all_junk_session_id(monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_SESSION_ID", "\n  \t !@#")
    job = WayfinderJob.new("init-junk", interval_seconds=3600)
    assert job.controller == {}


def test_controller_round_trips_through_yaml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_roundtrip")
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("init-roundtrip", script="strategy.py")
    store.create_job(job)

    loaded = store.load("init-roundtrip")

    assert loaded.controller["initializer_session_id"] == "ses_roundtrip"
    assert loaded.to_dict()["controller"]["initializer_session_id"] == ("ses_roundtrip")


def test_mcp_create_result_carries_initializer(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    from wayfinder_paths.mcp.tools import jobs as jobs_tools
    from wayfinder_paths.tests.test_wayfinder_jobs import _FakeBridge

    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_mcp42")
    monkeypatch.setattr("wayfinder_paths.jobs.compiler.RunnerBridge", _FakeBridge)
    monkeypatch.setattr(jobs_tools, "JobStore", lambda: JobStore(repo_root=tmp_path))
    monkeypatch.setattr(jobs_tools, "sync_all_jobs", lambda store=None: None)

    result = asyncio.run(
        jobs_tools.core_jobs(
            action="create",
            job_id="init-mcp",
            script="strategy.py",
            interval_seconds=3600,
        )
    )

    assert result["ok"], result
    controller = result["result"]["job"]["controller"]
    assert controller["initializer_session_id"] == "ses_mcp42"
