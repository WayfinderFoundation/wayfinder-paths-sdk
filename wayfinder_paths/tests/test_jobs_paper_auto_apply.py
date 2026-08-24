"""Paper auto-apply tier: gate-green paper proposals apply without a click.

Autonomy changes WHO clicks, not WHAT is checked — every test that auto-
applies still routes through the full `store.approve_proposal` gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wayfinder_paths.jobs import apply_launcher
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.proposals import (
    PAPER_AUTO_APPLY_ENV,
    maybe_auto_apply_paper_proposal,
    paper_auto_apply_blockers,
)
from wayfinder_paths.jobs.store import JobStore


def _store(
    tmp_path: Path,
    *,
    mode: str = "paper",
    wallet_label: str | None = None,
    auto_limits: dict | None = None,
) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "auto-demo",
        script=".wayfinder_runs/demo.py",
        interval_seconds=60,
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    job.script_loop.mode = mode
    if wallet_label:
        job.execution_params["wallet_label"] = wallet_label
    if auto_limits is not None:
        job.agent_loop.auto_limits = auto_limits
    store.save(job)
    return store, job.id


def _green_proposal(
    store: JobStore,
    job_id: str,
    pid: str = "prop-green",
    *,
    kind: str = "params_update",
    params: dict | None = None,
    scenarios: list | None = None,
) -> str:
    store.write_proposal(
        job_id,
        {
            "proposal_id": pid,
            "job_id": job_id,
            "status": "pending",
            "kind": kind,
            "proposed_change": {
                "summary": "tighten threshold",
                "execution_params": params if params is not None else {"threshold": 2},
            },
            "intent_contract": {"goal": "improve net"},
            "scenario_plan": {
                "scenarios": [{"name": "s1"}] if scenarios is None else scenarios
            },
            "application": {
                "status": "not_requested",
                "candidate_dir": f"applications/{pid}/candidate",
            },
            "candidate_report": {
                "revision": "rev-cand",
                "generated_at": "2026-08-24T00:00:00+00:00",
                "validation_summary": {"status": "passed"},
                "gate": {"live_ready": True, "reasons": []},
                "economic": {"ready": True, "enforcement": "advisory"},
            },
        },
    )
    return pid


@pytest.fixture()
def launched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    monkeypatch.setenv(PAPER_AUTO_APPLY_ENV, "1")
    calls: list[tuple[str, str]] = []

    def fake_launch(store, job_id, proposal_id):
        calls.append((job_id, proposal_id))
        return {"mode": "deterministic", "spawned": True}

    monkeypatch.setattr(apply_launcher, "launch_application", fake_launch)
    return calls


def _journal_events(store: JobStore, job_id: str, event_type: str) -> list[dict]:
    return [
        event
        for event in store.read_jsonl(job_id, "journal.jsonl")
        if event.get("type") == event_type
    ]


def test_green_paper_proposal_auto_applies(tmp_path: Path, launched) -> None:
    store, job_id = _store(tmp_path)
    pid = _green_proposal(store, job_id)

    result = maybe_auto_apply_paper_proposal(store, job_id, pid)

    assert result and result["auto_applied"] is True
    proposal = store.load_proposal(job_id, pid)
    assert proposal["status"] == "approved"
    assert proposal["approval"]["required"] is False
    assert proposal["approval"]["by"] == "paper-auto-apply"
    assert proposal["application"]["status"] == "queued"
    assert launched == [(job_id, pid)]
    events = _journal_events(store, job_id, "proposal_auto_applied")
    assert len(events) == 1
    assert events[0]["tier"] == "paper"
    assert events[0]["evidence"]["candidate_revision"] == "rev-cand"
    assert (
        f"wayfinder job rollback-apply {job_id} {pid}" == (events[0]["undo"]["command"])
    )
    assert events[0]["undo"]["window_expires_ts"]


def test_kill_switch_disables_auto_apply(
    tmp_path: Path, launched, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PAPER_AUTO_APPLY_ENV, "0")
    store, job_id = _store(tmp_path)
    pid = _green_proposal(store, job_id)

    assert maybe_auto_apply_paper_proposal(store, job_id, pid) is None
    assert store.load_proposal(job_id, pid)["status"] == "pending"
    assert launched == []


def test_live_capable_jobs_never_auto_apply(tmp_path: Path, launched) -> None:
    for variant, kwargs in (
        ("wallet", {"wallet_label": "main"}),
        ("live", {"mode": "live"}),
    ):
        store, job_id = _store(tmp_path / variant, **kwargs)
        pid = _green_proposal(store, job_id)
        assert maybe_auto_apply_paper_proposal(store, job_id, pid) is None
        assert store.load_proposal(job_id, pid)["status"] == "pending"
        blockers = paper_auto_apply_blockers(
            store, job_id, store.load_proposal(job_id, pid)
        )
        assert any("live-capable" in blocker for blocker in blockers) or any(
            "not paper" in blocker for blocker in blockers
        )
    assert launched == []


def test_non_green_reports_require_approval(tmp_path: Path, launched) -> None:
    store, job_id = _store(tmp_path)
    pid = _green_proposal(store, job_id)
    proposal = store.load_proposal(job_id, pid)
    proposal["candidate_report"]["gate"] = {"live_ready": False, "reasons": ["red"]}
    store.write_proposal(job_id, proposal)

    assert maybe_auto_apply_paper_proposal(store, job_id, pid) is None
    assert store.load_proposal(job_id, pid)["status"] == "pending"
    assert launched == []


def test_kind_limits_default_to_params_update_only(tmp_path: Path, launched) -> None:
    store, job_id = _store(tmp_path)
    pid = _green_proposal(store, job_id, "prop-code", kind="code_change")
    assert maybe_auto_apply_paper_proposal(store, job_id, pid) is None

    # auto_limits may widen the tier — but never to improver_change.
    store2, job2 = _store(
        tmp_path / "widened",
        auto_limits={"auto_apply_kinds": ["code_change", "improver_change"]},
    )
    pid_code = _green_proposal(store2, job2, "prop-code", kind="code_change")
    assert maybe_auto_apply_paper_proposal(store2, job2, pid_code) is not None
    pid_improver = _green_proposal(store2, job2, "prop-imp", kind="improver_change")
    assert maybe_auto_apply_paper_proposal(store2, job2, pid_improver) is None


def test_owner_owned_param_names_require_approval(tmp_path: Path, launched) -> None:
    store, job_id = _store(tmp_path)
    for pid, params in (
        ("prop-lev", {"leverage": 3}),
        ("prop-wallet", {"wallet_label": "main"}),
        ("prop-mode", {"protection_mode": "off"}),
        ("prop-gov", {"governance_window": 7}),
    ):
        _green_proposal(store, job_id, pid, params=params)
        assert maybe_auto_apply_paper_proposal(store, job_id, pid) is None, pid
    assert launched == []


def test_daily_cap_bounds_the_tier(tmp_path: Path, launched) -> None:
    store, job_id = _store(tmp_path)
    for i in range(3):
        store.append_journal(
            job_id, {"type": "proposal_auto_applied", "proposal_id": f"old-{i}"}
        )
    pid = _green_proposal(store, job_id)

    assert maybe_auto_apply_paper_proposal(store, job_id, pid) is None
    blockers = paper_auto_apply_blockers(
        store, job_id, store.load_proposal(job_id, pid)
    )
    assert any("cap" in blocker for blocker in blockers)
    assert launched == []


def test_approve_gate_refusal_leaves_proposal_pending(tmp_path: Path, launched) -> None:
    """Eligibility precheck can be optimistic; the REAL approve gate decides.

    An empty scenario plan passes the blockers but fails
    `_validate_applicable_proposal` inside approve — the proposal must stay
    pending on the owner path, un-mangled."""
    store, job_id = _store(tmp_path)
    pid = _green_proposal(store, job_id, scenarios=[])

    assert maybe_auto_apply_paper_proposal(store, job_id, pid) is None
    proposal = store.load_proposal(job_id, pid)
    assert proposal["status"] == "pending"
    assert proposal["approval"]["required"] is True
    assert launched == []


def test_auto_apply_launch_failure_is_watchdog_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PAPER_AUTO_APPLY_ENV, "1")

    def broken_launch(store, job_id, proposal_id):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(apply_launcher, "launch_application", broken_launch)
    store, job_id = _store(tmp_path)
    pid = _green_proposal(store, job_id)

    result = maybe_auto_apply_paper_proposal(store, job_id, pid)

    assert result and result["auto_applied"] is True
    proposal = store.load_proposal(job_id, pid)
    # Approved + queued is exactly the state _recover_queued backstops.
    assert proposal["status"] == "approved"
    assert proposal["application"]["status"] == "queued"
    assert _journal_events(store, job_id, "proposal_auto_apply_launch_failed")


def test_forbidden_params_regex_matches_documented_families(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    pid = _green_proposal(
        store, job_id, params={"nested": {"max_leverage": 5}, "threshold": 1}
    )
    blockers = paper_auto_apply_blockers(
        store, job_id, store.load_proposal(job_id, pid)
    )
    assert any("nested.max_leverage" in blocker for blocker in blockers)


def test_undo_command_round_trips_through_journal(tmp_path: Path, launched) -> None:
    store, job_id = _store(tmp_path)
    pid = _green_proposal(store, job_id)
    maybe_auto_apply_paper_proposal(store, job_id, pid)

    from wayfinder_paths.jobs.owner_attention import build_owner_attention

    decided = build_owner_attention(store, job_id)["decided_autonomously"]
    auto = [item for item in decided if item["kind"] == "paper_auto_apply"]
    assert auto and auto[0]["ref_id"] == pid
    assert json.loads(json.dumps(auto[0]["undo"]))["command"].startswith(
        "wayfinder job rollback-apply"
    )
