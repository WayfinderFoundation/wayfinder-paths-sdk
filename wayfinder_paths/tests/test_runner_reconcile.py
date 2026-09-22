from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from wayfinder_paths.core.clients.ScheduledJobsClient import SCHEDULED_JOBS_CLIENT
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


def _stub_sync_child(
    daemon: RunnerDaemon,
    monkeypatch: pytest.MonkeyPatch,
    *,
    exitcode: int | None = 0,
    events: list[str] | None = None,
) -> None:
    """Stand in for the forkserver seam: no child is forked in unit tests."""

    def _child() -> int | None:
        if events is not None:
            events.append("child")
        return exitcode

    monkeypatch.setattr(daemon, "_run_backend_sync_child", _child)


def _capture_logs(records: list[str]) -> int:
    from loguru import logger

    return logger.add(lambda message: records.append(str(message)), level="INFO")


def test_sync_to_backend_delivers_full_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercises _sync_to_backend_async end-to-end, not a hand-built payload:
    the registry bulk_sync runs in-process, then the wayfinder-jobs child."""
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-xyz")

    daemon = RunnerDaemon(paths=_paths(tmp_path))
    _add_local(daemon, "job-a")
    _add_local(daemon, "job-b")

    synced: list[list[dict]] = []
    events: list[str] = []

    def _bulk_sync(jobs: list[dict]) -> None:
        synced.append(jobs)
        events.append("bulk")

    monkeypatch.setattr(SCHEDULED_JOBS_CLIENT, "bulk_sync", _bulk_sync)
    _stub_sync_child(daemon, monkeypatch, events=events)

    daemon._sync_to_backend_async()

    assert _wait_for(lambda: events == ["bulk", "child"])
    names = {j["job_name"] for j in synced[0]}
    assert names == {"job-a", "job-b"}
    row = next(j for j in synced[0] if j["job_name"] == "job-a")
    assert row["status"] == JobStatus.ACTIVE
    assert row["interval_seconds"] == 60
    assert row["payload"] == {"script_path": "x.py"}


def test_sync_does_not_use_the_daemons_shared_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sync thread must read through its own connection: poisoning the
    daemon's shared one must not affect delivery."""
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-xyz")

    daemon = RunnerDaemon(paths=_paths(tmp_path))
    _add_local(daemon, "job-a")

    def _poisoned(*args, **kwargs):
        raise sqlite3.ProgrammingError("shared connection used across threads")

    monkeypatch.setattr(daemon._db, "list_jobs", _poisoned)
    monkeypatch.setattr(daemon._db, "get_job", _poisoned)

    synced: list[list[dict]] = []
    events: list[str] = []

    def _bulk_sync(jobs: list[dict]) -> None:
        synced.append(jobs)
        events.append("bulk")

    monkeypatch.setattr(SCHEDULED_JOBS_CLIENT, "bulk_sync", _bulk_sync)
    _stub_sync_child(daemon, monkeypatch, events=events)

    daemon._sync_to_backend_async()

    assert _wait_for(lambda: events == ["bulk", "child"])
    assert {j["job_name"] for j in synced[0]} == {"job-a"}


def test_sync_child_failure_warns_without_the_payload_or_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from loguru import logger

    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-xyz")
    daemon = RunnerDaemon(paths=_paths(tmp_path))
    _add_local(daemon, "job-a")
    monkeypatch.setattr(SCHEDULED_JOBS_CLIENT, "bulk_sync", lambda jobs: None)
    _stub_sync_child(daemon, monkeypatch, exitcode=1)

    records: list[str] = []
    sink_id = _capture_logs(records)
    try:
        daemon._sync_to_backend_async()
        assert _wait_for(
            lambda: any("Backend sync child failed (exit=1)" in r for r in records)
        )
    finally:
        logger.remove(sink_id)
    failure = next(r for r in records if "Backend sync child failed" in r)
    assert "WARNING" in failure
    assert not any('"jobs"' in r or "Traceback" in r for r in records)


def test_sync_child_timeout_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from loguru import logger

    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-xyz")
    daemon = RunnerDaemon(paths=_paths(tmp_path))
    monkeypatch.setattr(SCHEDULED_JOBS_CLIENT, "bulk_sync", lambda jobs: None)
    _stub_sync_child(daemon, monkeypatch, exitcode=None)

    records: list[str] = []
    sink_id = _capture_logs(records)
    try:
        daemon._sync_to_backend_async()
        assert _wait_for(
            lambda: any(
                "Backend sync child timed out after 300s; killed" in r
                and "WARNING" in r
                for r in records
            )
        )
    finally:
        logger.remove(sink_id)


def test_sync_child_spawn_failure_falls_back_to_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from loguru import logger

    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-xyz")
    daemon = RunnerDaemon(paths=_paths(tmp_path))
    monkeypatch.setattr(SCHEDULED_JOBS_CLIENT, "bulk_sync", lambda jobs: None)

    def _no_forkserver() -> int | None:
        raise RuntimeError("forkserver is dead")

    monkeypatch.setattr(daemon, "_run_backend_sync_child", _no_forkserver)
    stores: list = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.sync.sync_all_jobs",
        lambda **kwargs: stores.append(kwargs["store"]),
    )

    records: list[str] = []
    sink_id = _capture_logs(records)
    try:
        daemon._sync_to_backend_async()
        assert _wait_for(lambda: len(stores) == 1)
    finally:
        logger.remove(sink_id)
    assert stores[0].repo_root == tmp_path.resolve()
    assert any(
        "Backend sync child spawn failed; syncing in-process" in r and "WARNING" in r
        for r in records
    )


def test_side_effect_failure_logs_at_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Side-effect crashes must surface at WARNING, not debug."""
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


def test_reported_run_log_is_capped_to_the_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """report_run ships a bounded tail of the run log, never the whole file."""
    daemon = RunnerDaemon(paths=_paths(tmp_path))
    log_path = daemon._paths.logs_dir / "job-a" / "1.log"
    log_path.parent.mkdir(parents=True)
    lines = [f"line {index:07d} " + "x" * 90 for index in range(10_500)]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert log_path.stat().st_size > 1_000_000

    reported: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        SCHEDULED_JOBS_CLIENT,
        "report_run",
        lambda name, data: reported.append((name, data)),
    )
    now = int(time.time())
    rp = RunningProcess(
        run_id=1,
        job_id=1,
        job_name="job-a",
        started_at=now,
        reason="schedule",
        scheduled_for=now,
        timeout_seconds=None,
        popen=Mock(pid=1),
        log_path=log_path,
    )

    daemon._report_finished_run(rp, finished_at=now, status=RunStatus.OK, exit_code=0)

    (_name, data) = reported[0]
    assert len(data["log_output"].encode("utf-8")) <= 200_000
    assert data["log_output"].endswith(lines[-1])
