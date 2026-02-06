from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wayfinder_paths.runner.constants import JobStatus, RunStatus


def _utc_epoch_s() -> int:
    return int(time.time())


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


@dataclass(frozen=True)
class JobRow:
    id: int
    name: str
    type: str
    payload: dict[str, Any]
    interval_seconds: int
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
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._init_schema()

    @property
    def path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

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
              FOREIGN KEY(job_id) REFERENCES job_defs(id) ON DELETE CASCADE
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
              namespace TEXT NOT NULL,
              key TEXT NOT NULL,
              value_json TEXT NOT NULL,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY(namespace, key)
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_state_due ON job_state(status, next_run_at);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_job_status ON runs(job_id, status);"
        )

    def mark_stale_running_runs_aborted(self, *, note: str) -> int:
        now = _utc_epoch_s()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?, summary_json = ?
                WHERE status = ?
                """,
                (
                    RunStatus.ABORTED,
                    now,
                    _json_dumps({"note": note}),
                    RunStatus.RUNNING,
                ),
            )
            return int(cur.rowcount or 0)

    def add_job(
        self,
        *,
        name: str,
        job_type: str,
        payload: dict[str, Any],
        interval_seconds: int,
        status: str = JobStatus.ACTIVE,
        next_run_at: int | None = None,
    ) -> int:
        try:
            status = str(JobStatus(status))
        except ValueError as exc:
            raise ValueError(f"Invalid job status: {status}") from exc
        now = _utc_epoch_s()
        nra = int(next_run_at if next_run_at is not None else now)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO job_defs(name, type, payload_json, interval_seconds, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, job_type, _json_dumps(payload), int(interval_seconds), now, now),
            )
            job_id = int(cur.lastrowid)
            cur.execute(
                """
                INSERT INTO job_state(job_id, status, next_run_at, last_run_at, last_ok_at, consecutive_failures, last_error)
                VALUES (?, ?, ?, NULL, NULL, 0, NULL)
                """,
                (job_id, status, nra),
            )
            return job_id

    def update_job(
        self,
        *,
        name: str,
        payload: dict[str, Any] | None = None,
        interval_seconds: int | None = None,
    ) -> None:
        now = _utc_epoch_s()
        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [now]
        if payload is not None:
            sets.append("payload_json = ?")
            params.append(_json_dumps(payload))
        if interval_seconds is not None:
            sets.append("interval_seconds = ?")
            params.append(int(interval_seconds))
        if len(sets) == 1:
            return
        params.append(name)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"UPDATE job_defs SET {', '.join(sets)} WHERE name = ?",
                params,
            )
            if cur.rowcount == 0:
                raise KeyError(f"Job not found: {name}")

    def delete_job(self, *, name: str) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM job_defs WHERE name = ?", (str(name),))
            if cur.rowcount == 0:
                raise KeyError(f"Job not found: {name}")

    def get_job(self, *, name: str) -> tuple[JobRow, JobStateRow]:
        with self._lock:
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
                raise KeyError(f"Job not found: {name}")
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            job = JobRow(
                id=int(row["id"]),
                name=str(row["name"]),
                type=str(row["type"]),
                payload=payload,
                interval_seconds=int(row["interval_seconds"]),
                created_at=int(row["created_at"]),
                updated_at=int(row["updated_at"]),
            )
            state = JobStateRow(
                job_id=job.id,
                status=str(row["state_status"]),
                next_run_at=int(row["next_run_at"]),
                last_run_at=int(row["last_run_at"])
                if row["last_run_at"] is not None
                else None,
                last_ok_at=int(row["last_ok_at"])
                if row["last_ok_at"] is not None
                else None,
                consecutive_failures=int(row["consecutive_failures"] or 0),
                last_error=str(row["last_error"])
                if row["last_error"] is not None
                else None,
            )
            return job, state

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
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
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
            out.append(
                {
                    "id": int(r["id"]),
                    "name": str(r["name"]),
                    "type": str(r["type"]),
                    "payload": payload,
                    "interval_seconds": int(r["interval_seconds"]),
                    "created_at": int(r["created_at"]),
                    "updated_at": int(r["updated_at"]),
                    "status": str(r["state_status"]),
                    "next_run_at": int(r["next_run_at"]),
                    "last_run_at": int(r["last_run_at"])
                    if r["last_run_at"] is not None
                    else None,
                    "last_ok_at": int(r["last_ok_at"])
                    if r["last_ok_at"] is not None
                    else None,
                    "consecutive_failures": int(r["consecutive_failures"] or 0),
                    "last_error": str(r["last_error"])
                    if r["last_error"] is not None
                    else None,
                }
            )
        return out

    def set_job_status(self, *, name: str, status: str) -> None:
        try:
            status = str(JobStatus(status))
        except ValueError as exc:
            raise ValueError(f"Invalid job status: {status}") from exc
        with self._lock:
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
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE job_state SET next_run_at = ? WHERE job_id = ?",
                (int(next_run_at), int(job_id)),
            )

    def set_job_last_run(self, *, job_id: int, last_run_at: int) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE job_state SET last_run_at = ? WHERE job_id = ?",
                (int(last_run_at), int(job_id)),
            )

    def record_job_success(self, *, job_id: int, ok_at: int) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE job_state
                SET last_ok_at = ?, consecutive_failures = 0, last_error = NULL
                WHERE job_id = ?
                """,
                (int(ok_at), int(job_id)),
            )

    def record_job_failure(
        self,
        *,
        job_id: int,
        error_text: str,
        max_failures: int,
    ) -> tuple[int, str]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE job_state
                SET consecutive_failures = consecutive_failures + 1,
                    last_error = ?
                WHERE job_id = ?
                """,
                (str(error_text), int(job_id)),
            )
            cur.execute(
                "SELECT consecutive_failures FROM job_state WHERE job_id = ?",
                (int(job_id),),
            )
            row = cur.fetchone()
            failures = int(row["consecutive_failures"] if row else 0)
            status = JobStatus.ACTIVE
            if failures >= int(max_failures):
                status = JobStatus.ERROR
                cur.execute(
                    "UPDATE job_state SET status = ? WHERE job_id = ?",
                    (str(JobStatus.ERROR), int(job_id)),
                )
            return failures, str(status)

    def due_jobs(self, *, now: int) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT d.id, d.name, d.type, d.payload_json, d.interval_seconds,
                       s.status, s.next_run_at, s.last_run_at, s.last_ok_at,
                       s.consecutive_failures, s.last_error
                FROM job_defs d
                JOIN job_state s ON s.job_id = d.id
                WHERE s.status = ? AND s.next_run_at <= ?
                ORDER BY s.next_run_at ASC, d.id ASC
                """,
                (JobStatus.ACTIVE, int(now)),
            )
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "id": int(r["id"]),
                    "name": str(r["name"]),
                    "type": str(r["type"]),
                    "payload": json.loads(r["payload_json"])
                    if r["payload_json"]
                    else {},
                    "interval_seconds": int(r["interval_seconds"]),
                    "status": str(r["status"]),
                    "next_run_at": int(r["next_run_at"]),
                    "last_run_at": int(r["last_run_at"])
                    if r["last_run_at"] is not None
                    else None,
                    "last_ok_at": int(r["last_ok_at"])
                    if r["last_ok_at"] is not None
                    else None,
                    "consecutive_failures": int(r["consecutive_failures"] or 0),
                    "last_error": str(r["last_error"])
                    if r["last_error"] is not None
                    else None,
                }
            )
        return out

    def create_run(
        self,
        *,
        job_id: int,
        started_at: int,
        status: str = RunStatus.RUNNING,
        log_path: str | None = None,
        pid: int | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO runs(job_id, started_at, finished_at, status, exit_code, log_path, summary_json, pid)
                VALUES (?, ?, NULL, ?, NULL, ?, NULL, ?)
                """,
                (int(job_id), int(started_at), str(status), log_path, pid),
            )
            return int(cur.lastrowid)

    def finish_run(
        self,
        *,
        run_id: int,
        finished_at: int,
        status: str,
        exit_code: int | None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE runs
                SET finished_at = ?, status = ?, exit_code = ?, summary_json = ?
                WHERE run_id = ?
                """,
                (
                    int(finished_at),
                    str(status),
                    int(exit_code) if exit_code is not None else None,
                    _json_dumps(summary) if summary is not None else None,
                    int(run_id),
                ),
            )

    def update_run_pid(self, *, run_id: int, pid: int) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE runs SET pid = ? WHERE run_id = ?",
                (int(pid), int(run_id)),
            )

    def update_run_log_path(self, *, run_id: int, log_path: str) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE runs SET log_path = ? WHERE run_id = ?",
                (str(log_path), int(run_id)),
            )

    def last_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT r.run_id, r.job_id, d.name AS job_name, r.started_at, r.finished_at, r.status,
                       r.exit_code, r.log_path, r.summary_json, r.pid
                FROM runs r
                JOIN job_defs d ON d.id = r.job_id
                ORDER BY r.run_id DESC
                LIMIT ?
                """,
                (int(limit),),
            )
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "run_id": int(r["run_id"]),
                    "job_id": int(r["job_id"]),
                    "job_name": str(r["job_name"]),
                    "started_at": int(r["started_at"]),
                    "finished_at": int(r["finished_at"])
                    if r["finished_at"] is not None
                    else None,
                    "status": str(r["status"]),
                    "exit_code": int(r["exit_code"])
                    if r["exit_code"] is not None
                    else None,
                    "log_path": str(r["log_path"])
                    if r["log_path"] is not None
                    else None,
                    "summary": json.loads(r["summary_json"])
                    if r["summary_json"]
                    else None,
                    "pid": int(r["pid"]) if r["pid"] is not None else None,
                }
            )
        return out

    def runs_for_job(self, *, job_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT r.run_id, r.job_id, d.name AS job_name, r.started_at, r.finished_at, r.status,
                       r.exit_code, r.log_path, r.summary_json, r.pid
                FROM runs r
                JOIN job_defs d ON d.id = r.job_id
                WHERE r.job_id = ?
                ORDER BY r.run_id DESC
                LIMIT ?
                """,
                (int(job_id), int(limit)),
            )
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "run_id": int(r["run_id"]),
                    "job_id": int(r["job_id"]),
                    "job_name": str(r["job_name"]),
                    "started_at": int(r["started_at"]),
                    "finished_at": int(r["finished_at"])
                    if r["finished_at"] is not None
                    else None,
                    "status": str(r["status"]),
                    "exit_code": int(r["exit_code"])
                    if r["exit_code"] is not None
                    else None,
                    "log_path": str(r["log_path"])
                    if r["log_path"] is not None
                    else None,
                    "summary": json.loads(r["summary_json"])
                    if r["summary_json"]
                    else None,
                    "pid": int(r["pid"]) if r["pid"] is not None else None,
                }
            )
        return out

    def get_run(self, *, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT r.run_id, r.job_id, d.name AS job_name, r.started_at, r.finished_at, r.status,
                       r.exit_code, r.log_path, r.summary_json, r.pid
                FROM runs r
                JOIN job_defs d ON d.id = r.job_id
                WHERE r.run_id = ?
                """,
                (int(run_id),),
            )
            r = cur.fetchone()
        if not r:
            return None
        return {
            "run_id": int(r["run_id"]),
            "job_id": int(r["job_id"]),
            "job_name": str(r["job_name"]),
            "started_at": int(r["started_at"]),
            "finished_at": int(r["finished_at"])
            if r["finished_at"] is not None
            else None,
            "status": str(r["status"]),
            "exit_code": int(r["exit_code"]) if r["exit_code"] is not None else None,
            "log_path": str(r["log_path"]) if r["log_path"] is not None else None,
            "summary": json.loads(r["summary_json"]) if r["summary_json"] else None,
            "pid": int(r["pid"]) if r["pid"] is not None else None,
        }

    def kv_get(self, *, namespace: str, key: str) -> Any | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT value_json FROM kv WHERE namespace = ? AND key = ?",
                (namespace, key),
            )
            row = cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row["value_json"])
        except Exception:  # noqa: BLE001
            return None

    def kv_set(self, *, namespace: str, key: str, value: Any) -> None:
        now = _utc_epoch_s()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO kv(namespace, key, value_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                  value_json = excluded.value_json,
                  updated_at = excluded.updated_at
                """,
                (namespace, key, _json_dumps(value), now),
            )
