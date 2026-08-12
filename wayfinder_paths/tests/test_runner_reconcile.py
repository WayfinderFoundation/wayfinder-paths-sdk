from __future__ import annotations

import sqlite3
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


def _wait_for(predicate, timeout_s: float = 5.0) -> bool:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_sync_to_backend_delivers_full_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through _sync_to_backend_async (the earlier tests hand-built
    the payload and never exercised the async path that was silently dying)."""
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-xyz")

    daemon = RunnerDaemon(paths=_paths(tmp_path))
    _add_local(daemon, "job-a")
    _add_local(daemon, "job-b")

    synced: list[list[dict]] = []
    monkeypatch.setattr(
        SCHEDULED_JOBS_CLIENT, "bulk_sync", lambda jobs: synced.append(jobs)
    )

    daemon._sync_to_backend_async()

    assert _wait_for(lambda: len(synced) == 1)
    names = {j["job_name"] for j in synced[0]}
    assert names == {"job-a", "job-b"}
    row = next(j for j in synced[0] if j["job_name"] == "job-a")
    assert row["status"] == JobStatus.ACTIVE
    assert row["interval_seconds"] == 60
    assert row["payload"] == {"script_path": "x.py"}


def test_sync_does_not_use_the_daemons_shared_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the sync thread used self._db — one sqlite connection
    shared with the 1s scheduler loop and the control server — and died
    mid-read on every sync, before bulk_sync could POST or log. The backend
    mirror froze for a week on a production box. The sync must read through
    a private connection, so poisoning the shared one must not matter."""
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-xyz")

    daemon = RunnerDaemon(paths=_paths(tmp_path))
    _add_local(daemon, "job-a")

    def _poisoned(*args, **kwargs):
        raise sqlite3.ProgrammingError("shared connection used across threads")

    monkeypatch.setattr(daemon._db, "list_jobs", _poisoned)
    monkeypatch.setattr(daemon._db, "get_job", _poisoned)

    synced: list[list[dict]] = []
    monkeypatch.setattr(
        SCHEDULED_JOBS_CLIENT, "bulk_sync", lambda jobs: synced.append(jobs)
    )

    daemon._sync_to_backend_async()

    assert _wait_for(lambda: len(synced) == 1)
    assert {j["job_name"] for j in synced[0]} == {"job-a"}


def test_side_effect_failure_logs_at_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: side-effect crashes were logged at DEBUG — invisible at the
    daemon's INFO level — which is how the dying sync went unnoticed."""
    from loguru import logger

    daemon = RunnerDaemon(paths=_paths(tmp_path))

    records: list[str] = []
    sink_id = logger.add(lambda message: records.append(str(message)), level="WARNING")
    try:
        daemon._run_side_effect(
            "explode", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        assert _wait_for(lambda: any("explode" in r for r in records))
    finally:
        logger.remove(sink_id)
    assert any("Runner side effect explode failed" in r for r in records)
