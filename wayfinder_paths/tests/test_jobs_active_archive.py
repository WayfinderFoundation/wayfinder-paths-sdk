"""Active archive: content-derived candidate IDs dedup re-proposals onto one
entry (accumulating proposal UUIDs and parent edges), lineage walks the DAG,
promotion resolves legacy handles, and opportunity recall is constrained by
the constitution instead of flagging drawdown violators as missed wins."""

from __future__ import annotations

from wayfinder_paths.jobs.archive import (
    lineage_of,
    load_archive,
    record_candidate,
    set_candidate_status,
    set_incumbent,
)
from wayfinder_paths.jobs.evolution_ledger import _opportunity_recall
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def _job(tmp_path):
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "arch-demo",
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(job)
    return store, job.id


def _record(store, job_id, cid, **kwargs):
    defaults = dict(
        family="params",
        summary=f"candidate {cid}",
        status="archived",
        objective={"net_log_growth": 0.01},
    )
    defaults.update(kwargs)
    return record_candidate(store, job_id, candidate_id=cid, **defaults)


def test_content_id_dedup_accumulates_proposals_and_parents(tmp_path) -> None:
    store, job_id = _job(tmp_path)
    _record(
        store,
        job_id,
        "cand-aaa",
        proposal_id="prop-1",
        parent_candidate_ids=["cand-root"],
    )
    entry = _record(
        store,
        job_id,
        "cand-aaa",
        proposal_id="prop-2",
        parent_candidate_ids=["cand-root"],
    )
    assert entry["proposal_ids"] == ["prop-1", "prop-2"]
    assert entry["parent_candidate_ids"] == ["cand-root"]
    assert len(load_archive(store, job_id)["candidates"]) == 1

    # Sticky refutation survives content-id re-proposal.
    set_candidate_status(store, job_id, "cand-aaa", "refuted", evidence="dead")
    entry = _record(store, job_id, "cand-aaa", proposal_id="prop-3")
    assert entry["status"] == "refuted"
    assert "prop-3" in entry["proposal_ids"]


def test_lineage_walks_parent_dag(tmp_path) -> None:
    store, job_id = _job(tmp_path)
    _record(store, job_id, "cand-a")
    _record(store, job_id, "cand-b", parent_candidate_ids=["cand-a"])
    _record(store, job_id, "cand-c", parent_candidate_ids=["cand-b"])
    lineage = lineage_of(store, job_id, "cand-c")
    assert [e["candidate_id"] for e in lineage] == ["cand-b", "cand-a"]

    # Cycle-safe: a->b->a terminates.
    _record(store, job_id, "cand-x", parent_candidate_ids=["cand-y"])
    _record(store, job_id, "cand-y", parent_candidate_ids=["cand-x"])
    ids = [e["candidate_id"] for e in lineage_of(store, job_id, "cand-x")]
    assert ids == ["cand-y", "cand-x"]


def test_set_incumbent_resolves_proposal_uuid_and_revision(tmp_path) -> None:
    store, job_id = _job(tmp_path)
    _record(store, job_id, "cand-abc", proposal_id="prop-9", revision="abc123")
    # Legacy handle: promotion by proposal UUID still lands on the entry.
    set_incumbent(store, job_id, "prop-9")
    doc = load_archive(store, job_id)
    assert doc["candidates"][0]["status"] == "incumbent"
    # Content handle: cand-<revision> misses exact id, resolves via revision.
    _record(store, job_id, "cand-def", revision="def456")
    set_incumbent(store, job_id, "def456")
    by_id = {e["candidate_id"]: e for e in load_archive(store, job_id)["candidates"]}
    assert by_id["cand-def"]["status"] == "incumbent"
    assert by_id["cand-abc"]["status"] == "archived"


def test_opportunity_recall_is_constraint_aware(tmp_path) -> None:
    store, job_id = _job(tmp_path)
    root = store.job_dir(job_id)
    (root / "constitution.yaml").write_text(
        "objective:\n  weights:\n    downside: 0.5\n    tail: 1.0\n"
        "hard_constraints:\n  max_drawdown_pct: 0.25\n  max_tail_loss: 0.15\n"
    )
    _record(
        store,
        job_id,
        "cand-incumbent",
        status="incumbent",
        objective={
            "net_log_growth": 0.02,
            "downside_deviation": 0.01,
            "tail_loss": 0.01,
            "max_drawdown_pct": 0.05,
        },
    )
    # Higher growth but blows the drawdown ceiling: NOT a missed opportunity.
    _record(
        store,
        job_id,
        "cand-violator",
        objective={
            "net_log_growth": 0.10,
            "downside_deviation": 0.02,
            "tail_loss": 0.02,
            "max_drawdown_pct": 0.60,
        },
    )
    recall = _opportunity_recall(store, job_id)
    assert recall["missed"] is False
    assert recall["violating_excluded"] == 1

    # A passing candidate that utility-beats the incumbent IS missed.
    _record(
        store,
        job_id,
        "cand-better",
        objective={
            "net_log_growth": 0.05,
            "downside_deviation": 0.01,
            "tail_loss": 0.01,
            "max_drawdown_pct": 0.04,
        },
    )
    recall = _opportunity_recall(store, job_id)
    assert recall["missed"] is True
    assert recall["best_candidate_id"] == "cand-better"
    assert recall["utility_gap"] > 0
    assert "constraint-passing" in recall["basis"]
