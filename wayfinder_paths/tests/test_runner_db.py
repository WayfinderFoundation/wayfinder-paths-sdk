from __future__ import annotations

import sqlite3
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
    assert due[0]["schedule_kind"] == "interval"
    assert due[0]["cron_expr"] is None
    assert due[0]["timezone"] == "UTC"


def test_runner_db_adds_cron_job(tmp_path: Path) -> None:
    db = RunnerDB(tmp_path / "state.db")
    job_id = db.add_job(
        name="daily-job",
        job_type=JOB_TYPE_STRATEGY,
        payload={"strategy": "basis_trading_strategy"},
        interval_seconds=0,
        schedule_kind="cron",
        cron_expr="0 9 * * *",
        timezone="America/Toronto",
        status=JobStatus.ACTIVE,
        next_run_at=1_704_204_000,
    )

    job, state = db.get_job(name="daily-job")

    assert job.id == job_id
    assert job.interval_seconds == 0
    assert job.schedule_kind == "cron"
    assert job.cron_expr == "0 9 * * *"
    assert job.timezone == "America/Toronto"
    assert state.next_run_at == 1_704_204_000


def test_runner_db_clears_cron_when_switching_to_interval(tmp_path: Path) -> None:
    db = RunnerDB(tmp_path / "state.db")
    db.add_job(
        name="scheduled-job",
        job_type=JOB_TYPE_STRATEGY,
        payload={"strategy": "basis_trading_strategy"},
        interval_seconds=0,
        schedule_kind="cron",
        cron_expr="0 9 * * *",
        timezone="America/Toronto",
        status=JobStatus.ACTIVE,
        next_run_at=1_704_204_000,
    )

    db.update_job(
        name="scheduled-job",
        interval_seconds=600,
        schedule_kind="interval",
        clear_cron_expr=True,
        timezone="UTC",
    )
    job, _ = db.get_job(name="scheduled-job")

    assert job.schedule_kind == "interval"
    assert job.interval_seconds == 600
    assert job.cron_expr is None
    assert job.timezone == "UTC"


def test_runner_db_migrates_existing_interval_db(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE job_defs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          type TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          interval_seconds INTEGER NOT NULL,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL
        );
        CREATE TABLE job_state (
          job_id INTEGER PRIMARY KEY,
          status TEXT NOT NULL,
          next_run_at INTEGER NOT NULL,
          last_run_at INTEGER,
          last_ok_at INTEGER,
          consecutive_failures INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,
          FOREIGN KEY(job_id) REFERENCES job_defs(id) ON DELETE CASCADE
        );
        CREATE TABLE runs (
          run_id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id INTEGER NOT NULL,
          started_at INTEGER NOT NULL,
          finished_at INTEGER,
          status TEXT NOT NULL,
          exit_code INTEGER,
          log_path TEXT,
          summary_json TEXT,
          pid INTEGER,
          FOREIGN KEY(job_id) REFERENCES job_defs(id) ON DELETE CASCADE
        );
        INSERT INTO job_defs(name, type, payload_json, interval_seconds, created_at, updated_at)
        VALUES ('old-job', 'strategy', '{"strategy":"basis_trading_strategy"}', 600, 1, 1);
        INSERT INTO job_state(job_id, status, next_run_at, consecutive_failures)
        VALUES (1, 'active', 123, 0);
        INSERT INTO runs(job_id, started_at, status) VALUES (1, 100, 'RUNNING');
        """
    )
    conn.commit()
    conn.close()

    db = RunnerDB(db_path)
    job, state = db.get_job(name="old-job")
    run = db.get_run(run_id=1)

    assert job.schedule_kind == "interval"
    assert job.interval_seconds == 600
    assert job.cron_expr is None
    assert job.timezone == "UTC"
    assert state.next_run_at == 123
    assert run is not None
    assert run["reason"] is None
    assert run["scheduled_for"] is None


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


def test_runner_db_reserve_run_advances_schedule_atomically(tmp_path: Path) -> None:
    db = RunnerDB(tmp_path / "state.db")
    job_id = db.add_job(
        name="job",
        job_type=JOB_TYPE_STRATEGY,
        payload={"strategy": "basis_trading_strategy"},
        interval_seconds=10,
        status=JobStatus.ACTIVE,
        next_run_at=100,
    )

    run_id = db.reserve_run(
        job_id=job_id,
        started_at=100,
        next_run_at=110,
        reason="schedule",
        scheduled_for=100,
    )
    _, state = db.get_job(name="job")
    run = db.get_run(run_id=run_id)

    assert state.last_run_at == 100
    assert state.next_run_at == 110
    assert run is not None
    assert run["reason"] == "schedule"
    assert run["scheduled_for"] == 100
