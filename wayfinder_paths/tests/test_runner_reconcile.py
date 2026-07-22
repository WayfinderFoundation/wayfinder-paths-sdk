"""The runner daemon keeps the backend fresh with TWO pushes per sync.

1. Scheduled-jobs registry (ScheduledJobsClient.bulk_sync): registers each
   runner job so the backend accepts its per-run reports — report_run 404s for
   unregistered jobs, which empties the Strategies UI Activity tab. (#520
   removed this push believing /jobs/sync/ was dead; the endpoint is alive.)
2. Wayfinder-jobs snapshot (sync_all_jobs): conversations, proposals, and the
   reconciled scorecard/mode, so the UI doesn't go stale between agent wakes.
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


def test_sync_pushes_registry_and_wayfinder_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-xyz")
    daemon = RunnerDaemon(paths=_paths(tmp_path))
    _add_local(daemon, "job-a")
    _add_local(daemon, "job-b")

    stores: list[object] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.sync.sync_all_jobs",
        lambda *, store: stores.append(store),
    )
    registries: list[list[dict]] = []
    monkeypatch.setattr(
        "wayfinder_paths.runner.daemon.SCHEDULED_JOBS_CLIENT.bulk_sync",
        lambda jobs: registries.append(jobs),
    )
    # Run the side-effect callback inline so the assertion is deterministic.
    monkeypatch.setattr(daemon, "_run_side_effect", lambda _label, cb: cb())

    daemon._sync_to_backend_async()

    assert len(stores) == 1
    # The store is rooted at the daemon's repo_root so it resolves the job dirs.
    assert stores[0].repo_root == tmp_path.resolve()
    # Registry push carries every local runner job so subsequent report_run
    # calls do not 404 (the Activity-tab regression from #520).
    assert len(registries) == 1
    assert {j["job_name"] for j in registries[0]} == {"job-a", "job-b"}
    sample = next(j for j in registries[0] if j["job_name"] == "job-a")
    assert sample["job_type"] == JOB_TYPE_SCRIPT
    assert sample["interval_seconds"] == 60
    assert sample["payload"] == {"script_path": "x.py"}


def test_sync_noop_when_not_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENCODE_INSTANCE_ID", raising=False)
    daemon = RunnerDaemon(paths=_paths(tmp_path))
    _add_local(daemon, "job-a")

    stores: list[object] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.sync.sync_all_jobs",
        lambda *, store: stores.append(store),
    )
    registries: list[list[dict]] = []
    monkeypatch.setattr(
        "wayfinder_paths.runner.daemon.SCHEDULED_JOBS_CLIENT.bulk_sync",
        lambda jobs: registries.append(jobs),
    )
    monkeypatch.setattr(daemon, "_run_side_effect", lambda _label, cb: cb())

    daemon._sync_to_backend_async()

    assert stores == []
    assert registries == []


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
