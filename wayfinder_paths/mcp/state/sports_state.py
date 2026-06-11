"""Persisted, read-through SQLite mirror of sports backtest runs.

The backend DB is canonical. This mirror is an offline fallback so the primary agent
can still report on known runs when the gateway is briefly unreachable. It lives under
``.wayfinder/sports/state.sqlite`` -- the same ``.wayfinder`` tree the runner uses,
which on the Fly box resolves under the persisted user-vault volume and survives reboots.

Writes are opportunistic: whenever a gateway call returns run summaries, we upsert them
here. Reads prefer the gateway; callers fall back to ``list_runs``/``get_run`` only when
the gateway errors.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from wayfinder_paths.runner.paths import find_repo_root

_LOCK = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    status TEXT,
    sport TEXT,
    provider TEXT,
    model_id TEXT,
    title TEXT,
    next_poll_after TEXT,
    updated TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_updated ON runs(updated DESC);
"""


def state_db_path() -> Path:
    override = os.environ.get("WAYFINDER_SPORTS_STATE_DIR")
    if override:
        base = Path(override).expanduser()
        if not base.is_absolute():
            base = find_repo_root() / base
    else:
        base = find_repo_root() / ".wayfinder" / "sports"
    base.mkdir(parents=True, exist_ok=True)
    return (base / "state.sqlite").resolve()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(state_db_path()))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _run_id_of(run: dict[str, Any]) -> str | None:
    value = run.get("run_id") or run.get("id")
    return str(value).strip() if value else None


def upsert_runs(runs: list[dict[str, Any]]) -> int:
    """Mirror a list of run summaries returned by the gateway. Best-effort (never raises)."""
    rows = [run for run in (runs or []) if isinstance(run, dict) and _run_id_of(run)]
    if not rows:
        return 0
    try:
        with _LOCK, _connect() as conn:
            conn.executemany(
                """
                INSERT INTO runs
                    (run_id, status, sport, provider, model_id, title,
                     next_poll_after, updated, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,
                    sport=excluded.sport,
                    provider=excluded.provider,
                    model_id=excluded.model_id,
                    title=excluded.title,
                    next_poll_after=excluded.next_poll_after,
                    updated=excluded.updated,
                    payload=excluded.payload
                """,
                [
                    (
                        _run_id_of(run),
                        run.get("status"),
                        run.get("sport"),
                        run.get("provider"),
                        run.get("model_id"),
                        run.get("title"),
                        run.get("next_poll_after"),
                        run.get("updated"),
                        json.dumps(run, separators=(",", ":")),
                    )
                    for run in rows
                ],
            )
        return len(rows)
    except sqlite3.Error:
        return 0


def list_runs(*, active_only: bool = False, limit: int = 10) -> list[dict[str, Any]]:
    active = ("preview", "model_saved", "evaluation", "predictions")
    try:
        with _LOCK, _connect() as conn:
            if active_only:
                placeholders = ",".join("?" for _ in active)
                cur = conn.execute(
                    f"SELECT payload FROM runs WHERE status IN ({placeholders}) "
                    "ORDER BY updated DESC LIMIT ?",
                    (*active, int(limit)),
                )
            else:
                cur = conn.execute(
                    "SELECT payload FROM runs ORDER BY updated DESC LIMIT ?",
                    (int(limit),),
                )
            return [json.loads(row["payload"]) for row in cur.fetchall()]
    except (sqlite3.Error, ValueError):
        return []


def get_run(run_id: str) -> dict[str, Any] | None:
    try:
        with _LOCK, _connect() as conn:
            cur = conn.execute(
                "SELECT payload FROM runs WHERE run_id = ?", (str(run_id).strip(),)
            )
            row = cur.fetchone()
            return json.loads(row["payload"]) if row else None
    except (sqlite3.Error, ValueError):
        return None
