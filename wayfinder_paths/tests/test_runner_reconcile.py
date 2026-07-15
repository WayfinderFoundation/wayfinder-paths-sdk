"""The runner daemon keeps the wayfinder-jobs backend fresh.

The event-driven paths (agent wakes, propose, MCP core_jobs) already call
sync_all_jobs; the daemon adds the periodic/after-run push so the Strategies UI
(conversations, proposals, reconciled mode) doesn't go stale between wakes. This
replaced the daemon's dead legacy `/jobs/sync/` push (ScheduledJobsClient).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT, JobStatus, RunStatus
from wayfinder_paths.runner.daemon import RunnerDaemon, RunningProcess
from wayfinder_paths.runner.paths import RunnerPaths


def _paths(tmp_path: Path) -> RunnerPaths:
    runner_dir = tmp_path / "runner"
    runner_dir.mkdir()
    (tmp_path / "pyproject.toml").write_text("[tool.x]\n")
    return RunnerPaths(
        repo_root=tmp_path,
        runner_dir=runner_dir,
        db_path=runner_dir / "state.db",
        logs_dir=runner_dir / "logs",
        sock_path=runner_dir / "runner.sock",
    )


def _add_local(daemon: RunnerDaemon, name: str) -> None:
    daemon._db.add_job(
        name=name,
        job_type=JOB_TYPE_SCRIPT,
        payload={"script_path": "x.py"},
        interval_seconds=60,
        status=JobStatus.ACTIVE,
        next_run_at=0,
    )


def test_sync_pushes_wayfinder_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-xyz")
    daemon = RunnerDaemon(paths=_paths(tmp_path))

    stores: list[object] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.sync.sync_all_jobs",
        lambda *, store: stores.append(store),
    )
    # Run the side-effect callback inline so the assertion is deterministic.
    monkeypatch.setattr(daemon, "_run_side_effect", lambda _label, cb: cb())

    daemon._sync_to_backend_async()

    assert len(stores) == 1
    # The store is rooted at the daemon's repo_root so it resolves the job dirs.
    assert stores[0].repo_root == tmp_path.resolve()


def test_sync_noop_when_not_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENCODE_INSTANCE_ID", raising=False)
    daemon = RunnerDaemon(paths=_paths(tmp_path))

    stores: list[object] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.sync.sync_all_jobs",
        lambda *, store: stores.append(store),
    )
    monkeypatch.setattr(daemon, "_run_side_effect", lambda _label, cb: cb())

    daemon._sync_to_backend_async()

    assert stores == []


def test_finish_run_triggers_backend_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = RunnerDaemon(paths=_paths(tmp_path))
    _add_local(daemon, "job-a")
    job, _ = daemon._db.get_job(name="job-a")
    run_id = daemon._db.reserve_run(
        job_id=job.id, started_at=0, next_run_at=60, reason="schedule", scheduled_for=0
    )
    rp = RunningProcess(
        run_id=run_id,
        job_id=job.id,
        job_name="job-a",
        started_at=0,
        reason="schedule",
        scheduled_for=0,
        timeout_seconds=None,
        popen=Mock(),
        log_path=tmp_path / "x.log",
    )
    daemon._running[run_id] = rp

    synced = Mock()
    monkeypatch.setattr(daemon, "_sync_to_backend_async", synced)
    # Neutralize the notify/report side-effect threads for a focused assertion.
    monkeypatch.setattr(daemon, "_run_side_effect", lambda _label, _cb: None)

    daemon._finish_run(
        rp, finished_at=100, status=RunStatus.OK, exit_code=0, error_text=None
    )

    synced.assert_called_once()
