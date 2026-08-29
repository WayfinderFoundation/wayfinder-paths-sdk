"""Approvability contract: un-approvable proposals never reach the owner.

Incident (majors-5m-lab, prop-code-change-1d15c703): a behavior-preserving
numpy refactor was staged as a plain code_change; the fail-closed economic
gate froze ready=False (paired folds byte-identical, delta exactly 0.0) into
the pending proposal and approve ESCALATEd forever — a permanently
un-approvable item in the owner's queue. Four mechanisms close it:
propose-time refusal, the needs_you approvability filter, maintenance
auto-apply for certified equivalence, and watchdog triage of blocked
pendings.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs import apply_launcher
from wayfinder_paths.jobs.failures import TransientInfrastructureError
from wayfinder_paths.jobs.gating import PAIRED_FOLDS_RELATIVE
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.owner_attention import build_owner_attention
from wayfinder_paths.jobs.proposals import (
    MAINTENANCE_AUTO_APPLY_ENV,
    maintenance_auto_apply_blockers,
    maybe_auto_apply_maintenance_proposal,
    propose_change,
)
from wayfinder_paths.jobs.store import JobStore, proposal_approvable
from wayfinder_paths.jobs.watchdog import recover_stalled_applications
from wayfinder_paths.tests.test_jobs_apply_watchdog import _patch_runner
from wayfinder_paths.tests.test_jobs_gating import _make_job
from wayfinder_paths.tests.test_jobs_owner_attention import (
    _store as _attention_store,
)
from wayfinder_paths.tests.test_jobs_owner_attention import (
    _write_pending_proposal,
)
from wayfinder_paths.tests.test_jobs_propose import (
    _make_live_blocking,
    _propose_params,
)
from wayfinder_paths.tests.test_wayfinder_jobs import _intent_contract

_EVIDENCE_NEGATIVE_REASON = "paired delta LCB is not strictly positive"


def _economic(
    revision: str | None,
    *,
    ready: bool | None,
    reasons: list[str],
    enforcement: str = "blocking",
    status: str = "ok",
) -> dict[str, Any]:
    return {
        "ready": ready,
        "reasons": reasons,
        "enforcement": enforcement,
        "constitution_revision": revision,
        "status": status,
    }


def _journal_events(store: JobStore, job_id: str, event_type: str) -> list[dict]:
    return [
        event
        for event in store.read_jsonl(job_id, "journal.jsonl")
        if event.get("type") == event_type
    ]


# ── Part 1: propose-time refusal ─────────────────────────────────────────


def test_evidence_negative_propose_on_live_capable_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root = _make_job(tmp_path)
    revision = _make_live_blocking(store, job_id, root, tmp_path)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.proposals.evaluate_economic_gate",
        lambda *a, **k: _economic(
            revision, ready=False, reasons=[_EVIDENCE_NEGATIVE_REASON]
        ),
    )

    with pytest.raises(ValueError, match="refusing to stage"):
        _propose_params(store, job_id)

    assert list((root / "proposals").glob("*.json")) == [], "no proposal staged"
    assert not list((root / "applications").glob("*/candidate")), "candidate cleaned"
    journal = (root / "journal.jsonl").read_text(encoding="utf-8")
    assert "proposal_refused_ungated" in journal
    assert "proposal_created" not in journal
    refused = _journal_events(store, job_id, "proposal_refused_ungated")
    assert refused[0]["proposal_kind"] == "params_update"
    assert refused[0]["reasons"]


def test_byte_identical_folds_teach_behavior_equivalence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root = _make_job(tmp_path)
    revision = _make_live_blocking(store, job_id, root, tmp_path)

    def _fold(index: int) -> dict[str, Any]:
        return {
            "fold": index,
            "delta_utility": 0.0,
            "baseline": {"trade_count": 7},
            "candidate": {"trade_count": 7},
        }

    def neutral_gate(job_id_: str, *, candidate_dir: Any, **kwargs: Any) -> dict:
        path = Path(candidate_dir) / PAIRED_FOLDS_RELATIVE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "key": {},
                    "evaluation": {
                        "status": "ok",
                        "folds": [_fold(i) for i in range(3)],
                    },
                }
            ),
            encoding="utf-8",
        )
        return _economic(revision, ready=False, reasons=[_EVIDENCE_NEGATIVE_REASON])

    monkeypatch.setattr(
        "wayfinder_paths.jobs.proposals.evaluate_economic_gate", neutral_gate
    )

    with pytest.raises(ValueError, match="behavior-equivalence") as excinfo:
        _propose_params(store, job_id)

    message = str(excinfo.value)
    assert "acceptance_policy='behavior_equivalence'" in message
    assert "economic gate is for edge claims only" in message
    refused = _journal_events(store, job_id, "proposal_refused_ungated")
    assert len(refused) == 1
    assert refused[0]["behavior_preserving_folds"] is True
    assert list((root / "proposals").glob("*.json")) == []


def test_prelive_job_still_stages_evidence_negative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root = _make_job(tmp_path)  # no forward runs → pre-live
    monkeypatch.setattr(
        "wayfinder_paths.jobs.proposals.evaluate_economic_gate",
        lambda *a, **k: _economic(
            None,
            ready=False,
            reasons=[_EVIDENCE_NEGATIVE_REASON],
            enforcement="advisory",
        ),
    )

    proposal = _propose_params(store, job_id)

    assert (root / "proposals" / f"{proposal['proposal_id']}.json").exists()
    journal = (root / "journal.jsonl").read_text(encoding="utf-8")
    assert "proposal_created" in journal
    assert "proposal_refused_ungated" not in journal


def test_infra_economic_failure_still_aborts_not_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root = _make_job(tmp_path)
    _make_live_blocking(store, job_id, root, tmp_path)
    monkeypatch.setenv("WAYFINDER_PROPOSE_LOCK_WAIT_SECONDS", "0.1")

    def missing_bars(*a: Any, **k: Any) -> dict[str, Any]:
        raise RuntimeError(
            "No backtest bars found. Provide results/backtest/input_bars.json"
        )

    monkeypatch.setattr(
        "wayfinder_paths.jobs.proposals.evaluate_economic_gate", missing_bars
    )

    with pytest.raises(TransientInfrastructureError):
        _propose_params(store, job_id)

    journal = (root / "journal.jsonl").read_text(encoding="utf-8")
    assert "proposal_propose_aborted" in journal
    assert "proposal_refused_ungated" not in journal
    assert list((root / "proposals").glob("*.json")) == []


# ── Approvable helper: parity with approve ───────────────────────────────


def test_approvable_helper_parity_with_approve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Paper tier off: this test pins helper/approve parity on a PENDING
    # proposal, so the gate-green candidate must not auto-apply at propose.
    monkeypatch.setenv("WAYFINDER_PAPER_AUTO_APPLY", "0")
    store, job_id, root = _make_job(tmp_path)
    revision = _make_live_blocking(store, job_id, root, tmp_path)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.proposals.evaluate_economic_gate",
        lambda *a, **k: _economic(revision, ready=True, reasons=[]),
    )

    green = _propose_params(store, job_id)
    approvable, reason = proposal_approvable(store, job_id, green)
    assert approvable, reason
    approved = store.approve_proposal(job_id, green["proposal_id"])
    assert approved["status"] == "approved", "helper-approvable approves clean"

    blocked = _propose_params(store, job_id, params={"threshold": 11.3})
    blocked["candidate_report"]["economic"] = _economic(
        revision, ready=False, reasons=["median paired delta below constitution floor"]
    )
    store.write_proposal(job_id, blocked)
    reloaded = store.load_proposal(job_id, blocked["proposal_id"])

    approvable, reason = proposal_approvable(store, job_id, reloaded)
    assert approvable is False
    assert "ESCALATE" in reason
    with pytest.raises(ValueError, match="ESCALATE"):
        store.approve_proposal(job_id, blocked["proposal_id"])


# ── Part 2: needs_you filter ─────────────────────────────────────────────


def test_needs_you_excludes_unapprovable_pending(tmp_path: Path) -> None:
    store, job_id = _attention_store(tmp_path, wallet_label="main")
    _write_pending_proposal(store, job_id, "prop-good")
    store.write_proposal(
        job_id,
        {
            "proposal_id": "prop-bad",
            "job_id": job_id,
            "status": "pending",
            "kind": "params_update",
            "proposed_change": {"summary": "frozen red verdict"},
            "intent_contract": {"goal": "improve net"},
            "scenario_plan": {"scenarios": [{"name": "s1"}]},
            "application": {"status": "not_requested"},
            "candidate_report": {
                "revision": "rev-bad",
                "validation_summary": {
                    "status": "failed",
                    "failed_checks": ["scenario_entry"],
                    "failure_kind": "evidence",
                },
                "gate": {"live_ready": False, "reasons": ["validation failed"]},
                "economic": None,
            },
        },
    )

    items = build_owner_attention(store, job_id)["needs_you"]

    approvals = [i for i in items if i["kind"] == "live_proposal_approval"]
    assert [i["ref_id"] for i in approvals] == ["prop-good"]


def test_needs_you_non_live_jobs_unchanged(tmp_path: Path) -> None:
    store, job_id = _attention_store(tmp_path)  # paper, no wallet bound
    _write_pending_proposal(store, job_id, "prop-any")

    assert build_owner_attention(store, job_id)["needs_you"] == []


# ── Part 3: maintenance auto-apply lane ──────────────────────────────────


def _equivalence_candidate(root: Path, tmp_path: Path, suffix: str) -> Path:
    candidate = tmp_path / f"candidate-{suffix}"
    shutil.copytree(root / "workspace", candidate)
    strategy = candidate / "src" / "strategy.py"
    strategy.write_text(
        strategy.read_text(encoding="utf-8")
        + "\n# Implementation-only refactor marker.\n",
        encoding="utf-8",
    )
    return candidate


def test_certified_equivalence_auto_applies_on_live_capable_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root = _make_job(tmp_path)
    _make_live_blocking(store, job_id, root, tmp_path)
    candidate = _equivalence_candidate(root, tmp_path, "live-eq")
    launched: list[tuple[str, str]] = []
    monkeypatch.setattr(
        apply_launcher,
        "launch_application",
        lambda _store, jid, pid: launched.append((jid, pid)),
    )

    proposal = propose_change(
        store,
        job_id,
        kind="code_change",
        summary="Vectorize indicator computation.",
        intent_contract=_intent_contract(),
        candidate_source=candidate,
        acceptance_policy="behavior_equivalence",
    )

    pid = proposal["proposal_id"]
    assert proposal["status"] == "approved"
    assert proposal["approval"]["by"] == "maintenance-auto"
    assert proposal["approval"]["required"] is False
    assert proposal["application"]["status"] == "queued"
    assert launched == [(job_id, pid)]
    events = _journal_events(store, job_id, "maintenance_auto_applied")
    assert len(events) == 1
    assert events[0]["tier"] == "behavior_equivalence"
    assert events[0]["evidence"]["behavior_equivalent"] is True
    assert (
        events[0]["undo"]["command"] == f"wayfinder job rollback-apply {job_id} {pid}"
    )

    attention = build_owner_attention(store, job_id)
    decided = [
        item
        for item in attention["decided_autonomously"]
        if item["kind"] == "maintenance_auto_apply"
    ]
    assert decided and decided[0]["ref_id"] == pid
    assert "rollback-apply" in decided[0]["undo"]["command"]
    assert all(item["ref_id"] != pid for item in attention["needs_you"])


def test_kill_switch_leaves_maintenance_pending_in_owner_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MAINTENANCE_AUTO_APPLY_ENV, "0")
    store, job_id, root = _make_job(tmp_path)
    candidate = _equivalence_candidate(root, tmp_path, "killswitch")

    proposal = propose_change(
        store,
        job_id,
        kind="code_change",
        summary="Refactor while the lane is disabled.",
        intent_contract=_intent_contract(),
        candidate_source=candidate,
        acceptance_policy="behavior_equivalence",
    )

    pid = proposal["proposal_id"]
    assert proposal["status"] == "pending"
    assert store.proposal_queue(job_id)["pending"], "sits in the normal owner queue"
    approvable, reason = proposal_approvable(store, job_id, proposal)
    assert approvable, reason
    skipped = _journal_events(store, job_id, "maintenance_auto_apply_skipped")
    assert skipped and skipped[0]["proposal_id"] == pid
    assert any("disabled" in blocker for blocker in skipped[0]["blockers"])
    assert _journal_events(store, job_id, "maintenance_auto_applied") == []


def _equivalence_proposal(pid: str = "prop-eq", **overrides: Any) -> dict[str, Any]:
    proposal: dict[str, Any] = {
        "proposal_id": pid,
        "status": "pending",
        "kind": "code_change",
        "acceptance_policy": "behavior_equivalence",
        "proposed_change": {"summary": "refactor"},
        "application": {"status": "not_requested"},
        "candidate_report": {
            "acceptance_policy": "behavior_equivalence",
            "maintenance": {"ready": True},
            "validation_summary": {"status": "passed"},
        },
    }
    proposal.update(overrides)
    return proposal


def test_each_eligibility_condition_defeats_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "maint-elig",
        script=".wayfinder_runs/demo.py",
        interval_seconds=60,
        execution_contract="jobs_v1",
    )
    store.save(job)
    job_id = job.id

    assert maintenance_auto_apply_blockers(store, job_id, _equivalence_proposal()) == []

    monkeypatch.setenv(MAINTENANCE_AUTO_APPLY_ENV, "0")
    blockers = maintenance_auto_apply_blockers(store, job_id, _equivalence_proposal())
    assert any("disabled" in blocker for blocker in blockers)
    monkeypatch.delenv(MAINTENANCE_AUTO_APPLY_ENV)

    not_equivalence = _equivalence_proposal(acceptance_policy="economic_improvement")
    blockers = maintenance_auto_apply_blockers(store, job_id, not_equivalence)
    assert any("behavior-equivalence" in blocker for blocker in blockers)

    proof_red = _equivalence_proposal()
    proof_red["candidate_report"]["maintenance"] = {"ready": False}
    blockers = maintenance_auto_apply_blockers(store, job_id, proof_red)
    assert any("proof" in blocker for blocker in blockers)

    validation_red = _equivalence_proposal()
    validation_red["candidate_report"]["validation_summary"] = {"status": "failed"}
    blockers = maintenance_auto_apply_blockers(store, job_id, validation_red)
    assert any("validation" in blocker for blocker in blockers)

    blacklisted = _equivalence_proposal(
        proposed_change={
            "summary": "refactor",
            "execution_params": {"nested": {"max_leverage": 4}},
        }
    )
    blockers = maintenance_auto_apply_blockers(store, job_id, blacklisted)
    assert any("nested.max_leverage" in blocker for blocker in blockers)

    store.write_proposal(
        job_id,
        {
            "proposal_id": "prop-in-flight",
            "job_id": job_id,
            "status": "approved",
            "proposed_change": {"summary": "other change"},
            "application": {"status": "queued"},
        },
    )
    blockers = maintenance_auto_apply_blockers(store, job_id, _equivalence_proposal())
    assert any("in flight" in blocker for blocker in blockers)


def test_defeated_lane_falls_back_to_pending_owner_queue(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "maint-fallback",
        script=".wayfinder_runs/demo.py",
        interval_seconds=60,
        execution_contract="jobs_v1",
    )
    store.save(job)
    proposal = _equivalence_proposal(
        proposed_change={
            "summary": "refactor",
            "execution_params": {"risk_cap": 2},
        },
        job_id=job.id,
        intent_contract={"goal": "g"},
        scenario_plan={"scenarios": [{"name": "s"}]},
    )
    store.write_proposal(job.id, proposal)

    assert maybe_auto_apply_maintenance_proposal(store, job.id, "prop-eq") is None
    assert store.load_proposal(job.id, "prop-eq")["status"] == "pending"
    skipped = _journal_events(store, job.id, "maintenance_auto_apply_skipped")
    assert skipped and any("risk_cap" in blocker for blocker in skipped[0]["blockers"])


# ── Part 4: watchdog triage of blocked pendings ──────────────────────────


def _triage_job(tmp_path: Path, job_id: str) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        job_id,
        script=".wayfinder_runs/demo.py",
        interval_seconds=60,
        execution_contract="jobs_v1",
    )
    store.save(job)
    return store, job.id


def _blocked_pending(
    store: JobStore,
    job_id: str,
    pid: str,
    *,
    failure_kind: str,
    generated_at: str,
) -> None:
    store.write_proposal(
        job_id,
        {
            "proposal_id": pid,
            "job_id": job_id,
            "status": "pending",
            "kind": "params_update",
            "proposed_change": {"summary": "frozen verdict"},
            "intent_contract": {"goal": "g"},
            "scenario_plan": {"scenarios": [{"name": "s"}]},
            "application": {"status": "not_requested"},
            "candidate_report": {
                "revision": "rev-frozen",
                "generated_at": generated_at,
                "validation_summary": {
                    "status": "failed",
                    "failed_checks": ["candidate_backtest_valid"],
                    "failure_kind": failure_kind,
                },
                "gate": {"live_ready": False, "reasons": ["validation failed"]},
                "economic": None,
            },
        },
    )


def test_watchdog_revalidates_infra_frozen_pending_once_per_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_runner(monkeypatch)
    store, job_id = _triage_job(tmp_path, "triage-infra")
    _blocked_pending(
        store,
        job_id,
        "prop-frozen",
        failure_kind="infrastructure",
        generated_at=datetime.now(UTC).isoformat(),
    )
    calls: list[str] = []

    def fake_revalidate(store_: JobStore, jid: str, pid: str) -> dict[str, Any]:
        calls.append(pid)
        return store_.load_proposal(jid, pid)

    monkeypatch.setattr(
        "wayfinder_paths.jobs.proposals.revalidate_proposal", fake_revalidate
    )

    recover_stalled_applications(store=store)
    recover_stalled_applications(store=store)

    assert calls == ["prop-frozen"], "marker dedupes to one attempt per interval"
    events = _journal_events(store, job_id, "proposal_triage_revalidated")
    assert len(events) == 1
    marker = store.read_json(job_id, "state/proposal_triage.json")
    assert marker["prop-frozen"]["revalidated_at"]

    stale = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    marker["prop-frozen"]["revalidated_at"] = stale
    store.write_json(job_id, "state/proposal_triage.json", marker)
    recover_stalled_applications(store=store)
    assert calls == ["prop-frozen", "prop-frozen"], "interval elapsed → retried"
    assert store.load_proposal(job_id, "prop-frozen")["status"] == "pending"


def test_watchdog_expires_evidence_negative_past_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_runner(monkeypatch)
    store, job_id = _triage_job(tmp_path, "triage-ttl")
    old = (datetime.now(UTC) - timedelta(hours=72)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    _blocked_pending(
        store, job_id, "prop-old", failure_kind="evidence", generated_at=old
    )
    _blocked_pending(
        store, job_id, "prop-fresh", failure_kind="evidence", generated_at=fresh
    )

    result = recover_stalled_applications(store=store)

    rejected = store.load_proposal(job_id, "prop-old")
    assert rejected["status"] == "rejected"
    assert rejected["rejection"]["by"] == "watchdog"
    assert store.load_proposal(job_id, "prop-fresh")["status"] == "pending"
    expired = _journal_events(store, job_id, "proposal_expired_unapprovable")
    assert [event["proposal_id"] for event in expired] == ["prop-old"]
    assert (
        expired[0]["undo"]["command"] == f"wayfinder job revalidate {job_id} prop-old"
    )
    assert expired[0]["reasons"]
    actions = [event["action"] for event in result["recovered"] if "action" in event]
    assert "proposal_expired_unapprovable" in actions

    decided = build_owner_attention(store, job_id)["decided_autonomously"]
    entries = [item for item in decided if item["kind"] == "proposal_expired"]
    assert entries and entries[0]["ref_id"] == "prop-old"
    assert "revalidate" in entries[0]["undo"]["command"]


def test_watchdog_leaves_approvable_pending_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_runner(monkeypatch)
    store, job_id = _triage_job(tmp_path, "triage-green")
    old = (datetime.now(UTC) - timedelta(hours=100)).isoformat()
    store.write_proposal(
        job_id,
        {
            "proposal_id": "prop-green",
            "job_id": job_id,
            "status": "pending",
            "kind": "params_update",
            "proposed_change": {"summary": "green change"},
            "intent_contract": {"goal": "g"},
            "scenario_plan": {"scenarios": [{"name": "s"}]},
            "application": {"status": "not_requested"},
            "candidate_report": {
                "revision": "rev-green",
                "generated_at": old,
                "validation_summary": {"status": "passed"},
                "gate": {"live_ready": True, "reasons": []},
                "economic": {"ready": True, "enforcement": "advisory"},
            },
        },
    )

    recover_stalled_applications(store=store)

    assert store.load_proposal(job_id, "prop-green")["status"] == "pending"
    assert _journal_events(store, job_id, "proposal_expired_unapprovable") == []


def test_reject_guard_still_refuses_agent_rejection_of_approved_escalate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#697 regression pin: the triage machinery-reject must not weaken the
    guard — an APPROVED proposal whose latest failure is a fail-closed gate
    ESCALATE still refuses agent rejection, and the watchdog triage (pending
    only) never touches it."""
    _patch_runner(monkeypatch)
    store, job_id = _triage_job(tmp_path, "guard-697")
    store.write_proposal(
        job_id,
        {
            "proposal_id": "prop-approved",
            "job_id": job_id,
            "status": "approved",
            "proposed_change": {"summary": "owner-approved change"},
            "application": {
                "status": "not_requested",
                "restage_last_error": (
                    "ESCALATE: blocking governance requires economic_ready=True "
                    "for a live-capable job; got None"
                ),
            },
        },
    )

    with pytest.raises(ValueError, match="refusing agent rejection"):
        store.reject_proposal(
            job_id, "prop-approved", reason="gate is red", rejected_by="agent"
        )

    kept = store.load_proposal(job_id, "prop-approved")
    assert kept["status"] == "approved"
    assert kept["application"]["restage_requested"] is True
    assert _journal_events(store, job_id, "proposal_reject_refused")

    recover_stalled_applications(store=store)
    assert store.load_proposal(job_id, "prop-approved")["status"] == "approved"
