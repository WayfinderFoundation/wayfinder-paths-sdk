"""Decision log: threading via supersession references, outcome
classification (owner veto vs self-cull), glance stats, caps."""

from __future__ import annotations

import datetime as dt
import json

from wayfinder_paths.jobs.decision_log import build_decision_log
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def _ts(hours_ago: float) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours_ago)).isoformat()


def _mk(tmp_path):
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("dlog-demo", agent_mode="intervene")
    store.save(job)
    return store, job.id


def _write_proposal(root, pid: str, summary: str) -> None:
    (root / "proposals").mkdir(exist_ok=True)
    (root / "proposals" / f"{pid}.json").write_text(
        json.dumps({"proposal_id": pid, "payload": {"summary": summary}}),
        encoding="utf-8",
    )


def test_threading_outcomes_and_stats(tmp_path) -> None:
    store, job_id = _mk(tmp_path)
    root = store.job_dir(job_id)
    _write_proposal(root, "prop-11111111", "Probation tier v1")
    _write_proposal(
        root, "prop-22222222", "Probation tier v2 — supersedes prop-11111111"
    )
    _write_proposal(root, "prop-33333333", "Probation tier v3")
    _write_proposal(root, "prop-99999999", "LIT stop widening")

    journal = [
        {"ts": _ts(30), "type": "proposal_created", "proposal_id": "prop-11111111"},
        {
            "ts": _ts(28),
            "type": "proposal_rejected",
            "proposal_id": "prop-11111111",
            "rejected_by": "owner",
            "reason": None,
        },
        {"ts": _ts(20), "type": "proposal_created", "proposal_id": "prop-22222222"},
        {
            "ts": _ts(18),
            "type": "proposal_rejected",
            "proposal_id": "prop-22222222",
            "rejected_by": "agent",
            "reason": "superseded by prop-33333333 — entries must match scan events",
        },
        {"ts": _ts(10), "type": "proposal_created", "proposal_id": "prop-33333333"},
        {
            "ts": _ts(8),
            "type": "proposal_rejected",
            "proposal_id": "prop-33333333",
            "rejected_by": "agent",
            "reason": "VERIFICATION BAR failed: trade delta +227 outside 30-160",
        },
        {"ts": _ts(6), "type": "proposal_created", "proposal_id": "prop-99999999"},
        {
            "ts": _ts(5),
            "type": "proposal_promoted",
            "proposal_id": "prop-99999999",
            "revision": "abc123",
            "changed_files": ["workspace/src/strategy.py"],
        },
        {
            "ts": _ts(4),
            "type": "probation_leg_opened",
            "leg": "LIT_x",
            "proposal_id": "prop-99999999",
        },
        {
            "ts": _ts(3),
            "type": "application_watchdog_recovered",
            "proposal_id": "prop-99999999",
        },
    ]
    (root / "journal.jsonl").write_text(
        "\n".join(json.dumps(e) for e in journal) + "\n", encoding="utf-8"
    )
    (root / "ledgers").mkdir(exist_ok=True)
    (root / "ledgers" / "candidates.jsonl").write_text(
        json.dumps(
            {
                "ts": _ts(7),
                "family": "probation_tier",
                "name": "v3-self-rejected",
                "note": "bar failed for prop-33333333; see prop-22222222 lineage",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    store.write_json(
        job_id,
        "results/research/universe_scan.json",
        {
            "generated_at": _ts(2),
            "pooled_tests": 6581,
            "candidates": [
                {"symbol": "HYPE", "promote": 1, "probation": 1, "scanned": True},
                {"symbol": "ETH", "promote": 0, "probation": 0, "scanned": True},
            ],
        },
    )

    log = build_decision_log(store, job_id)
    entries = log["entries"]
    assert entries[0]["ts"] > entries[-1]["ts"]  # newest first

    by_pid = {}
    for e in entries:
        if e.get("proposal_id"):
            by_pid.setdefault(e["proposal_id"], []).append(e)

    # v2 and v3 share a thread (supersession reference in the v2 reason and
    # the ledger note links all three generations).
    threads = {
        e["proposal_id"]: e.get("thread") for e in entries if e.get("proposal_id")
    }
    assert threads["prop-22222222"] == threads["prop-33333333"]
    assert threads["prop-11111111"] == threads["prop-33333333"]  # via ledger note
    assert threads["prop-99999999"] != threads["prop-33333333"]

    # Outcome classification.
    outcomes = {
        (e["proposal_id"], e["outcome"])
        for e in entries
        if e["kind"] == "proposal" and e["outcome"] != "pending"
    }
    assert ("prop-11111111", "owner_rejected") in outcomes
    assert ("prop-22222222", "self_culled") in outcomes
    assert ("prop-33333333", "self_culled") in outcomes
    assert ("prop-99999999", "applied") in outcomes

    # Titles joined from proposal summaries.
    v3 = next(
        e
        for e in entries
        if e.get("proposal_id") == "prop-33333333" and e["outcome"] == "self_culled"
    )
    assert v3["title"] == "Probation tier v3"
    assert "VERIFICATION BAR" in v3["detail"]

    # Discovery entry from the universe scan (only symbols with edge rows).
    disc = next(e for e in entries if e["kind"] == "discovery")
    assert "HYPE" in disc["detail"] and "ETH" not in disc["detail"]
    assert disc["outcome"] == "discovery"

    stats = log["stats"]
    assert stats["applied"] == 1
    assert stats["owner_rejected"] == 1
    assert stats["self_culled"] == 2
    assert stats["discoveries"] == 1
    assert stats["research_rows"] == 1
    assert stats["current_focus"]


def test_caps_and_empty_job(tmp_path) -> None:
    store, job_id = _mk(tmp_path)
    log = build_decision_log(store, job_id)
    assert log["entries"] == []
    assert log["stats"]["applied"] == 0

    root = store.job_dir(job_id)
    journal = [
        {"ts": _ts(i), "type": "proposal_created", "proposal_id": f"prop-{i:08d}"}
        for i in range(80)
    ]
    (root / "journal.jsonl").write_text(
        "\n".join(json.dumps(e) for e in journal) + "\n", encoding="utf-8"
    )
    log = build_decision_log(store, job_id, limit=25)
    assert len(log["entries"]) == 25
