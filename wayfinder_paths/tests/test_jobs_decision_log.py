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
    # Stats see past the display cap: all 80 pending proposals counted.
    assert log["stats"]["proposals_created"] == 80


def test_active_evolution_campaign_is_one_compact_progress_row(tmp_path) -> None:
    store, job_id = _mk(tmp_path)
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {
            "campaign_id": "campaign-1",
            "status": "active",
            "stage": "generate",
            "started_at": "2026-08-28T16:00:00+00:00",
            "counts": {
                "generated": 3,
                "quick_evaluated": 2,
                "full_dev": 0,
                "proposed": 0,
            },
            "candidates": [
                {
                    "candidate_id": "c03",
                    "prepared_at": "2026-08-28T17:00:00+00:00",
                }
            ],
        },
    )
    root = store.job_dir(job_id)
    (root / "journal.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "ts": f"2026-08-28T16:0{i}:00+00:00",
                    "type": event_type,
                }
            )
            for i, event_type in enumerate(
                (
                    "evolution_campaign_started",
                    "evolution_worker_wakeup",
                    "evolution_session_retired",
                )
            )
        )
        + "\n",
        encoding="utf-8",
    )

    entries = build_decision_log(store, job_id)["entries"]

    assert len(entries) == 1
    assert entries[0] == {
        "ts": "2026-08-28T17:00:00+00:00",
        "kind": "research",
        "title": "Evolution campaign generating candidates",
        "detail": (
            "3 generated · 2 screened · 0 full-development evaluations · "
            "0 finalist-gate evaluations"
        ),
        "outcome": "info",
        "actor": "harness",
    }


def test_terminal_evolution_campaigns_show_outcomes_without_live_duplicate(
    tmp_path,
) -> None:
    store, job_id = _mk(tmp_path)
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-2", "status": "complete", "stage": "complete"},
    )
    root = store.job_dir(job_id)
    journal = [
        {
            "ts": "2026-08-27T12:00:00+00:00",
            "type": "evolution_campaign_failed",
            "campaign_id": "campaign-1",
            "finalize_attempts": 3,
            "reason": "campaign exceeded its safety horizon",
        },
        {
            "ts": "2026-08-28T12:00:00+00:00",
            "type": "evolution_campaign_completed",
            "campaign_id": "campaign-2",
            "paper_proposals": 1,
            "counts": {
                "generated": 12,
                "quick_evaluated": 12,
                "full_dev": 4,
                "proposed": 1,
            },
        },
    ]
    (root / "journal.jsonl").write_text(
        "\n".join(json.dumps(row) for row in journal) + "\n", encoding="utf-8"
    )

    entries = build_decision_log(store, job_id)["entries"]

    assert len(entries) == 2
    assert entries[0]["title"] == (
        "Evolution campaign completed — 1 advanced to forward paper testing"
    )
    assert entries[0]["kind"] == "research"
    assert "12 generated · 12 screened" in entries[0]["detail"]
    assert entries[1]["title"] == "Evolution campaign stopped before completion"
    assert entries[1]["kind"] == "recovery"
    assert entries[1]["detail"].endswith("3 finalization attempt(s)")


def test_apply_lifecycle_events(tmp_path) -> None:
    """The apply pipeline narrates itself: stale-baseline deferral, re-stage,
    genuine apply failure, and owner repair each become feed entries; applied
    and deferral outcomes are not double-reported via proposal_apply_finished."""
    store, job_id = _mk(tmp_path)
    root = store.job_dir(job_id)
    _write_proposal(root, "prop-aaaaaaaa", "Add vol_surge SignalDef")
    _write_proposal(root, "prop-bbbbbbbb", "Bump stop_pct")

    journal = [
        {
            "ts": _ts(10),
            "type": "stale_baseline_promotion_refused",
            "proposal_id": "prop-aaaaaaaa",
            "base_revision": "aaaa11112222",
            "active_revision": "bbbb33334444",
        },
        {
            "ts": _ts(9),
            "type": "proposal_apply_finished",
            "proposal_id": "prop-aaaaaaaa",
            "application_status": "failed",
            "error": "baseline drift: candidate was staged against revision …",
        },
        {
            "ts": _ts(8),
            "type": "proposal_restaged",
            "proposal_id": "prop-aaaaaaaa",
            "new_base_revision": "bbbb33334444",
        },
        {
            "ts": _ts(7),
            "type": "proposal_apply_finished",
            "proposal_id": "prop-bbbbbbbb",
            "application_status": "failed",
            "error": "Candidate validation failed: entrypoint missing",
        },
        {
            "ts": _ts(6),
            "type": "proposal_apply_finished",
            "proposal_id": "prop-bbbbbbbb",
            "application_status": "applied",
            "error": None,
        },
        {
            "ts": _ts(5),
            "type": "owner_workspace_repair",
            "reason": "Applies reverted the HYPE graduation; restored from backup.",
        },
    ]
    (root / "journal.jsonl").write_text(
        "\n".join(json.dumps(row) for row in journal) + "\n", encoding="utf-8"
    )

    log = build_decision_log(store, job_id)
    titles = [entry["title"] for entry in log["entries"]]

    assert any(t.startswith("Apply deferred") for t in titles)
    assert any(t.startswith("Re-staged against current strategy") for t in titles)
    # Genuine failure reported once; baseline-drift failure NOT double-reported.
    failures = [t for t in titles if t.startswith("Apply failed")]
    assert failures == ["Apply failed: Bump stop_pct"]
    # Applied completions are narrated by proposal_promoted, not apply_finished.
    assert not any("applied" in t.lower() for t in titles)
    repair = next(
        entry
        for entry in log["entries"]
        if entry["title"] == "Owner repaired the strategy workspace"
    )
    assert repair["actor"] == "owner"
    assert "HYPE graduation" in repair["detail"]
    # Deferral + re-stage thread with the proposal they belong to.
    deferred = next(
        entry for entry in log["entries"] if entry["title"].startswith("Apply deferred")
    )
    assert deferred["proposal_id"] == "prop-aaaaaaaa"


def test_data_feed_events_reach_the_feed(tmp_path) -> None:
    """Feed degradations must be owner-visible: out_of_credits names the
    owner action, and recovery closes the episode."""
    store, job_id = _mk(tmp_path)
    root = store.job_dir(job_id)
    journal = [
        {
            "ts": _ts(10),
            "type": "data_feed_degraded",
            "cause": "out_of_credits",
            "error": "out_of_credits: HTTP 402 for candles",
        },
        {"ts": _ts(2), "type": "data_feed_recovered"},
    ]
    (root / "journal.jsonl").write_text(
        "\n".join(json.dumps(row) for row in journal) + "\n", encoding="utf-8"
    )

    log = build_decision_log(store, job_id)
    titles = [entry["title"] for entry in log["entries"]]
    degraded = next(t for t in titles if t.startswith("Data feed degraded"))
    assert "out_of_credits" in degraded
    assert "top up API credits" in degraded
    assert any(t == "Data feed recovered" for t in titles)


def test_ideation_events_reach_the_feed(tmp_path) -> None:
    """Research expeditions must be owner-visible: artifacts show ranked
    bucket counts, and an overdue expedition names the broken contract."""
    store, job_id = _mk(tmp_path)
    root = store.job_dir(job_id)
    journal = [
        {
            "ts": _ts(10),
            "type": "ideation_artifact",
            "generated_at": "2026-08-12T00:00:00+00:00",
            "sources": 4,
            "hypotheses": 5,
            "buckets": {"testable": 1, "starved": 3, "refuted": 1},
        },
        {"ts": _ts(2), "type": "ideation_incomplete", "artifact_age_s": 180000},
    ]
    (root / "journal.jsonl").write_text(
        "\n".join(json.dumps(row) for row in journal) + "\n", encoding="utf-8"
    )

    log = build_decision_log(store, job_id)
    by_title = {entry["title"]: entry for entry in log["entries"]}
    artifact = next(
        entry
        for title, entry in by_title.items()
        if title.startswith("Research expedition: 5 hypotheses")
    )
    assert "4 external sources" in artifact["title"]
    assert "1 testable, 3 starved, 1 refuted" in artifact["detail"]
    overdue = next(
        entry
        for title, entry in by_title.items()
        if title.startswith("Research expedition overdue")
    )
    assert "50h old" in overdue["detail"]
