from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wayfinder_paths.runner.constants import JobStatus, RunStatus


@dataclass(frozen=True)
class JobRow:
    id: int
    name: str
    type: str
    payload: dict[str, Any]
    interval_seconds: int
    schedule_kind: str
    cron_expr: str | None
    timezone: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class JobStateRow:
    job_id: int
    status: str
    next_run_at: int
    last_run_at: int | None
    last_ok_at: int | None
    consecutive_failures: int
    last_error: str | None


class RunnerDB:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            timeout=10,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA foreign_keys=ON;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS job_defs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              interval_seconds INTEGER NOT NULL,
              schedule_kind TEXT NOT NULL DEFAULT 'interval',
              cron_expr TEXT,
              timezone TEXT NOT NULL DEFAULT 'UTC',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS job_state (
              job_id INTEGER PRIMARY KEY,
              status TEXT NOT NULL,
              next_run_at INTEGER NOT NULL,
              last_run_at INTEGER,
              last_ok_at INTEGER,
              consecutive_failures INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,
              FOREIGN KEY(job_id) REFERENCES job_defs(id) ON DELETE CASCADE
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
              run_id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id INTEGER NOT NULL,
              started_at INTEGER NOT NULL,
              finished_at INTEGER,
              status TEXT NOT NULL,
              exit_code INTEGER,
              log_path TEXT,
              summary_json TEXT,
              pid INTEGER,
              reason TEXT,
              scheduled_for INTEGER,
              FOREIGN KEY(job_id) REFERENCES job_defs(id) ON DELETE CASCADE
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_state_due ON job_state(status, next_run_at);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_job_status ON runs(job_id, status);"
        )
        self._ensure_column(
            "job_defs", "schedule_kind", "TEXT NOT NULL DEFAULT 'interval'"
        )
        self._ensure_column("job_defs", "cron_expr", "TEXT")
        self._ensure_column("job_defs", "timezone", "TEXT NOT NULL DEFAULT 'UTC'")
        self._ensure_column("runs", "reason", "TEXT")
        self._ensure_column("runs", "scheduled_for", "INTEGER")

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        cur = self._conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        existing = {str(row["name"]) for row in cur.fetchall()}
        if column in existing:
            return
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def mark_stale_running_runs_aborted(self, *, note: str) -> int:
        now = int(time.time())
        cur = self._conn.cursor()
        cur.execute(
            """
            UPDATE runs
            SET status = ?, finished_at = ?, summary_json = ?
            WHERE status = ?
            """,
            (RunStatus.ABORTED, now, json.dumps({"note": note}), RunStatus.RUNNING),
        )
        return cur.rowcount or 0

    def add_job(
        self,
        *,
        name: str,
        job_type: str,
        payload: dict[str, Any],
        interval_seconds: int,
        schedule_kind: str = "interval",
        cron_expr: str | None = None,
        timezone: str = "UTC",
        status: str = JobStatus.ACTIVE,
        next_run_at: int | None = None,
    ) -> int:
        now = int(time.time())
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO job_defs(
              name, type, payload_json, interval_seconds,
              schedule_kind, cron_expr, timezone,
              created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                job_type,
                json.dumps(payload),
                interval_seconds,
                schedule_kind,
                cron_expr,
                timezone,
                now,
                now,
            ),
        )
        job_id = cur.lastrowid
        cur.execute(
            """
            INSERT INTO job_state(job_id, status, next_run_at, last_run_at, last_ok_at, consecutive_failures, last_error)
            VALUES (?, ?, ?, NULL, NULL, 0, NULL)
            """,
            (job_id, status, next_run_at if next_run_at is not None else now),
        )
        return job_id

    def update_job(
        self,
        *,
        name: str,
        payload: dict[str, Any] | None = None,
        interval_seconds: int | None = None,
        schedule_kind: str | None = None,
        cron_expr: str | None = None,
        clear_cron_expr: bool = False,
        timezone: str | None = None,
    ) -> None:
        now = int(time.time())
        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [now]
        if payload is not None:
            sets.append("payload_json = ?")
            params.append(json.dumps(payload))
        if interval_seconds is not None:
            sets.append("interval_seconds = ?")
            params.append(interval_seconds)
        if schedule_kind is not None:
            sets.append("schedule_kind = ?")
            params.append(schedule_kind)
        if cron_expr is not None:
            sets.append("cron_expr = ?")
            params.append(cron_expr)
        elif clear_cron_expr:
            sets.append("cron_expr = NULL")
        if timezone is not None:
            sets.append("timezone = ?")
            params.append(timezone)
        if len(sets) == 1:
            return
        params.append(name)
        cur = self._conn.cursor()
        cur.execute(
            f"UPDATE job_defs SET {', '.join(sets)} WHERE name = ?",
            params,
        )
        if cur.rowcount == 0:
            raise KeyError(f"Job not found: {name}")

    def delete_job(self, *, name: str) -> None:
        cur = self._conn.cursor()
        cur.execute("DELETE FROM job_defs WHERE name = ?", (name,))
        if cur.rowcount == 0:
            raise KeyError(f"Job not found: {name}")

    def get_job(self, *, name: str) -> tuple[JobRow, JobStateRow] | None:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT d.*, s.status AS state_status, s.next_run_at, s.last_run_at, s.last_ok_at,
                   s.consecutive_failures, s.last_error
            FROM job_defs d
            JOIN job_state s ON s.job_id = d.id
            WHERE d.name = ?
            """,
            (name,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        job = JobRow(
            id=row["id"],
            name=row["name"],
            type=row["type"],
            payload=json.loads(row["payload_json"]),
            interval_seconds=row["interval_seconds"],
            schedule_kind=row["schedule_kind"],
            cron_expr=row["cron_expr"],
            timezone=row["timezone"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        state = JobStateRow(
            job_id=job.id,
            status=row["state_status"],
            next_run_at=row["next_run_at"],
            last_run_at=row["last_run_at"],
            last_ok_at=row["last_ok_at"],
            consecutive_failures=row["consecutive_failures"],
            last_error=row["last_error"],
        )
        return job, state

    def list_jobs(self) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT d.*, s.status AS state_status, s.next_run_at, s.last_run_at, s.last_ok_at,
                   s.consecutive_failures, s.last_error
            FROM job_defs d
            JOIN job_state s ON s.job_id = d.id
            ORDER BY d.id ASC
            """
        )
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "type": r["type"],
                "payload": json.loads(r["payload_json"]),
                "interval_seconds": r["interval_seconds"],
                "schedule_kind": r["schedule_kind"],
                "cron_expr": r["cron_expr"],
                "timezone": r["timezone"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "status": r["state_status"],
                "next_run_at": r["next_run_at"],
                "last_run_at": r["last_run_at"],
                "last_ok_at": r["last_ok_at"],
                "consecutive_failures": r["consecutive_failures"],
                "last_error": r["last_error"],
            }
            for r in cur.fetchall()
        ]

    def set_job_status(self, *, name: str, status: str) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            UPDATE job_state
            SET status = ?
            WHERE job_id = (SELECT id FROM job_defs WHERE name = ?)
            """,
            (status, name),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Job not found: {name}")

    def set_next_run_at(self, *, job_id: int, next_run_at: int) -> None:
        self._conn.cursor().execute(
            "UPDATE job_state SET next_run_at = ? WHERE job_id = ?",
            (next_run_at, job_id),
        )

    def set_job_last_run(self, *, job_id: int, last_run_at: int) -> None:
        self._conn.cursor().execute(
            "UPDATE job_state SET last_run_at = ? WHERE job_id = ?",
            (last_run_at, job_id),
        )

    def record_job_success(self, *, job_id: int, ok_at: int) -> None:
        self._conn.cursor().execute(
            """
            UPDATE job_state
            SET last_ok_at = ?, consecutive_failures = 0, last_error = NULL
            WHERE job_id = ?
            """,
            (ok_at, job_id),
        )

    def record_job_failure(
        self,
        *,
        job_id: int,
        error_text: str,
        max_failures: int,
    ) -> tuple[int, str]:
        cur = self._conn.cursor()
        cur.execute(
            """
            UPDATE job_state
            SET consecutive_failures = consecutive_failures + 1,
                last_error = ?
            WHERE job_id = ?
            """,
            (error_text, job_id),
        )
        cur.execute(
            "SELECT consecutive_failures FROM job_state WHERE job_id = ?",
            (job_id,),
        )
        failures = cur.fetchone()["consecutive_failures"]
        status = JobStatus.ACTIVE
        if failures >= max_failures:
            status = JobStatus.ERROR
            cur.execute(
                "UPDATE job_state SET status = ? WHERE job_id = ?",
                (JobStatus.ERROR, job_id),
            )
        return failures, status

    def due_jobs(self, *, now: int) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT d.id, d.name, d.type, d.payload_json, d.interval_seconds,
                   d.schedule_kind, d.cron_expr, d.timezone,
                   s.status, s.next_run_at, s.last_run_at, s.last_ok_at,
                   s.consecutive_failures, s.last_error
            FROM job_defs d
            JOIN job_state s ON s.job_id = d.id
            WHERE s.status = ? AND s.next_run_at <= ?
            ORDER BY s.next_run_at ASC, d.id ASC
            """,
            (JobStatus.ACTIVE, now),
        )
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "type": r["type"],
                "payload": json.loads(r["payload_json"]),
                "interval_seconds": r["interval_seconds"],
                "schedule_kind": r["schedule_kind"],
                "cron_expr": r["cron_expr"],
                "timezone": r["timezone"],
                "status": r["status"],
                "next_run_at": r["next_run_at"],
                "last_run_at": r["last_run_at"],
                "last_ok_at": r["last_ok_at"],
                "consecutive_failures": r["consecutive_failures"],
                "last_error": r["last_error"],
            }
            for r in cur.fetchall()
        ]

    def create_run(
        self,
        *,
        job_id: int,
        started_at: int,
        status: str = RunStatus.RUNNING,
        log_path: str | None = None,
        pid: int | None = None,
        reason: str | None = None,
        scheduled_for: int | None = None,
    ) -> int:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO runs(
              job_id, started_at, finished_at, status, exit_code,
              log_path, summary_json, pid, reason, scheduled_for
            )
            VALUES (?, ?, NULL, ?, NULL, ?, NULL, ?, ?, ?)
            """,
            (job_id, started_at, status, log_path, pid, reason, scheduled_for),
        )
        return cur.lastrowid

    def reserve_run(
        self,
        *,
        job_id: int,
        started_at: int,
        next_run_at: int | None,
        reason: str,
        scheduled_for: int | None,
    ) -> int:
        cur = self._conn.cursor()
        transaction_started = False
        try:
            cur.execute("BEGIN IMMEDIATE")
            transaction_started = True
            cur.execute(
                "UPDATE job_state SET last_run_at = ? WHERE job_id = ?",
                (started_at, job_id),
            )
            if next_run_at is not None:
                cur.execute(
                    "UPDATE job_state SET next_run_at = ? WHERE job_id = ?",
                    (next_run_at, job_id),
                )
            cur.execute(
                """
                INSERT INTO runs(
                  job_id, started_at, finished_at, status, exit_code,
                  log_path, summary_json, pid, reason, scheduled_for
                )
                VALUES (?, ?, NULL, ?, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (job_id, started_at, RunStatus.RUNNING, reason, scheduled_for),
            )
            run_id = cur.lastrowid
            cur.execute("COMMIT")
            return int(run_id)
        except Exception:
            if transaction_started:
                cur.execute("ROLLBACK")
            raise

    def finish_run(
        self,
        *,
        run_id: int,
        finished_at: int,
        status: str,
        exit_code: int | None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        self._conn.cursor().execute(
            """
            UPDATE runs
            SET finished_at = ?, status = ?, exit_code = ?, summary_json = ?
            WHERE run_id = ?
            """,
            (
                finished_at,
                status,
                exit_code,
                json.dumps(summary) if summary is not None else None,
                run_id,
            ),
        )

    def update_run_pid(self, *, run_id: int, pid: int) -> None:
        self._conn.cursor().execute(
            "UPDATE runs SET pid = ? WHERE run_id = ?", (pid, run_id)
        )

    def update_run_log_path(self, *, run_id: int, log_path: str) -> None:
        self._conn.cursor().execute(
            "UPDATE runs SET log_path = ? WHERE run_id = ?", (log_path, run_id)
        )

    def last_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT r.run_id, r.job_id, d.name AS job_name, r.started_at, r.finished_at, r.status,
                   r.exit_code, r.log_path, r.summary_json, r.pid, r.reason, r.scheduled_for
            FROM runs r
            JOIN job_defs d ON d.id = r.job_id
            ORDER BY r.run_id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [self._run_dict(r) for r in cur.fetchall()]

    def runs_for_job(self, *, job_id: int, limit: int = 50) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT r.run_id, r.job_id, d.name AS job_name, r.started_at, r.finished_at, r.status,
                   r.exit_code, r.log_path, r.summary_json, r.pid, r.reason, r.scheduled_for
            FROM runs r
            JOIN job_defs d ON d.id = r.job_id
            WHERE r.job_id = ?
            ORDER BY r.run_id DESC
            LIMIT ?
            """,
            (job_id, limit),
        )
        return [self._run_dict(r) for r in cur.fetchall()]

    def get_run(self, *, run_id: int) -> dict[str, Any] | None:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT r.run_id, r.job_id, d.name AS job_name, r.started_at, r.finished_at, r.status,
                   r.exit_code, r.log_path, r.summary_json, r.pid, r.reason, r.scheduled_for
            FROM runs r
            JOIN job_defs d ON d.id = r.job_id
            WHERE r.run_id = ?
            """,
            (run_id,),
        )
        r = cur.fetchone()
        if not r:
            return None
        return self._run_dict(r)

    def _run_dict(self, r: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": r["run_id"],
            "job_id": r["job_id"],
            "job_name": r["job_name"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "status": r["status"],
            "exit_code": r["exit_code"],
            "log_path": r["log_path"],
            "summary": json.loads(r["summary_json"]) if r["summary_json"] else None,
            "pid": r["pid"],
            "reason": r["reason"],
            "scheduled_for": r["scheduled_for"],
        }
