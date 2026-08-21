"""JobStore resource-bound reads used by tick-time monitors."""

from __future__ import annotations

import json
from pathlib import Path

from wayfinder_paths.jobs.store import JobStore


def test_jsonl_limit_streams_only_requested_tail(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    path = store.job_dir("bounded-tail") / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps({"index": index}) for index in range(5)) + "\n",
        encoding="utf-8",
    )

    assert store.read_jsonl("bounded-tail", "events.jsonl", limit=2) == [
        {"index": 3},
        {"index": 4},
    ]
    assert store.read_jsonl("bounded-tail", "events.jsonl", limit=0) == []
    assert store.read_jsonl("bounded-tail", "events.jsonl", limit=-1) == []
