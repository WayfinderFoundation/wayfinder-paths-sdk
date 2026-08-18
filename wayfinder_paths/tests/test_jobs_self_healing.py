"""Self-healing pipeline: failure taxonomy, mechanical compute-status truth,
durable restage (agents cannot bury approved work on infra failures),
process-vs-substantive rejection kinds with successor expectations, and
continuation-aware island scheduling.

Motivating incident: one production OOM cascaded into 14h of frozen research
— "OOM-blocked" agendas, lanes marked exhausted, and an owner-approved
proposal self-rejected because its mechanical re-stage failed inside the OOM
window. The asymmetry under test: INFRASTRUCTURE failures self-repair;
EVIDENCE failures still stop the line.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs.failures import classify_failure
from wayfinder_paths.jobs.improver.scheduler import (
    assign_island,
    load_scheduler_state,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.triggers import ALWAYS_WAKE_EVENTS
from wayfinder_paths.jobs.watchdog import (
    _recover_restage,
    recover_stalled_applications,
)


def _make_store(tmp_path: Path, job_id: str = "heal-demo") -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        job_id,
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(job)
    return store, job.id


def _journal_events(store: JobStore, job_id: str, event_type: str) -> list[dict]:
    path = store.job_dir(job_id) / "journal.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return [row for row in rows if row.get("type") == event_type]


@pytest.fixture
def wakes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_worker(job_id: str, *, mode: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"job_id": job_id, "mode": mode, **kwargs})
        return {"status": "queued"}

    monkeypatch.setattr("wayfinder_paths.jobs.worker.run_job_worker", fake_worker)
    return calls


def test_classify_failure_taxonomy() -> None:
    infrastructure = [
        "backtest process was killed (signal 9) — likely out of memory",
        "OOM while building the grid",
        "heavy-compute lock busy",
        "request timed out after 300s",
        "HTTP 503 from opencode",
        "Event loop is closed",
        "opencode-unavailable: prompt_async failed",
        "Resource temporarily unavailable",
    ]
    for text in infrastructure:
        assert classify_failure(text) == "infrastructure", text
    evidence = [
        "candidate validation is not passed: failed",
        "candidate gate is not live-ready: net_return below baseline",
        "thesis refuted by walk-forward folds",
        "",
    ]
    for text in evidence:
        assert classify_failure(text) == "evidence", text


def test_compute_status_block_in_wake_prompt(tmp_path: Path) -> None:
    from wayfinder_paths.jobs.worker import prepare_job_worker_prompt

    store, job_id = _make_store(tmp_path, "heal-prompt")
    sections = prepare_job_worker_prompt(store=store, job_id=job_id, mode="intervene")
    assert "compute_status" in sections["prompt"]
    assert "mem_available_mb" in sections["prompt"]
    assert "FORBIDDEN as reasons" in sections["prompt"]  # the _basis contract
    # Stable-prefix rules: infra claims cite compute_status; process
    # rejections are invitations, not vetoes.
    assert "compute_status" in sections["stable_prefix"]
    assert "INVITATIONS" in sections["stable_prefix"]


def _approved_proposal(error: str | None) -> dict[str, Any]:
    return {
        "proposal_id": "prop-heal",
        "status": "approved",
        "proposed_change": {"summary": "x", "execution_params": {"a": 1}},
        "application": {
            "status": "failed",
            "restage_requested": True,
            "error": error,
        },
    }


def test_agent_cannot_bury_approved_work_on_infra_failure(tmp_path: Path) -> None:
    store, job_id = _make_store(tmp_path)
    store.write_proposal(
        job_id,
        _approved_proposal("process was killed (signal 9) — likely out of memory"),
    )
    with pytest.raises(ValueError, match="infrastructure-class"):
        store.reject_proposal(
            job_id, "prop-heal", reason="re-stage failed", rejected_by="agent"
        )
    kept = store.load_proposal(job_id, "prop-heal")
    assert kept["status"] == "approved"  # approval survives the box failure
    assert kept["application"]["restage_requested"] is True
    assert _journal_events(store, job_id, "proposal_reject_refused")

    # The owner may still abandon approved work — the guard binds agents only.
    rejected = store.reject_proposal(
        job_id, "prop-heal", reason="no longer wanted", rejected_by="owner"
    )
    assert rejected["status"] == "rejected"


def test_agent_reject_of_evidence_failure_still_passes(tmp_path: Path) -> None:
    store, job_id = _make_store(tmp_path)
    store.write_proposal(
        job_id, _approved_proposal("candidate gate is not live-ready: worse net_return")
    )
    rejected = store.reject_proposal(
        job_id,
        "prop-heal",
        reason="superseded by prop-next",
        rejected_by="agent",
    )
    assert rejected["status"] == "rejected"  # evidence failures stop the line
    assert rejected["rejection"]["kind"] == "process"  # superseded → process


def test_rejection_kind_heuristic_and_successor_expectation(tmp_path: Path) -> None:
    store, job_id = _make_store(tmp_path)
    store.write_proposal(
        job_id,
        {
            "proposal_id": "prop-a",
            "status": "pending",
            "application": {"status": "not_requested"},
        },
    )
    store.write_proposal(
        job_id,
        {
            "proposal_id": "prop-b",
            "status": "pending",
            "application": {"status": "not_requested"},
        },
    )
    # Owner process rejection (re-stage mechanics) → successor expected.
    rejected = store.reject_proposal(
        job_id, "prop-a", reason="restage failed under red gate", rejected_by="owner"
    )
    assert rejected["rejection"]["kind"] == "process"
    expected = store.read_json(job_id, "state/successor_expected.json")
    assert [entry["proposal_id"] for entry in expected] == ["prop-a"]

    # Owner substantive rejection → binding veto, no successor expectation.
    rejected = store.reject_proposal(
        job_id, "prop-b", reason="thesis refuted by walk-forward", rejected_by="owner"
    )
    assert rejected["rejection"]["kind"] == "substantive"
    expected = store.read_json(job_id, "state/successor_expected.json")
    assert [entry["proposal_id"] for entry in expected] == ["prop-a"]

    # Explicit kind overrides the heuristic.
    store.write_proposal(
        job_id,
        {
            "proposal_id": "prop-c",
            "status": "pending",
            "application": {"status": "not_requested"},
        },
    )
    rejected = store.reject_proposal(
        job_id, "prop-c", reason="try again with tighter stop", kind="process"
    )
    assert rejected["rejection"]["kind"] == "process"


def test_successor_overdue_journals_once_and_wakes_agent(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job_id = _make_store(tmp_path)
    stale_ts = (datetime.now(UTC) - timedelta(hours=13)).isoformat()
    store.write_json(
        job_id,
        "state/successor_expected.json",
        [{"proposal_id": "prop-old", "ts": stale_ts, "reason": "superseded"}],
    )

    result = recover_stalled_applications(store=store)
    events = [e for e in result["recovered"] if e.get("action") == "successor_overdue"]
    assert len(events) == 1
    assert len(_journal_events(store, job_id, "successor_overdue")) == 1
    assert wakes and wakes[0]["job_id"] == job_id  # trigger wake fired
    assert "successor_overdue" in ALWAYS_WAKE_EVENTS

    # Second pass: entry is marked notified — no duplicate journal/wake.
    result = recover_stalled_applications(store=store)
    assert not [
        e for e in result["recovered"] if e.get("action") == "successor_overdue"
    ]
    assert len(_journal_events(store, job_id, "successor_overdue")) == 1
    assert len(wakes) == 1


def test_successor_overdue_suppressed_when_successor_exists(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job_id = _make_store(tmp_path)
    stale_ts = (datetime.now(UTC) - timedelta(hours=13)).isoformat()
    store.write_json(
        job_id,
        "state/successor_expected.json",
        [{"proposal_id": "prop-old", "ts": stale_ts, "reason": "superseded"}],
    )
    store.write_proposal(
        job_id,
        {
            "proposal_id": "prop-successor",
            "status": "pending",
            "application": {"status": "not_requested"},
            "candidate_report": {"generated_at": datetime.now(UTC).isoformat()},
        },
    )
    result = recover_stalled_applications(store=store)
    assert not [
        e for e in result["recovered"] if e.get("action") == "successor_overdue"
    ]
    assert not _journal_events(store, job_id, "successor_overdue")
    assert not wakes


def test_watchdog_retries_infra_failed_restage_with_bounded_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id = _make_store(tmp_path)
    store.write_proposal(job_id, _approved_proposal(None))

    def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("backtest child killed — out of memory")

    monkeypatch.setattr("wayfinder_paths.jobs.proposals.restage_proposal", boom)
    now = datetime.now(UTC)

    for attempt in range(1, 6):
        proposal = store.load_proposal(job_id, "prop-heal")
        event = _recover_restage(store, job_id, proposal, now)
        assert event is None  # failure journaled, retry left to the next pass
        proposal = store.load_proposal(job_id, "prop-heal")
        assert proposal["application"]["restage_attempts"] == attempt
        assert proposal["application"]["restage_requested"] is True  # kept alive

    # Attempt 6: budget exhausted — escalate once, then stand down.
    proposal = store.load_proposal(job_id, "prop-heal")
    event = _recover_restage(store, job_id, proposal, now)
    assert event is not None
    assert event["action"] == "restage_attempts_exhausted"
    proposal = store.load_proposal(job_id, "prop-heal")
    assert _recover_restage(store, job_id, proposal, now) is None  # no re-escalate


def test_scheduler_prefers_continuation_island(tmp_path: Path) -> None:
    store, job_id = _make_store(tmp_path, "heal-sched")
    first = assign_island(store, job_id)
    assert first["island"] is not None

    ops_dir = store.job_dir(job_id) / "state" / "background_ops"
    ops_dir.mkdir(parents=True, exist_ok=True)
    op_path = ops_dir / "backtest_job.json"
    op_status = {
        "op": "backtest_job",
        "state": "done",
        "finished_at": datetime.now(UTC).isoformat(),
        "island": first["island"],
    }
    op_path.write_text(json.dumps(op_status))

    second = assign_island(store, job_id)
    assert second["island"] == first["island"]  # thread not abandoned
    assert any("continuation" in reason for reason in second["reasons"])
    state = load_scheduler_state(store, job_id)
    assert state["total"] == 2  # counted as a normal assignment
    assert state["history"][-1]["island"] == first["island"]

    # A running op with a live pid also pins the rotation.
    op_path.write_text(
        json.dumps({"op": "backtest_job", "state": "running", "pid": os.getpid()})
    )
    third = assign_island(store, job_id)
    assert third["island"] == first["island"]

    # Harvested (or stale) results release the rotation to normal deficit.
    op_path.write_text(json.dumps({**op_status, "harvested": True}))
    fourth = assign_island(store, job_id)
    assert not any("continuation" in reason for reason in fourth["reasons"])
