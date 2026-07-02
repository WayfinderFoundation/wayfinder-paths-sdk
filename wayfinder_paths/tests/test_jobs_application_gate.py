"""Apply-path correctness: the live gate must survive a successful apply.

Candidate validation runs backtest+preflight+validation against the candidate
workspace; those artifacts are revision-stamped and copied on promotion
(candidate revision == promoted revision), so `evaluate_live_gate` stays green
through an apply instead of going stale until a manual re-run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from wayfinder_paths.jobs.application import (
    claim_application,
    complete_application,
    ensure_jobs_v1_contract,
    validate_application_candidate,
)
from wayfinder_paths.jobs.execution.experiments import promote_params
from wayfinder_paths.jobs.gating import evaluate_live_gate
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.tests.test_jobs_gating import _make_job
from wayfinder_paths.tests.test_jobs_preflight import _bars
from wayfinder_paths.tests.test_wayfinder_jobs import _intent_contract


def _patch_runner(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    class FakeBridge:
        def __init__(self, *, repo_root=None):  # noqa: ANN001
            self.repo_root = repo_root

        def pause(self, name: str) -> dict:
            calls.append(("pause", name))
            return {"ok": True}

        def resume(self, name: str) -> dict:
            calls.append(("resume", name))
            return {"ok": True}

    class FakeCompiler:
        def __init__(self, *, store=None):  # noqa: ANN001
            self.store = store

        def compile(self, job):  # noqa: ANN001
            calls.append(("compile", job.id))
            return {"job_id": job.id, "jobs": []}

    monkeypatch.setattr("wayfinder_paths.jobs.application.RunnerBridge", FakeBridge)
    monkeypatch.setattr("wayfinder_paths.jobs.application.JobCompiler", FakeCompiler)
    return calls


def _write_bars_proposal(store: JobStore, job_id: str, proposal_id: str) -> None:
    store.write_proposal(
        job_id,
        {
            "proposal_id": proposal_id,
            "job_id": job_id,
            "status": "pending",
            "proposed_change": {"summary": "Loosen the entry threshold."},
            "intent_contract": _intent_contract(),
            "scenario_plan": {
                "scenarios": [
                    {
                        "name": "baseline_replay",
                        "bars": _bars(),
                        "expect": {"execution_valid": True},
                    }
                ]
            },
        },
    )


def test_apply_keeps_live_gate_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_runner(monkeypatch)
    store, job_id, root = _make_job(tmp_path)
    _write_bars_proposal(store, job_id, "prop_gate")

    # Hand-written proposal (no candidate_report): the legacy prose-driven
    # apply path, kept alive via the explicit escape hatch.
    store.approve_proposal(job_id, "prop_gate", allow_ungated=True)
    claimed = claim_application(store, job_id, "prop_gate")
    candidate_dir = (
        store.repo_root / claimed["proposal"]["application"]["candidate_dir"]
    )

    # The agent's change: edit the CANDIDATE params (changes the revision).
    candidate_yaml = candidate_dir / "job.yaml"
    job_yaml = yaml.safe_load(candidate_yaml.read_text(encoding="utf-8"))
    job_yaml["execution_params"]["threshold"] = 10.7
    candidate_yaml.write_text(yaml.safe_dump(job_yaml, sort_keys=False))

    validation = validate_application_candidate(store, job_id, "prop_gate")
    assert validation["status"] == "passed", validation["checks"]
    # Candidate validation persisted revision-stamped artifacts in the bundle.
    assert (candidate_dir / "results" / "backtest" / "latest.json").exists()
    assert (candidate_dir / "reports" / "preflight" / "latest.json").exists()
    assert (candidate_dir / "reports" / "validation" / "latest.json").exists()

    completed = complete_application(store, job_id, "prop_gate", status="applied")
    promoted = completed["promoted_revision"]
    assert completed["proposal"]["application"]["status"] == "applied"
    assert promoted

    # The whole point: gate green at the promoted revision, no manual re-runs.
    gate = evaluate_live_gate(job_id, store=store)
    assert gate["live_ready"] is True, gate["reasons"]
    assert gate["revision"] == promoted

    backtest = json.loads(
        (root / "results" / "backtest" / "latest.json").read_text(encoding="utf-8")
    )
    assert backtest["revision"] == promoted
    apply_report = store.read_json(job_id, "reports/apply/latest.json", default={})
    assert apply_report["live_gate"]["live_ready"] is True

    # And the promoted job actually carries the proposed change.
    job = store.load(job_id)
    assert job.execution_params["threshold"] == 10.7


def test_promote_params_via_proposal_is_approvable_without_scenario_plan(
    tmp_path: Path,
) -> None:
    store, job_id, _ = _make_job(tmp_path)
    # Job declares no execution_scenario_plan: the proposal must synthesize a
    # replay scenario from the backtest dataset instead of dead-ending. It now
    # routes through propose_change, so it also carries a candidate_report and
    # approves WITHOUT the escape hatch.
    outcome = promote_params(
        job_id, params={"threshold": 42.0}, via_proposal=True, store=store
    )
    assert outcome["mode"] == "proposal"
    report = outcome["candidate_report"]
    assert report["gate"]["live_ready"] is True, report["gate"]["reasons"]

    proposal = store.approve_proposal(job_id, outcome["proposal_id"])
    assert proposal["status"] == "approved"
    scenarios = proposal["scenario_plan"]["scenarios"]
    assert scenarios and scenarios[0]["bars"], "synthesized replay scenario"
    assert scenarios[0]["expect"] == {"execution_valid": True}


def test_agent_only_job_approves_without_scenarios(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "research-demo",
        script=".wayfinder/jobs/research-demo/workspace/src/notes.py",
        interval_seconds=300,
        execution_contract="jobs_v1",
    )
    job.script_loop.enabled = False  # research-only: nothing to replay
    store.save(job)
    store.write_proposal(
        job.id,
        {
            "proposal_id": "prop_research",
            "job_id": job.id,
            "status": "pending",
            "proposed_change": {"summary": "Track a new data source."},
            "intent_contract": _intent_contract(),
        },
    )

    proposal = store.approve_proposal(job.id, "prop_research", allow_ungated=True)
    assert proposal["status"] == "approved"


def test_contract_guard_blocks_legacy_jobs(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "legacy-demo",
        script=".wayfinder_runs/demo.py",
        interval_seconds=300,
    )
    store.save(job)

    with pytest.raises(ValueError, match="legacy execution contract"):
        ensure_jobs_v1_contract(store, job.id)
    ensure_jobs_v1_contract(store, job.id, allow_legacy=True)  # escape hatch

    jobs_v1 = WayfinderJob.new(
        "v1-demo",
        script=".wayfinder/jobs/v1-demo/workspace/src/s.py",
        interval_seconds=300,
        execution_contract="jobs_v1",
    )
    store.save(jobs_v1)
    ensure_jobs_v1_contract(store, jobs_v1.id)  # passes silently


def test_direct_promote_params_restamps_preflight(tmp_path: Path) -> None:
    from wayfinder_paths.tests.test_jobs_gating import _run_full_gate_pipeline

    store, job_id, root = _make_job(tmp_path)
    _run_full_gate_pipeline(store, job_id)
    assert evaluate_live_gate(job_id, store=store)["live_ready"] is True

    outcome = promote_params(job_id, params={"threshold": 11.1}, store=store)
    assert outcome["mode"] == "direct"
    assert outcome["preflight"] == "passed"

    gate = evaluate_live_gate(job_id, store=store)
    assert gate["live_ready"] is True, gate["reasons"]
    assert gate["revision"] == outcome["revision"]
