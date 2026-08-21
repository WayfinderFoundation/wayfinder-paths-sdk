"""Durable regime remediation keeps incumbent alarms actionable."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.remediation import (
    handle_remediation_application,
    handle_remediation_rejection,
    link_remediation_proposal,
    load_remediation,
    proposal_remediation_stamp,
    sync_remediation_with_health,
    update_remediation_progress,
)
from wayfinder_paths.jobs.store import JobStore


def _store(tmp_path: Path) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("remediation-demo", agent_mode="intervene")
    store.save(job)
    return store, job.id


def _health(*, score: int = 4, fingerprint: str = "health-a") -> dict:
    return {
        "status": "critical",
        "score": score,
        "computed_at": "2026-08-21T12:00:00+00:00",
        "evidence_fingerprint": fingerprint,
        "incumbent": {"workspace_revision": "rev-active"},
        "signals": [
            {
                "kind": "drawdown",
                "severity": 2,
                "value": 0.12,
                "window_days": 7,
            }
        ],
    }


def test_case_opens_once_and_retries_until_accountable_outcome(
    tmp_path: Path,
) -> None:
    store, job_id = _store(tmp_path)
    now = dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC)

    opened = sync_remediation_with_health(store, job_id, _health(), now=now)
    assert opened and opened["event"] == "regime_shift"
    assert (
        sync_remediation_with_health(
            store, job_id, _health(), now=now + dt.timedelta(minutes=10)
        )
        is None
    )

    retry = sync_remediation_with_health(
        store, job_id, _health(), now=now + dt.timedelta(minutes=31)
    )
    assert retry and retry["event"] == "regime_remediation_due"
    assert load_remediation(store, job_id)["attempts"] == 2  # type: ignore[index]


def test_material_evidence_bypasses_retry_debounce(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    now = dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC)
    sync_remediation_with_health(store, job_id, _health(), now=now)

    refreshed = sync_remediation_with_health(
        store,
        job_id,
        _health(score=6, fingerprint="health-b"),
        now=now + dt.timedelta(minutes=1),
    )

    assert refreshed and refreshed["event"] == "regime_shift"
    assert "score_increased" in refreshed["material_reasons"]


def test_green_proposal_waits_for_owner_and_rejections_have_provenance(
    tmp_path: Path,
) -> None:
    store, job_id = _store(tmp_path)
    sync_remediation_with_health(store, job_id, _health())
    stamp = proposal_remediation_stamp(store, job_id)
    proposal = {
        "proposal_id": "prop-disable-hype",
        "remediation": stamp,
        "candidate_report": {
            "gate": {"live_ready": True},
            "economic": {"ready": True},
        },
    }

    link_remediation_proposal(store, job_id, proposal)
    assert load_remediation(store, job_id)["state"] == "proposal_pending"  # type: ignore[index]

    agent_rejection = {
        **proposal,
        "rejection": {"by": "agent", "kind": "process", "reason": "superseded"},
    }
    handle_remediation_rejection(store, job_id, agent_rejection)
    assert load_remediation(store, job_id)["state"] == "open"  # type: ignore[index]

    link_remediation_proposal(store, job_id, proposal)
    owner_rejection = {
        **proposal,
        "rejection": {"by": "owner", "kind": "substantive", "reason": "accept risk"},
    }
    handle_remediation_rejection(store, job_id, owner_rejection)
    assert load_remediation(store, job_id)["state"] == "owner_accepted_risk"  # type: ignore[index]


def test_red_candidate_and_bounded_evaluation_keep_case_open(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    sync_remediation_with_health(store, job_id, _health())
    proposal = {
        "proposal_id": "prop-red",
        "remediation": proposal_remediation_stamp(store, job_id),
        "candidate_report": {
            "gate": {"live_ready": False},
            "economic": {"ready": False, "reasons": ["negative OOS"]},
        },
    }
    link_remediation_proposal(store, job_id, proposal)
    assert load_remediation(store, job_id)["state"] == "blocked"  # type: ignore[index]

    update_remediation_progress(
        store,
        job_id,
        state="evaluating",
        note="Running HYPE ablation on all OOS folds",
        artifact_path="results/backtest/hype_ablation.json",
    )
    case = load_remediation(store, job_id)
    assert case and case["state"] == "evaluating"
    assert case["progress"]["artifact_path"].endswith("hype_ablation.json")


def test_applied_treatment_monitors_until_health_recovers(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    sync_remediation_with_health(store, job_id, _health())
    proposal = {
        "proposal_id": "prop-disable-hype",
        "remediation": proposal_remediation_stamp(store, job_id),
        "candidate_report": {
            "gate": {"live_ready": True},
            "economic": {"ready": True},
        },
    }
    link_remediation_proposal(store, job_id, proposal)

    handle_remediation_application(store, job_id, proposal, status="applied")
    assert load_remediation(store, job_id)["state"] == "monitoring"  # type: ignore[index]

    sync_remediation_with_health(
        store,
        job_id,
        {**_health(), "status": "healthy", "score": 0, "signals": []},
    )
    assert load_remediation(store, job_id)["state"] == "resolved"  # type: ignore[index]
