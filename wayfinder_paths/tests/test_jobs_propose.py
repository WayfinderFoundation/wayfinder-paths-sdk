"""Structured propose + compare: pre-approval candidates with evidence.

The change the user approves must be byte-for-byte the change that promotes:
propose stages a validated candidate, claim REUSES it (revision-checked), and
the approval gate demands the candidate_report evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from wayfinder_paths.jobs.application import (
    claim_application,
    complete_application,
    validate_application_candidate,
)
from wayfinder_paths.jobs.backtest_artifacts import load_backtest_view
from wayfinder_paths.jobs.gating import evaluate_live_gate
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.proposals import propose_change
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.tests.test_jobs_application_gate import _patch_runner
from wayfinder_paths.tests.test_jobs_gating import _make_job
from wayfinder_paths.tests.test_wayfinder_jobs import _intent_contract


def _propose_params(store: JobStore, job_id: str, **overrides):
    return propose_change(
        store,
        job_id,
        kind=overrides.pop("kind", "params_update"),
        summary=overrides.pop("summary", "Loosen the entry threshold."),
        intent_contract=_intent_contract(),
        params=overrides.pop("params", {"threshold": 10.7}),
        **overrides,
    )


def test_propose_builds_full_candidate_report(tmp_path: Path) -> None:
    store, job_id, root = _make_job(tmp_path)

    proposal = _propose_params(store, job_id)

    report = proposal["candidate_report"]
    assert report["mode"] == "full"
    assert report["gate"]["live_ready"] is True, report["gate"]["reasons"]
    assert report["revision"] and report["base_revision"]
    assert report["revision"] != report["base_revision"]
    assert report["validation_summary"]["status"] == "passed"
    comparison = report["comparison"]
    assert comparison["baseline"]["stats"]
    assert comparison["candidate"]["stats"]
    assert "net_return" in comparison["deltas"]
    assert "series" not in json.dumps(comparison), "stats only, never points"
    assert "job.yaml" in proposal["changed_files"]

    pid = proposal["proposal_id"]
    assert (root / "applications" / pid / "comparison.json").exists()
    candidate_yaml = yaml.safe_load(
        (root / "applications" / pid / "candidate" / "job.yaml").read_text()
    )
    assert candidate_yaml["execution_params"]["threshold"] == 10.7


def test_claim_reuses_propose_time_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The destroy-the-change regression: claiming must not recopy the active
    workspace over the proposed change."""
    _patch_runner(monkeypatch)
    store, job_id, root = _make_job(tmp_path)
    proposal = _propose_params(store, job_id)
    pid = proposal["proposal_id"]

    store.approve_proposal(job_id, pid)  # no escape hatch needed
    claim_application(store, job_id, pid)

    candidate_yaml = yaml.safe_load(
        (root / "applications" / pid / "candidate" / "job.yaml").read_text()
    )
    assert candidate_yaml["execution_params"]["threshold"] == 10.7
    journal = (root / "journal.jsonl").read_text(encoding="utf-8")
    assert "candidate_reused" in journal

    validation = validate_application_candidate(store, job_id, pid)
    assert validation["status"] == "passed", validation["checks"]
    completed = complete_application(store, job_id, pid, status="applied")
    assert completed["proposal"]["application"]["status"] == "applied"
    assert store.load(job_id).execution_params["threshold"] == 10.7
    assert evaluate_live_gate(job_id, store=store)["live_ready"] is True


def test_stale_baseline_is_recorded_but_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_runner(monkeypatch)
    store, job_id, root = _make_job(tmp_path)
    proposal = _propose_params(store, job_id)
    pid = proposal["proposal_id"]

    # Active workspace moves after propose.
    script = root / "workspace" / "src" / "strategy.py"
    script.write_text(script.read_text() + "\n# drift\n", encoding="utf-8")

    store.approve_proposal(job_id, pid)
    claimed = claim_application(store, job_id, pid)

    assert claimed["candidate"].get("stale_baseline") is True
    journal = (root / "journal.jsonl").read_text(encoding="utf-8")
    assert "candidate_baseline_drift" in journal
    # The self-contained candidate still carries the change.
    candidate_yaml = yaml.safe_load(
        (root / "applications" / pid / "candidate" / "job.yaml").read_text()
    )
    assert candidate_yaml["execution_params"]["threshold"] == 10.7


def test_tampered_candidate_is_rejected_at_approve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2 apply-drift guard: a candidate edited after its report was generated
    no longer hashes to the validated revision, so approval must reject it
    (rather than promote a bundle that was never validated)."""
    _patch_runner(monkeypatch)
    store, job_id, root = _make_job(tmp_path)
    proposal = _propose_params(store, job_id)
    pid = proposal["proposal_id"]

    tampered = root / "applications" / pid / "candidate" / "workspace" / "x.py"
    tampered.write_text("tampered = True\n", encoding="utf-8")

    with pytest.raises(ValueError, match="candidate changed since its report"):
        store.approve_proposal(job_id, pid)


def test_tampered_candidate_falls_back_to_fresh_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_runner(monkeypatch)
    store, job_id, root = _make_job(tmp_path)
    proposal = _propose_params(store, job_id)
    pid = proposal["proposal_id"]

    # Hand-edit the candidate AFTER propose: recorded revision no longer
    # matches, so claim must rebuild from the active workspace. Use the ungated
    # escape hatch to bypass the approve-time freshness guard and reach the
    # claim fallback path (which now records why the change vanished).
    tampered = root / "applications" / pid / "candidate" / "workspace" / "x.py"
    tampered.write_text("tampered = True\n", encoding="utf-8")

    store.approve_proposal(job_id, pid, allow_ungated=True)
    claim_application(store, job_id, pid)

    candidate_yaml = yaml.safe_load(
        (root / "applications" / pid / "candidate" / "job.yaml").read_text()
    )
    assert "threshold" not in candidate_yaml.get("execution_params", {}) or (
        candidate_yaml["execution_params"].get("threshold") != 10.7
    )
    journal = (root / "journal.jsonl").read_text(encoding="utf-8")
    assert "candidate_reused" not in journal
    assert "candidate_report_stale" in journal


def test_ungated_jobs_v1_approval_requires_escape_hatch(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path)
    store.write_proposal(
        job_id,
        {
            "proposal_id": "prop_bare",
            "job_id": job_id,
            "status": "pending",
            "proposed_change": {"summary": "Hand-written change."},
            "intent_contract": _intent_contract(),
            "scenario_plan": {"scenarios": [{"name": "s", "bars": [1]}]},
        },
    )

    with pytest.raises(ValueError, match="candidate_report"):
        store.approve_proposal(job_id, "prop_bare")
    proposal = store.approve_proposal(job_id, "prop_bare", allow_ungated=True)
    assert proposal["status"] == "approved"


def test_failed_validation_blocks_approval(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path)
    proposal = _propose_params(store, job_id)
    pid = proposal["proposal_id"]
    # Corrupt the report to simulate a failed candidate validation.
    proposal["candidate_report"]["validation_summary"]["status"] = "failed"
    store.write_proposal(job_id, proposal)

    with pytest.raises(ValueError, match="not passed"):
        store.approve_proposal(job_id, pid)


def test_research_job_gets_validation_only_report(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "research-propose",
        script=".wayfinder/jobs/research-propose/workspace/src/notes.py",
        interval_seconds=300,
        execution_contract="jobs_v1",
    )
    job.script_loop.enabled = False
    store.save(job)
    (store.job_dir(job.id) / "workspace").mkdir(parents=True, exist_ok=True)
    (store.job_dir(job.id) / "job.yaml").exists()

    proposal = propose_change(
        store,
        job.id,
        kind="model_update",
        summary="Track a new research feed.",
        intent_contract=_intent_contract(),
        params={"research_feed": "weather-noaa"},
    )

    report = proposal["candidate_report"]
    assert report["mode"] == "validation_only"
    assert report["gate"]["live_ready"] is None
    assert report["comparison"] is None
    assert report["validation_summary"]["status"] == "passed"
    # And it approves without the escape hatch (validation-only path).
    approved = store.approve_proposal(job.id, proposal["proposal_id"])
    assert approved["status"] == "approved"


def test_backtest_view_supports_proposal_scope(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path)
    proposal = _propose_params(store, job_id)
    pid = proposal["proposal_id"]

    candidate_view = load_backtest_view(job_id, store=store, proposal_id=pid)
    assert candidate_view["available"] is True
    assert candidate_view["run_id"]

    baseline_view = load_backtest_view(job_id, store=store)
    assert baseline_view["available"] is True
    assert baseline_view["run_id"] != candidate_view["run_id"]

    missing = load_backtest_view(job_id, store=store, proposal_id="nope")
    assert missing["available"] is False


def test_propose_requires_a_change_and_valid_kind(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path)
    with pytest.raises(ValueError, match="nothing to propose"):
        propose_change(
            store,
            job_id,
            kind="params_update",
            summary="empty",
            intent_contract=_intent_contract(),
        )
    with pytest.raises(ValueError, match="kind"):
        propose_change(
            store,
            job_id,
            kind="mystery",
            summary="bad kind",
            intent_contract=_intent_contract(),
            params={"threshold": 1},
        )


def test_propose_memo_writes_file_and_rides_change_summary(tmp_path: Path) -> None:
    store, job_id, root = _make_job(tmp_path)
    memo = (
        "# Proposal: loosen threshold\n\n"
        "## Status quo\nEntries gated at 10.4.\n\n"
        "## What the data shows\nMissed entries cluster just above.\n"
    )

    proposal = _propose_params(store, job_id, memo=memo)

    pid = proposal["proposal_id"]
    memo_path = root / "proposals" / f"{pid}.md"
    assert memo_path.exists()
    assert "Status quo" in memo_path.read_text(encoding="utf-8")
    # Light surfacing: the memo IS the change_summary the backend serializes.
    assert proposal["change_summary"] == memo
    assert proposal["proposed_change"]["summary"] != memo  # one-liner intact

    # No memo -> behavior identical to before (change_summary == summary).
    plain = _propose_params(store, job_id, params={"threshold": 11.1})
    assert plain["change_summary"] == plain["proposed_change"]["summary"]
    assert not (root / "proposals" / f"{plain['proposal_id']}.md").exists()
