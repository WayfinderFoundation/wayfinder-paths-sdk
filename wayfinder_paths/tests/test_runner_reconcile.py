from __future__ import annotations

from pathlib import Path

import pytest

from wayfinder_paths.core.clients.ScheduledJobsClient import SCHEDULED_JOBS_CLIENT
from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT, JobStatus
from wayfinder_paths.runner.daemon import RunnerDaemon
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


def test_bulk_sync_sends_all_local_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-xyz")

    daemon = RunnerDaemon(paths=_paths(tmp_path))
    _add_local(daemon, "job-a")
    _add_local(daemon, "job-b")

    synced: list[list[dict]] = []
    monkeypatch.setattr(
        SCHEDULED_JOBS_CLIENT, "bulk_sync", lambda jobs: synced.append(jobs)
    )

    jobs = []
    for j in daemon._db.list_jobs():
        job, state = daemon._db.get_job(name=j["name"])
        jobs.append(
            {
                "job_name": job.name,
                "job_type": job.type,
                "status": state.status,
                "interval_seconds": job.interval_seconds,
                "payload": job.payload,
            }
        )
    SCHEDULED_JOBS_CLIENT.bulk_sync(jobs)

    assert len(synced) == 1
    names = {j["job_name"] for j in synced[0]}
    assert names == {"job-a", "job-b"}


def test_bulk_sync_noop_when_not_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENCODE_INSTANCE_ID", raising=False)

    daemon = RunnerDaemon(paths=_paths(tmp_path))
    _add_local(daemon, "job-a")

    called = False

    def _fail(jobs):
        nonlocal called
        called = True

    monkeypatch.setattr(SCHEDULED_JOBS_CLIENT, "bulk_sync", _fail)

    daemon._sync_to_backend_async()

    assert not called


def test_bulk_sync_empty_when_no_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-xyz")

    daemon = RunnerDaemon(paths=_paths(tmp_path))

    synced: list[list[dict]] = []
    monkeypatch.setattr(
        SCHEDULED_JOBS_CLIENT, "bulk_sync", lambda jobs: synced.append(jobs)
    )

    jobs = []
    for j in daemon._db.list_jobs():
        job, state = daemon._db.get_job(name=j["name"])
        jobs.append(
            {
                "job_name": job.name,
                "job_type": job.type,
                "status": state.status,
                "interval_seconds": job.interval_seconds,
                "payload": job.payload,
            }
        )
    SCHEDULED_JOBS_CLIENT.bulk_sync(jobs)

    assert len(synced) == 1
    assert synced[0] == []
