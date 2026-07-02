from __future__ import annotations

import json
from pathlib import Path

from wayfinder_paths.jobs.memory_hygiene import (
    sanitize_job_memory,
    sanitize_memory_json,
    sanitize_memory_markdown,
    scan_unsupported_perf_claims,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def test_scan_matches_the_confabulation_triad_only() -> None:
    assert scan_unsupported_perf_claims("100% win rate, +$560.55, 5 forward trades")
    assert scan_unsupported_perf_claims("win rate 13.89%")
    # Honest zeros and bare metrics (sharpe, plain % not near "win") do not match.
    assert scan_unsupported_perf_claims("0 trades, $0 PnL, 0% win rate") == []
    assert scan_unsupported_perf_claims("Sharpe 60.97, min_range_pct=0.015") == []


def test_sanitize_markdown_pulls_only_offending_lines() -> None:
    text = (
        "Durable lessons:\n"
        "- Chop entries lose; enable min_range_pct.\n"
        "- Forward prove-out (5 trades): 100% win rate, +$560.55 net PnL.\n"
        "Calibration:\n- No decisions recorded yet.\n"
    )
    cleaned, quarantined = sanitize_memory_markdown(text)
    assert len(quarantined) == 1
    assert "Forward prove-out" in quarantined[0]
    assert "Forward prove-out" not in cleaned
    # Structure and clean lessons survive.
    assert "Durable lessons:" in cleaned
    assert "enable min_range_pct" in cleaned
    assert "No decisions recorded yet" in cleaned

    # No-op when clean.
    clean_text = "Durable lessons:\n- None yet.\n"
    assert sanitize_memory_markdown(clean_text) == (clean_text, [])


def test_sanitize_json_drops_poisoned_entries() -> None:
    obj = {
        "lessons": [
            "Enable min_range_pct to filter chop.",
            "Forward prove-out confirms filter: 5/5 trend wins, +$560.55 net PnL",
            {"lesson": "Post-apply: 100% win rate over 10 trades", "ts": "t"},
        ],
        "constraints": ["Never activate without approval."],
        "current_concern": "Forward profit factor below baseline.",
    }
    cleaned, quarantined = sanitize_memory_json(obj)
    assert len(quarantined) == 2  # the two poisoned lessons
    assert cleaned["lessons"] == ["Enable min_range_pct to filter chop."]
    assert cleaned["constraints"] == ["Never activate without approval."]
    # current_concern here has no numeric perf claim -> preserved.
    assert cleaned["current_concern"] == "Forward profit factor below baseline."

    # A poisoned current_concern is nulled.
    obj2 = {"lessons": [], "current_concern": "Live: 100% win rate, +$12.00"}
    cleaned2, q2 = sanitize_memory_json(obj2)
    assert q2 and cleaned2["current_concern"] is None


def _poison_memory(store: JobStore, job_id: str) -> None:
    root = store.job_dir(job_id)
    (root / "memory.md").write_text(
        "Durable lessons:\n"
        "- Enable min_range_pct.\n"
        "- Forward prove-out (5 trades): 100% win rate, +$560.55.\n",
        encoding="utf-8",
    )
    store.write_json(
        job_id,
        "memory.json",
        {
            "job_id": job_id,
            "lessons": ["Forward prove-out: 5/5 trend wins, +$560.55 net PnL"],
            "constraints": [],
            "current_concern": None,
        },
    )


def test_sanitize_job_memory_cleans_when_forward_empty(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("hygiene-empty", agent_mode="intervene")
    store.save(job)
    _poison_memory(store, job.id)

    summary = sanitize_job_memory(store, job.id, forward={})  # empty -> active
    assert summary == {"active": True, "md": 1, "json": 1}

    root = store.job_dir(job.id)
    md = (root / "memory.md").read_text(encoding="utf-8")
    assert "Forward prove-out" not in md
    assert "Enable min_range_pct" in md  # clean lesson kept
    mem_json = store.read_json(job.id, "memory.json")
    assert mem_json["lessons"] == []

    # Quarantine file + journal preserve what was removed.
    quarantine = (root / "memory_quarantine.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in quarantine.splitlines() if line]
    assert len(rows) == 2
    assert all(r["reason"] for r in rows)
    journal = (root / "journal.jsonl").read_text(encoding="utf-8")
    assert "memory_quarantined" in journal

    # Idempotent: a second clean wake finds nothing to remove.
    again = sanitize_job_memory(store, job.id, forward={})
    assert again == {"active": True, "md": 0, "json": 0}


def test_sanitize_job_memory_noop_when_forward_present(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("hygiene-live", agent_mode="intervene")
    store.save(job)
    _poison_memory(store, job.id)

    forward = {"summary": {"runs": {"count": 3}}, "recent_trades": [{"pnl": 1.0}]}
    summary = sanitize_job_memory(store, job.id, forward=forward)
    assert summary == {"active": False, "md": 0, "json": 0}
    # Memory untouched while forward telemetry exists (claims may be supported).
    md = (store.job_dir(job.id) / "memory.md").read_text(encoding="utf-8")
    assert "Forward prove-out" in md


def test_prompt_build_sanitizes_poisoned_memory(tmp_path: Path) -> None:
    """End-to-end: a fresh job (no forward telemetry) with poisoned durable
    memory is cleaned by prepare_job_worker_prompt before the prompt is built,
    so the agent never sees the fabricated forward figures to restate."""
    from wayfinder_paths.jobs.worker import prepare_job_worker_prompt

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("hygiene-prompt", agent_mode="intervene")
    store.save(job)
    _poison_memory(store, job.id)

    result = prepare_job_worker_prompt(store=store, job_id=job.id, mode="intervene")
    assert "Forward prove-out" not in result["prompt"]
    assert "560.55" not in result["prompt"]
    # On-disk memory is now durably clean (fixes production and the eval scan).
    md = (store.job_dir(job.id) / "memory.md").read_text(encoding="utf-8")
    assert "Forward prove-out" not in md
