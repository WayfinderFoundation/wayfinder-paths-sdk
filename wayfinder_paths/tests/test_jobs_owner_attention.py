"""Two-zone owner attention: needs_you routing and the decided feed."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from wayfinder_paths.jobs import sync as sync_mod
from wayfinder_paths.jobs.halt import request_halt
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.owner_attention import (
    build_owner_attention,
    job_live_capital_risk,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import snapshot_job


def _store(
    tmp_path: Path,
    *,
    mode: str = "paper",
    wallet_label: str | None = None,
    job_id: str = "attn-demo",
) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        job_id,
        script=".wayfinder_runs/demo.py",
        interval_seconds=60,
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    job.script_loop.mode = mode
    if wallet_label:
        job.execution_params["wallet_label"] = wallet_label
    store.save(job)
    return store, job.id


def _write_pending_proposal(store: JobStore, job_id: str, pid: str) -> None:
    store.write_proposal(
        job_id,
        {
            "proposal_id": pid,
            "job_id": job_id,
            "status": "pending",
            "kind": "params_update",
            "proposed_change": {"summary": "raise threshold"},
            "application": {"status": "not_requested"},
        },
    )


def _journal(store: JobStore, job_id: str, event: dict) -> None:
    store.append_journal(job_id, event)


def _kinds(items: list[dict]) -> list[str]:
    return [item["kind"] for item in items]


# ── needs_you routing ────────────────────────────────────────────────────


def test_live_capital_risk_is_mode_or_wallet() -> None:
    paper = WayfinderJob.new("a", script="s.py", interval_seconds=60)
    assert job_live_capital_risk(paper) is False
    live = WayfinderJob.new("b", script="s.py", interval_seconds=60)
    live.script_loop.mode = "live"
    assert job_live_capital_risk(live) is True
    bound = WayfinderJob.new("c", script="s.py", interval_seconds=60)
    bound.execution_params["wallet_label"] = "main"
    assert job_live_capital_risk(bound) is True


def test_pending_proposal_on_live_capable_job_needs_owner(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path, wallet_label="main")
    _write_pending_proposal(store, job_id, "prop-live")
    _journal(store, job_id, {"type": "proposal_created", "proposal_id": "prop-live"})

    attention = build_owner_attention(store, job_id)

    items = attention["needs_you"]
    assert _kinds(items) == ["live_proposal_approval"]
    assert items[0]["ref_id"] == "prop-live"
    assert items[0]["evidence_ref"] == "proposals/prop-live.json"
    assert items[0]["since_ts"]


def test_pending_proposal_on_pure_paper_job_is_mechanical(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)  # paper, no wallet
    _write_pending_proposal(store, job_id, "prop-paper")

    attention = build_owner_attention(store, job_id)

    assert attention["needs_you"] == []


def test_risk_latched_halt_needs_owner_but_manual_does_not(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    request_halt(store, job_id, reason="dd breach", source="risk_limits")
    items = build_owner_attention(store, job_id)["needs_you"]
    assert _kinds(items) == ["halt_awaiting_owner_clear"]
    assert items[0]["ref_id"] == "risk_limits"

    store2, job2 = _store(tmp_path / "b", job_id="attn-manual")
    request_halt(store2, job2, reason="pause please", source="manual")
    assert build_owner_attention(store2, job2)["needs_you"] == []


def test_owner_review_markers_surface_and_resolve(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    _journal(
        store,
        job_id,
        {
            "type": "successor_abandoned",
            "proposal_id": "prop-dead",
            "rearms": 3,
            "owner_review_required": "successor was self-rejected 3 times",
        },
    )
    # Reject-refusal escalation on a proposal still stuck in restage.
    store.write_proposal(
        job_id,
        {
            "proposal_id": "prop-stuck",
            "job_id": job_id,
            "status": "approved",
            "proposed_change": {"summary": "x"},
            "application": {"status": "not_requested", "restage_requested": True},
        },
    )
    _journal(
        store,
        job_id,
        {
            "type": "proposal_reject_refused",
            "proposal_id": "prop-stuck",
            "failure_kind": "escalate",
            "owner_review_required": "a fail-closed gate ESCALATED",
        },
    )

    kinds = _kinds(build_owner_attention(store, job_id)["needs_you"])
    assert sorted(kinds) == ["proposal_reject_refused", "successor_abandoned"]

    # Once the restage flag clears, the reject-refusal marker resolves.
    proposal = store.load_proposal(job_id, "prop-stuck")
    proposal["application"]["restage_requested"] = False
    store.write_proposal(job_id, proposal)
    kinds = _kinds(build_owner_attention(store, job_id)["needs_you"])
    assert kinds == ["successor_abandoned"]


def test_unauditable_pending_claim_needs_owner(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)).isoformat()
    claims = {
        # No mechanical verdict after the grace window → owner.
        "claim-stuck": {"status": "pending", "lane": "imx-gap", "filed_at": old},
        # Audit produced a verdict → mechanical path owns it.
        "claim-rejected": {
            "status": "pending",
            "lane": "other",
            "filed_at": old,
            "audit": {"verdict": "reject"},
        },
        # Freshly filed → the audit simply hasn't run yet.
        "claim-fresh": {
            "status": "pending",
            "lane": "new",
            "filed_at": dt.datetime.now(dt.UTC).isoformat(),
        },
    }
    for claim_id, claim in claims.items():
        store.write_json(
            job_id,
            f"research/exhaustion_claims/{claim_id}.json",
            {"claim_id": claim_id, **claim},
        )

    items = build_owner_attention(store, job_id)["needs_you"]
    assert _kinds(items) == ["exhaustion_claim_unauditable"]
    assert items[0]["ref_id"] == "claim-stuck"


def test_tripped_decision_gate_needs_owner(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path, wallet_label="main")
    store.write_json(
        job_id,
        "research/decision_gates.json",
        {
            "gates": [
                {
                    "gate_id": "gate-imx",
                    "status": "tripped_needs_owner",
                    "on_met": "retire_and_pivot",
                    "tripped_at": "2026-08-24T00:00:00+00:00",
                },
                {"gate_id": "gate-armed", "status": "armed"},
            ]
        },
    )

    items = build_owner_attention(store, job_id)["needs_you"]
    assert _kinds(items) == ["decision_gate_tripped"]
    assert items[0]["ref_id"] == "gate-imx"


# ── decided_autonomously feed ────────────────────────────────────────────


def test_decided_feed_carries_undo_refs(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    _journal(
        store,
        job_id,
        {
            "type": "proposal_auto_applied",
            "proposal_id": "prop-auto",
            "kind": "params_update",
            "tier": "paper",
            "evidence": {"validation_status": "passed"},
            "undo": {
                "command": f"wayfinder job rollback-apply {job_id} prop-auto",
                "window_expires_ts": "2026-08-27T00:00:00+00:00",
            },
        },
    )
    _journal(
        store,
        job_id,
        {
            "type": "gate_auto_resolved",
            "gate_id": "gate-imx",
            "action": "retire_and_pivot",
            "criteria": {"min_trades": 20},
            "measured": {"closed_trades": 20},
            "undo": {
                "command": f"wayfinder job decision-gate reopen {job_id} gate-imx",
                "window_expires_ts": "2026-08-31T00:00:00+00:00",
            },
        },
    )
    _journal(
        store,
        job_id,
        {
            "type": "exhaustion_claim_audit_passed",
            "claim_id": "claim-1",
            "by": "coverage-audit",
            "audit_verdict": "narrow",
            "certificate": "research/coverage/cert-1.json",
            "owner_override_until": "2026-08-26T00:00:00+00:00",
        },
    )
    _journal(store, job_id, {"type": "probation_leg_opened", "leg": "hype-leg"})
    _journal(store, job_id, {"type": "probation_leg_killed", "leg": "hype-leg"})

    decided = build_owner_attention(store, job_id)["decided_autonomously"]

    by_kind = {item["kind"]: item for item in decided}
    assert by_kind["paper_auto_apply"]["decision"] == "auto_applied"
    assert "rollback-apply" in by_kind["paper_auto_apply"]["undo"]["command"]
    assert by_kind["decision_gate"]["decision"] == "retire_and_pivot"
    assert "decision-gate reopen" in by_kind["decision_gate"]["undo"]["command"]
    claim = by_kind["exhaustion_claim_audit"]
    assert claim["decision"] == "narrow"
    assert claim["undo"]["window_expires_ts"] == "2026-08-26T00:00:00+00:00"
    probation = [item for item in decided if item["kind"] == "probation"]
    assert {item["decision"] for item in probation} == {"opened", "killed"}
    # Feed is newest-first.
    timestamps = [item["ts"] for item in decided]
    assert timestamps == sorted(timestamps, reverse=True)


def test_decided_feed_is_windowed_and_capped(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    journal_path = store.job_dir(job_id) / "journal.jsonl"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    stale_ts = (dt.datetime.now(dt.UTC) - dt.timedelta(days=8)).isoformat()
    rows = [
        {"ts": stale_ts, "type": "proposal_auto_applied", "proposal_id": "prop-old"}
    ]
    fresh_ts = dt.datetime.now(dt.UTC).isoformat()
    rows.extend(
        {"ts": fresh_ts, "type": "proposal_auto_applied", "proposal_id": f"p{i}"}
        for i in range(60)
    )
    journal_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    decided = build_owner_attention(store, job_id)["decided_autonomously"]

    assert len(decided) == 50  # capped
    assert all(item["ref_id"] != "prop-old" for item in decided)  # 7d window


# ── snapshot integration ─────────────────────────────────────────────────


class _FakeBridge:
    def __init__(self, *, repo_root=None) -> None:
        self.repo_root = repo_root

    def job_states(self) -> dict:
        return {}


def test_snapshot_ships_owner_attention_top_level(tmp_path: Path, monkeypatch) -> None:
    store, job_id = _store(tmp_path, wallet_label="main")
    monkeypatch.setattr(sync_mod, "RunnerBridge", _FakeBridge)
    _write_pending_proposal(store, job_id, "prop-live")

    snapshot = snapshot_job(job_id, store=store)

    attention = snapshot["owner_attention"]
    assert _kinds(attention["needs_you"]) == ["live_proposal_approval"]
    assert attention["decided_autonomously"] == []


def test_builder_never_raises(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    assert build_owner_attention(store, "missing-job") == {
        "needs_you": [],
        "decided_autonomously": [],
    }
