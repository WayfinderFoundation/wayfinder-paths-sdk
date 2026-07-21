"""Event-driven agent wakes: agent_loop.triggers finally has a consumer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs.execution.driver import _tick_trigger_events
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.triggers import WAKE_STATE_PATH, fire_triggers


@pytest.fixture
def wakes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_worker(job_id: str, *, mode: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"job_id": job_id, "mode": mode, **kwargs})
        return {"status": "queued"}

    monkeypatch.setattr("wayfinder_paths.jobs.worker.run_job_worker", fake_worker)
    return calls


def _make_job(tmp_path: Path, *, mode: str = "intervene") -> tuple[JobStore, Any]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "trigger-demo",
        script=".wayfinder/jobs/trigger-demo/workspace/src/s.py",
        interval_seconds=300,
        agent_mode=mode,  # type: ignore[arg-type]
        execution_contract="jobs_v1",
    )
    store.save(job)
    return store, job


def test_matching_trigger_fires_once_and_debounces(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job = _make_job(tmp_path)

    first = fire_triggers(store, job, ["risk_halt"], source="test")
    assert first is not None
    assert first["triggers"] == ["risk_halt"]
    assert len(wakes) == 1
    assert wakes[0]["mode"] == "intervene"
    journal = (store.job_dir(job.id) / "journal.jsonl").read_text(encoding="utf-8")
    assert "agent_triggered_wake" in journal

    # Second event inside the debounce window: suppressed.
    second = fire_triggers(store, job, ["drift_warning"], source="test")
    assert second is None
    assert len(wakes) == 1


def test_debounce_window_expiry_allows_next_wake(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job = _make_job(tmp_path)
    fire_triggers(store, job, ["risk_halt"], source="test")

    # Age the wake state past the window.
    wake_path = store.job_dir(job.id) / WAKE_STATE_PATH
    stale_ts = datetime.now(UTC) - timedelta(seconds=3600)
    wake_path.write_text(
        json.dumps({"last_triggered_wake_ts": stale_ts.isoformat()}),
        encoding="utf-8",
    )

    assert fire_triggers(store, job, ["risk_halt"], source="test") is not None
    assert len(wakes) == 2


def test_non_matching_and_disabled_agents_never_fire(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job = _make_job(tmp_path)
    assert fire_triggers(store, job, ["unknown_event"], source="test") is None

    off_store, off_job = _make_job(tmp_path / "off", mode="off")
    assert fire_triggers(off_store, off_job, ["risk_halt"], source="test") is None
    assert wakes == []


def test_applying_application_suppresses_wakes(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job = _make_job(tmp_path)
    store.refresh_scorecard(job.id, {})
    scorecard = store.read_json(job.id, "scorecard.json", default={})
    scorecard["applying_proposal_applications"] = 1
    store.write_json(job.id, "scorecard.json", scorecard)

    assert fire_triggers(store, job, ["risk_halt"], source="test") is None
    assert wakes == []


def test_worker_failure_is_journaled_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job = _make_job(tmp_path)

    def boom(job_id: str, *, mode: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("opencode unreachable")

    monkeypatch.setattr("wayfinder_paths.jobs.worker.run_job_worker", boom)
    assert fire_triggers(store, job, ["risk_halt"], source="test") is None
    journal = (store.job_dir(job.id) / "journal.jsonl").read_text(encoding="utf-8")
    assert "agent_trigger_wake_failed" in journal


def test_default_triggers_include_risk_halt() -> None:
    job = WayfinderJob.new(
        "d", script="x.py", interval_seconds=60, agent_mode="monitor"
    )
    assert "risk_halt" in job.agent_loop.triggers


def test_tick_trigger_event_derivation() -> None:
    assert _tick_trigger_events({"ok": False}) == ["script_failure"]
    assert _tick_trigger_events({"ok": True, "snapshot": {"status": "ambiguous"}}) == [
        "reconcile_mismatch"
    ]
    assert _tick_trigger_events(
        {"ok": True, "guard_events": [{"kind": "manual_halt"}]}
    ) == ["risk_halt"]
    assert _tick_trigger_events(
        {"ok": True, "guard_events": [{"kind": "risk_halt"}]}
    ) == ["risk_halt"]
    assert _tick_trigger_events({"ok": True, "snapshot": {"status": "valid"}}) == []
