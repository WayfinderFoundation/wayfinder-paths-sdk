"""Append-only job ledgers: the anti-amnesia history for agent loops."""

from __future__ import annotations

from pathlib import Path

import pytest

from wayfinder_paths.jobs.ledger import append_ledger_row, tail_ledger
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def _store(tmp_path: Path) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "ledger-demo",
        script=".wayfinder/jobs/ledger-demo/workspace/src/s.py",
        interval_seconds=300,
    )
    store.save(job)
    return store, job.id


def test_append_and_tail_round_trip(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)

    first = append_ledger_row(
        store,
        job_id,
        "candidates",
        {"name": "chop filter", "bucket": "adjacent", "status": "proposed"},
    )
    assert first["ts"]
    append_ledger_row(
        store,
        job_id,
        "candidates",
        {"name": "OP rotation", "bucket": "divergent", "status": "no_edge"},
    )

    rows = tail_ledger(store, job_id, "candidates")
    assert [row["name"] for row in rows] == ["chop filter", "OP rotation"]
    assert (store.job_dir(job_id) / "ledgers" / "candidates.jsonl").exists()


def test_tail_limit_returns_most_recent(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    for index in range(30):
        append_ledger_row(store, job_id, "decisions", {"n": index})

    rows = tail_ledger(store, job_id, "decisions", limit=5)
    assert [row["n"] for row in rows] == [25, 26, 27, 28, 29]


def test_corrupt_line_never_breaks_the_tail(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    append_ledger_row(store, job_id, "decisions", {"n": 1})
    path = store.job_dir(job_id) / "ledgers" / "decisions.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    append_ledger_row(store, job_id, "decisions", {"n": 2})

    rows = tail_ledger(store, job_id, "decisions")
    assert [row["n"] for row in rows] == [1, 2]


def test_name_validation_and_row_shape(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    with pytest.raises(ValueError, match="ledger name"):
        append_ledger_row(store, job_id, "../escape", {"n": 1})
    with pytest.raises(ValueError, match="ledger name"):
        tail_ledger(store, job_id, "Bad Name")
    with pytest.raises(ValueError, match="non-empty object"):
        append_ledger_row(store, job_id, "candidates", {})
    assert tail_ledger(store, job_id, "empty-ledger") == []
