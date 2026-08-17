"""Fail-closed economic gating: for a live-capable job under blocking
governance, missing evidence (ready=None), a governance change between
evaluation and approval, or a tampered chain all ESCALATE instead of
silently promoting — the review's central fail-open findings."""

from __future__ import annotations

import json

import pytest

from wayfinder_paths.jobs.governance import (
    HARD_CONSTRAINTS_FILE,
    commit_epoch,
    governance_dir,
    migrate_from_constitution,
    record_evidence_access,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def _live_job(tmp_path, *, enforcement: str = "blocking", forward_runs: int = 3):
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "fc-demo",
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(job)
    root = store.job_dir(job.id)
    (root / "constitution.yaml").write_text(f"enforcement: {enforcement}\n")
    migrate_from_constitution(tmp_path, job.id, root)
    if forward_runs:
        summary = root / "results" / "forward" / "summary.json"
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text(json.dumps({"runs": {"count": forward_runs}}))
    return store, job.id


def _proposal(economic: dict) -> dict:
    return {
        "proposal_id": "p1",
        "candidate_report": {
            "revision": "cafecafecafe",
            "validation_summary": {"status": "passed"},
            "gate": {"live_ready": True, "reasons": []},
            "economic": economic,
        },
    }


def _gate(store, job_id, economic, monkeypatch=None) -> None:
    # _ensure_candidate_matches_report needs a real candidate dir; bypass it —
    # this test targets the governance gate specifically.
    store._ensure_candidate_matches_report = lambda *a, **k: None
    store._ensure_candidate_report_gate(
        job_id, _proposal(economic), allow_ungated=False
    )


def test_ready_none_escalates_for_live_blocking(tmp_path) -> None:
    store, job_id = _live_job(tmp_path)
    current_rev = None  # revision matching skipped when absent from report
    with pytest.raises(ValueError, match="ESCALATE.*ready"):
        _gate(store, job_id, {"ready": None, "enforcement": "blocking"})
    with pytest.raises(ValueError, match="ESCALATE"):
        _gate(store, job_id, {"ready": False, "enforcement": "blocking"})
    assert current_rev is None


def test_ready_true_passes_and_prelive_keeps_old_semantics(tmp_path) -> None:
    store, job_id = _live_job(tmp_path)
    from wayfinder_paths.jobs.constitution import load_constitution

    rev = load_constitution(store.job_dir(job_id))["revision"]
    _gate(
        store,
        job_id,
        {"ready": True, "enforcement": "blocking", "constitution_revision": rev},
    )

    # Pre-live job (no forward runs): ready=None passes (old semantics).
    store2, job2 = _live_job(tmp_path / "b", forward_runs=0)
    _gate(store2, job2, {"ready": None, "enforcement": "blocking"})

    # Advisory governance: even a live job passes on None (report-only).
    store3, job3 = _live_job(tmp_path / "c", enforcement="advisory")
    _gate(store3, job3, {"ready": None, "enforcement": "advisory"})
    # ...but an explicit computed False under a blocking REPORT still blocks.
    with pytest.raises(ValueError, match="not economic-ready"):
        _gate(store3, job3, {"ready": False, "enforcement": "blocking"})


def test_governance_change_between_eval_and_approval_escalates(tmp_path) -> None:
    store, job_id = _live_job(tmp_path)
    from wayfinder_paths.jobs.constitution import load_constitution

    old_rev = load_constitution(store.job_dir(job_id))["revision"]
    gov = governance_dir(tmp_path, job_id)
    (gov / HARD_CONSTRAINTS_FILE).write_text("max_drawdown_pct: 0.30\n")
    commit_epoch(gov, note="owner loosened after evaluation")

    with pytest.raises(ValueError, match="ESCALATE.*changed since"):
        _gate(
            store,
            job_id,
            {
                "ready": True,
                "enforcement": "blocking",
                "constitution_revision": old_rev,
            },
        )


def test_tampered_chain_escalates_approval_and_claim(tmp_path) -> None:
    store, job_id = _live_job(tmp_path)
    gov = governance_dir(tmp_path, job_id)
    (gov / HARD_CONSTRAINTS_FILE).write_text("max_drawdown_pct: 0.99\n")  # no commit

    with pytest.raises(ValueError, match="ESCALATE.*tampered"):
        _gate(store, job_id, {"ready": True, "enforcement": "blocking"})

    from wayfinder_paths.jobs.application import claim_application

    with pytest.raises(ValueError, match="ESCALATE.*tampered"):
        claim_application(store, job_id, "p1")


def test_crash_record_carries_real_enforcement(tmp_path, monkeypatch) -> None:
    store, job_id = _live_job(tmp_path)
    import wayfinder_paths.jobs.proposals as proposals_module

    def boom(*args, **kwargs):
        raise RuntimeError("dataset missing")

    monkeypatch.setattr(proposals_module, "evaluate_economic_gate", boom)
    # Reach into the propose flow's except branch via a direct call shape:
    # simulate what propose_change builds when evaluation crashes.
    from wayfinder_paths.jobs.constitution import load_constitution

    constitution = load_constitution(store.job_dir(job_id))
    assert constitution["enforcement"] == "blocking"
    # The new except-branch behavior is exercised end-to-end in propose tests;
    # here we assert the invariant it exists to protect: a crash record under
    # blocking governance can never pass the live gate.
    with pytest.raises(ValueError, match="ESCALATE"):
        _gate(
            store,
            job_id,
            {
                "ready": None,
                "enforcement": "blocking",
                "status": "error",
                "escalate": True,
            },
        )


def test_evidence_access_ledger(tmp_path) -> None:
    record_evidence_access(tmp_path, "fc-demo", "backtest_job", {"quick_bars": 500})
    record_evidence_access(tmp_path, "fc-demo", "experiments")
    path = tmp_path / "audit" / "fc-demo" / "evidence_access.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["op"] for row in rows] == ["backtest_job", "experiments"]
    from wayfinder_paths.jobs.governance import evidence_access_count

    assert evidence_access_count(tmp_path, "fc-demo") == 2


def test_op_runner_records_evidence_access(tmp_path, monkeypatch) -> None:
    from wayfinder_paths.jobs.execution import op_runner

    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.governance.record_evidence_access",
        lambda repo_root, job_id, op, detail=None: recorded.append((job_id, op)),
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.store.JobStore",
        lambda: type("S", (), {"repo_root": tmp_path})(),
    )
    monkeypatch.setattr(op_runner, "_run_op", lambda op, kwargs: {"ok": True})
    op_runner._run("backtest_job", {"job_id": "fc-demo", "quick_bars": 100})
    op_runner._run("__echo__", {"job_id": "fc-demo"})  # not an evidence op
    assert recorded == [("fc-demo", "backtest_job")]
