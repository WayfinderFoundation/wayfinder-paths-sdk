from __future__ import annotations

import time
from pathlib import Path

from wayfinder_paths.runner.constants import (
    JOB_TYPE_STRATEGY,
    JobStatus,
)
from wayfinder_paths.runner.db import RunnerDB


def test_runner_db_add_and_due_jobs(tmp_path: Path) -> None:
    db = RunnerDB(tmp_path / "state.db")
    job_id = db.add_job(
        name="basis-update",
        job_type=JOB_TYPE_STRATEGY,
        payload={
            "strategy": "basis_trading_strategy",
            "action": "update",
            "config": "config.json",
        },
        interval_seconds=600,
        status=JobStatus.ACTIVE,
        next_run_at=int(time.time()) - 1,
    )
    assert isinstance(job_id, int) and job_id > 0

    jobs = db.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["name"] == "basis-update"
    assert jobs[0]["status"] == JobStatus.ACTIVE

    due = db.due_jobs(now=int(time.time()))
    assert len(due) == 1
    assert due[0]["id"] == job_id


def test_runner_db_failure_circuit_breaker(tmp_path: Path) -> None:
    db = RunnerDB(tmp_path / "state.db")
    job_id = db.add_job(
        name="job",
        job_type=JOB_TYPE_STRATEGY,
        payload={"strategy": "basis_trading_strategy"},
        interval_seconds=10,
        status=JobStatus.ACTIVE,
        next_run_at=int(time.time()),
    )

    failures, status = db.record_job_failure(
        job_id=job_id, error_text="boom", max_failures=2
    )
    assert failures == 1
    assert status == JobStatus.ACTIVE

    failures, status = db.record_job_failure(
        job_id=job_id, error_text="boom2", max_failures=2
    )
    assert failures == 2
    assert status == JobStatus.ERROR


def test_runner_db_delete_job_cascades_state_and_runs(tmp_path: Path) -> None:
    db = RunnerDB(tmp_path / "state.db")
    now = int(time.time())
    job_id = db.add_job(
        name="job",
        job_type=JOB_TYPE_STRATEGY,
        payload={"strategy": "basis_trading_strategy"},
        interval_seconds=10,
        status=JobStatus.ACTIVE,
        next_run_at=now,
    )

    run_id = db.create_run(job_id=job_id, started_at=now)
    assert db.get_run(run_id=run_id) is not None

    db.delete_job(name="job")
    assert db.list_jobs() == []
    assert db.get_run(run_id=run_id) is None
