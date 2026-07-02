"""Append-only job ledgers: tiny histories that make agent loops non-amnesic.

Two conventional ledgers power the exploration/exploitation loops (names are
convention, not enforced schema):

- `candidates` (improve loop): one row per seriously considered idea —
  `{name, bucket: core|adjacent|divergent, family, score?, status:
  proposed|no_edge|deferred|rejected, note}`. The worker checks this tail
  before exploring, so a `no_edge`/`rejected` family is never re-explored
  unchanged.
- `decisions` (auto loop): one row per considered opportunity —
  `{market, bucket, decision: executed|skipped|blocked|watch, size?, edge?,
  confidence?, reason}`. Powers memory calibration ("last 10: 2 executed /
  5 skipped / 3 blocked").

Rows are append-only JSONL under `.wayfinder/jobs/<id>/ledgers/<name>.jsonl`;
recent tails are fed into the worker's dynamic prompt context.
"""

from __future__ import annotations

import json
import re
from typing import Any

from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

LEDGER_DIR = "ledgers"
_NAME_RE = re.compile(r"^[a-z0-9_-]+$")


def _ledger_path(store: JobStore, job_id: str, name: str):
    if not _NAME_RE.match(name or ""):
        raise ValueError(
            f"ledger name must match [a-z0-9_-]+: {name!r}"
        )
    return store.job_dir(job_id) / LEDGER_DIR / f"{name}.jsonl"


def append_ledger_row(
    store: JobStore, job_id: str, name: str, row: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(row, dict) or not row:
        raise ValueError("ledger row must be a non-empty object")
    payload = {"ts": utc_now_iso(), **row}
    target = _ledger_path(store, job_id, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    return payload


def tail_ledger(
    store: JobStore, job_id: str, name: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    target = _ledger_path(store, job_id, name)
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a corrupt line never breaks the tail
        if isinstance(row, dict):
            rows.append(row)
    return rows[-max(int(limit), 1):]
