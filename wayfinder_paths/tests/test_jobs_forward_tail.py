"""`tail_jsonl` is the one bounded reader behind every ledger tail (forward
snapshot, decision log, JobStore.read_jsonl(limit=)): it streams the last
rows instead of materializing a multi-MB append-only history."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wayfinder_paths.jobs.forward import tail_jsonl
from wayfinder_paths.jobs.store import JobStore


def _write_ledger(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for index in range(rows):
            handle.write(json.dumps({"seq": index}) + "\n")
        handle.write('{"seq": "torn')  # a writer cut off mid-append


def _forbid_whole_file_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(self: Path, *args: object, **kwargs: object) -> str:
        raise AssertionError(f"read_text materialized {self}")

    monkeypatch.setattr(Path, "read_text", _explode)


def test_tail_jsonl_streams_the_last_valid_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fills.jsonl"
    _write_ledger(path, 10_000)
    _forbid_whole_file_reads(monkeypatch)

    assert [row["seq"] for row in tail_jsonl(path, 3)] == [9997, 9998, 9999]
    assert tail_jsonl(path, 0) == []
    assert tail_jsonl(tmp_path / "missing.jsonl", 3) == []


def test_store_read_jsonl_limit_uses_the_streaming_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(repo_root=tmp_path)
    _write_ledger(store.job_dir("j") / "results" / "forward" / "fills.jsonl", 10_000)
    _forbid_whole_file_reads(monkeypatch)

    rows = store.read_jsonl("j", "results/forward/fills.jsonl", limit=3)
    assert [row["seq"] for row in rows] == [9997, 9998, 9999]
    assert store.read_jsonl("j", "results/forward/fills.jsonl", limit=0) == []
