"""Bootstrap contract: never-operational jobs nudge, then park with an undo;
monitor-mode jobs whose deploy evidence stopped replicating share the park
path. Mechanical zone only — live and wallet-bound jobs are never parked."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from wayfinder_paths.jobs.lifecycle import (
    BOOTSTRAP_FAILURE_PATH,
    LIFECYCLE_PATH,
    bootstrap_directive,
    closed_forward_trades,
    is_operational,
    lifecycle_sweep,
)
from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.owner_attention import build_owner_attention
from wayfinder_paths.jobs.store import JobStore


def _patch_bridge(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
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

    monkeypatch.setattr("wayfinder_paths.jobs.lifecycle.RunnerBridge", FakeBridge)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.lifecycle.sync_all_jobs", lambda *, store: None
    )
    return calls


def _make_job(
    store: JobStore,
    job_id: str,
    *,
    age_hours: float = 0.0,
    agent_mode: str = "intervene",
) -> WayfinderJob:
    job = WayfinderJob.new(
        job_id,
        script="workspace/src/loop.py",
        interval_seconds=60,
        agent_mode=agent_mode,
    )
    job.created_at = (
        dt.datetime.now(dt.UTC) - dt.timedelta(hours=age_hours)
    ).isoformat()
    store.save(job)
    return job


def _journal_events(store: JobStore, job_id: str) -> list[dict]:
    path = store.job_dir(job_id) / "journal.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _journal_types(store: JobStore, job_id: str) -> list[str]:
    return [str(event.get("type")) for event in _journal_events(store, job_id)]


def test_operational_predicate_truth_table(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "op-demo")
    assert is_operational(store, job.id) is False

    store.refresh_scorecard(job.id, {"last_script_run_at": utc_now_iso()})
    assert is_operational(store, job.id) is True
    store.refresh_scorecard(job.id, {"last_script_run_at": None})
    assert is_operational(store, job.id) is False

    store.refresh_scorecard(job.id, {"last_agent_check_at": utc_now_iso()})
    assert is_operational(store, job.id) is True
    store.refresh_scorecard(job.id, {"last_agent_check_at": None})
    assert is_operational(store, job.id) is False

    baseline = store.job_dir(job.id) / "results" / "backtest" / "baseline.json"
    baseline.write_text("{}\n", encoding="utf-8")
    assert is_operational(store, job.id) is True
    baseline.unlink()
    assert is_operational(store, job.id) is False

    trades = store.job_dir(job.id) / "results" / "forward" / "trades.jsonl"
    trades.write_text('{"symbol": "BTC", "net_pnl": 1.0}\n', encoding="utf-8")
    assert closed_forward_trades(store, job.id) == 1
    assert is_operational(store, job.id) is True


def test_nudge_journals_once_and_prompt_directive_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bridge(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "nudge-demo", age_hours=40)  # past 72/2, before 72

    first = lifecycle_sweep(store, force=True)
    second = lifecycle_sweep(store, force=True)

    assert any(a["action"] == "bootstrap_lagging" for a in first["actions"])
    assert not any(a["action"] == "bootstrap_lagging" for a in second["actions"])
    assert _journal_types(store, job.id).count("bootstrap_lagging") == 1
    assert "never reached operational state" in bootstrap_directive(store, job.id)

    # An operational job renders no directive even past the nudge threshold.
    store.refresh_scorecard(job.id, {"last_script_run_at": utc_now_iso()})
    assert bootstrap_directive(store, job.id) == ""


def test_park_pauses_loops_writes_state_and_journals_undo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_bridge(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "park-demo", age_hours=80)

    result = lifecycle_sweep(store, force=True)

    assert any(a["action"] == "job_parked_unbootstrapped" for a in result["actions"])
    assert ("pause", job.script_loop.runner_job_name) in calls
    assert ("pause", job.agent_loop.runner_job_name) in calls
    failure = store.read_json(job.id, BOOTSTRAP_FAILURE_PATH)
    assert failure["reason"].startswith("never operational")
    assert failure["parked_at"]
    assert "no_forward_trades" in failure["predicates_failed"]
    parked = [
        e
        for e in _journal_events(store, job.id)
        if e.get("type") == "job_parked_unbootstrapped"
    ]
    assert len(parked) == 1
    assert parked[0]["undo"] == {"command": f"wayfinder job resume {job.id}"}

    # Once parked, the sweep never re-parks (an owner resume is deliberate).
    calls.clear()
    again = lifecycle_sweep(store, force=True)
    assert not any(a["action"].startswith("job_parked") for a in again["actions"])
    assert calls == []


def test_live_and_wallet_bound_jobs_are_never_parked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_bridge(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    live = _make_job(store, "live-demo", age_hours=80)
    live.script_loop.mode = "live"
    store.save(live)
    bound = _make_job(store, "bound-demo", age_hours=80)
    bound.execution_params = {"wallet_label": "main"}
    store.save(bound)

    result = lifecycle_sweep(store, force=True)

    assert not any(a["action"].startswith("job_parked") for a in result["actions"])
    assert calls == []
    assert store.read_json(live.id, BOOTSTRAP_FAILURE_PATH) is None
    assert store.read_json(bound.id, BOOTSTRAP_FAILURE_PATH) is None
    # The nudge (visibility) still lands — only the park is exempt.
    assert "bootstrap_lagging" in _journal_types(store, live.id)


def test_reaper_kill_switch_disables_park_but_keeps_nudge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_bridge(monkeypatch)
    monkeypatch.setenv("WAYFINDER_LIFECYCLE_REAPER", "0")
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "killswitch-demo", age_hours=80)

    result = lifecycle_sweep(store, force=True)

    assert not any(a["action"].startswith("job_parked") for a in result["actions"])
    assert calls == []
    assert "bootstrap_lagging" in _journal_types(store, job.id)
    assert "never reached operational state" in bootstrap_directive(store, job.id)


def test_monitor_decay_parks_after_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_bridge(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "decay-demo", agent_mode="monitor")
    store.refresh_scorecard(job.id, {"last_script_run_at": utc_now_iso()})
    store.write_json(
        job.id,
        "results/backtest/replication.json",
        {"available": True, "status": "decayed"},
    )
    now = dt.datetime.now(dt.UTC)

    # First sighting stamps the clock — no park yet.
    first = lifecycle_sweep(store, now=now, force=True)
    assert not any(a["action"].startswith("job_parked") for a in first["actions"])
    marker = store.read_json(job.id, LIFECYCLE_PATH)
    assert marker["monitor_decay"]["status"] == "decayed"

    # Inside the 7-day window: still no park.
    mid = lifecycle_sweep(store, now=now + dt.timedelta(days=3), force=True)
    assert not any(a["action"].startswith("job_parked") for a in mid["actions"])

    later = now + dt.timedelta(days=8)
    result = lifecycle_sweep(store, now=later, force=True)

    parked = [a for a in result["actions"] if a["action"] == "job_parked_monitor_decay"]
    assert parked and "replication decayed" in parked[0]["reason"]
    assert ("pause", job.script_loop.runner_job_name) in calls
    events = [
        e
        for e in _journal_events(store, job.id)
        if e.get("type") == "job_parked_monitor_decay"
    ]
    assert events[0]["undo"] == {"command": f"wayfinder job resume {job.id}"}


def test_monitor_decay_deferred_by_activity_and_cleared_on_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bridge(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "decay-active-demo", agent_mode="monitor")
    store.refresh_scorecard(job.id, {"last_script_run_at": utc_now_iso()})
    store.write_json(
        job.id,
        "results/backtest/replication.json",
        {"available": True, "status": "invalid"},
    )
    now = dt.datetime.now(dt.UTC)
    lifecycle_sweep(store, now=now, force=True)

    # Journal activity inside the window defers the park.
    store.append_journal(job.id, {"type": "agent_wakeup", "mode": "monitor"})
    deferred = lifecycle_sweep(store, now=now + dt.timedelta(days=8), force=True)
    assert not any(a["action"].startswith("job_parked") for a in deferred["actions"])

    # Replication recovery clears the decay clock entirely.
    store.write_json(
        job.id,
        "results/backtest/replication.json",
        {"available": True, "status": "valid"},
    )
    lifecycle_sweep(store, now=now + dt.timedelta(days=9), force=True)
    assert "monitor_decay" not in (store.read_json(job.id, LIFECYCLE_PATH) or {})


def test_sweep_daily_throttle_and_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bridge(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    _make_job(store, "throttle-demo")

    first = lifecycle_sweep(store)
    throttled = lifecycle_sweep(store)
    forced = lifecycle_sweep(store, force=True)
    next_day = lifecycle_sweep(
        store, now=dt.datetime.now(dt.UTC) + dt.timedelta(hours=25)
    )

    assert first["throttled"] is False and first["scanned"] == 1
    assert throttled["throttled"] is True and throttled["scanned"] == 0
    assert forced["throttled"] is False
    assert next_day["throttled"] is False


def test_worker_prompt_carries_bootstrap_directive(tmp_path: Path) -> None:
    from wayfinder_paths.jobs.worker import _build_worker_prompt_sections
    from wayfinder_paths.tests.test_wayfinder_jobs import _worker_snapshot

    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "prompt-demo", age_hours=40)

    prompt = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job),
    )["prompt"]
    assert "never reached operational state" in prompt

    store.refresh_scorecard(job.id, {"last_script_run_at": utc_now_iso()})
    operational_prompt = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode="intervene",
        snapshot=_worker_snapshot(job),
    )["prompt"]
    assert "never reached operational state" not in operational_prompt


def test_owner_attention_surfaces_parks_with_undo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bridge(monkeypatch)
    store = JobStore(repo_root=tmp_path)
    job = _make_job(store, "attention-demo", age_hours=80)
    lifecycle_sweep(store, force=True)

    feed = build_owner_attention(store, job.id)

    parks = [
        item
        for item in feed["decided_autonomously"]
        if item["kind"] == "lifecycle_park"
    ]
    assert len(parks) == 1
    assert parks[0]["decision"] == "unbootstrapped"
    assert parks[0]["undo"] == {"command": f"wayfinder job resume {job.id}"}
    # The closed needs_you kind union is untouched.
    assert all(item["kind"] != "lifecycle_park" for item in feed["needs_you"])
