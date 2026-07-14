"""Job status/stats must reflect RUNTIME truth (the runner env + engine),
not the declared job.yaml. The driver executes the mode baked into the runner
env WAYFINDER_JOB_MODE, which an agent can flip without touching job.yaml —
that split-brain once left a job live-trading while the UI read "paper".
snapshot_job reconciles the runner state into the scorecard; the driver
fail-safes to paper when the two diverge.
"""

from __future__ import annotations

from pathlib import Path

from wayfinder_paths.jobs import sync as sync_mod
from wayfinder_paths.jobs.execution import driver as driver_mod
from wayfinder_paths.jobs.execution.driver import (
    _tick_trigger_events,
    run_scheduled_tick,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import snapshot_job


def _job(tmp_path: Path, mode: str = "paper") -> tuple[JobStore, WayfinderJob]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "carry", script="strategy.py", interval_seconds=3600, agent_mode="monitor"
    )
    job.script_loop.mode = mode
    store.create_job(job)
    return store, job


class _FakeBridge:
    """Fake RunnerBridge exposing job_states — {name: full_runner_dict}."""

    def __init__(self, states: dict) -> None:
        self._states = states

    def __call__(self, *, repo_root=None):
        return self

    def job_states(self) -> dict:
        return self._states


def _script_state(**env) -> dict:
    return {
        "status": "ACTIVE",
        "payload": {"env": {"WAYFINDER_JOB_MODE": "live", **env}},
        "last_run_at": 1784045000,
        "last_ok_at": 1784045014,
        "next_run_at": 1784047289,
        "consecutive_failures": 0,
        "last_error": None,
    }


def test_snapshot_reconciles_runtime_mode_and_flags_mismatch(tmp_path, monkeypatch):
    # Declared paper, but the runner env runs live — the exact imx-short bug.
    store, job = _job(tmp_path, mode="paper")
    monkeypatch.setattr(
        sync_mod, "RunnerBridge", _FakeBridge({"carry-script": _script_state()})
    )
    sc = snapshot_job(job.id, store=store)["scorecard"]
    assert sc["mode"] == "live"  # runtime truth, not declared "paper"
    assert sc["mode_mismatch"] is True
    assert sc["runner_status"] == "ACTIVE"


def test_snapshot_mode_matches_no_mismatch(tmp_path, monkeypatch):
    store, job = _job(tmp_path, mode="live")
    monkeypatch.setattr(
        sync_mod, "RunnerBridge", _FakeBridge({"carry-script": _script_state()})
    )
    sc = snapshot_job(job.id, store=store)["scorecard"]
    assert sc["mode"] == "live"
    assert sc["mode_mismatch"] is False


def test_snapshot_surfaces_runner_metrics_as_iso(tmp_path, monkeypatch):
    store, job = _job(tmp_path, mode="live")
    state = _script_state()
    state["consecutive_failures"] = 3
    state["last_error"] = "reconcile_fetch_failed"
    monkeypatch.setattr(sync_mod, "RunnerBridge", _FakeBridge({"carry-script": state}))
    sc = snapshot_job(job.id, store=store)["scorecard"]
    assert sc["last_run_at"].startswith("2026-")  # unix -> ISO
    assert sc["next_run_at"].startswith("2026-")
    assert sc["consecutive_failures"] == 3
    assert sc["last_error"] == "reconcile_fetch_failed"


def test_snapshot_reconciles_agent_mode_and_revision(tmp_path, monkeypatch):
    store, job = _job(tmp_path, mode="live")
    monkeypatch.setattr(
        sync_mod,
        "RunnerBridge",
        _FakeBridge(
            {
                "carry-script": _script_state(WAYFINDER_JOB_REVISION="rev123"),
                "carry-agent": {
                    "status": "ACTIVE",
                    "payload": {"env": {"WAYFINDER_JOB_AGENT_MODE": "intervene"}},
                },
            }
        ),
    )
    sc = snapshot_job(job.id, store=store)["scorecard"]
    assert sc["agent_mode"] == "intervene"
    assert sc["active_revision"] == "rev123"


def test_snapshot_falls_back_to_engine_mode(tmp_path, monkeypatch):
    # Runner env lacks WAYFINDER_JOB_MODE -> use engine_state.json (last ran as).
    store, job = _job(tmp_path, mode="paper")
    store.write_json(job.id, "state/engine_state.json", {"mode": "live"})
    state = {"status": "ACTIVE", "payload": {"env": {}}}
    monkeypatch.setattr(sync_mod, "RunnerBridge", _FakeBridge({"carry-script": state}))
    sc = snapshot_job(job.id, store=store)["scorecard"]
    assert sc["mode"] == "live"
    assert sc["mode_mismatch"] is True


def test_snapshot_degrades_when_runner_down(tmp_path, monkeypatch):
    # Empty states == unreachable daemon -> no runtime overlay at all.
    store, job = _job(tmp_path, mode="paper")
    monkeypatch.setattr(sync_mod, "RunnerBridge", _FakeBridge({}))
    sc = snapshot_job(job.id, store=store)["scorecard"]
    assert "mode" not in sc
    assert "paused" not in sc


def test_paused_still_reconciled(tmp_path, monkeypatch):
    store, job = _job(tmp_path, mode="live")
    state = _script_state()
    state["status"] = "PAUSED"
    monkeypatch.setattr(sync_mod, "RunnerBridge", _FakeBridge({"carry-script": state}))
    assert snapshot_job(job.id, store=store)["scorecard"]["paused"] is True


def test_driver_downgrades_live_under_paper(tmp_path, monkeypatch):
    # The fail-safe: env says live, job.yaml says paper -> execute paper +
    # emit a mode_divergence guard that wakes the advisor.
    store, job = _job(tmp_path, mode="paper")
    captured: dict = {}

    def _fake_tick(job_arg, root, mode, **kw):
        captured["mode"] = mode
        return {"ok": True, "snapshot": {"status": "ok"}}

    monkeypatch.setattr(driver_mod, "tick_job", _fake_tick)
    monkeypatch.setattr(driver_mod, "fire_triggers", lambda *a, **k: None)
    monkeypatch.setenv("WAYFINDER_JOB_DIR", str(store.job_dir(job.id)))
    monkeypatch.setenv("WAYFINDER_JOB_MODE", "live")

    payload = run_scheduled_tick()
    assert captured["mode"] == "paper"  # downgraded, no live orders
    kinds = {g["kind"] for g in payload["guard_events"]}
    assert "mode_divergence" in kinds
    assert "reconcile_mismatch" in _tick_trigger_events(payload)


def test_driver_runs_live_when_declared_live(tmp_path, monkeypatch):
    store, job = _job(tmp_path, mode="live")
    captured: dict = {}
    monkeypatch.setattr(
        driver_mod,
        "tick_job",
        lambda job_arg, root, mode, **kw: captured.update(mode=mode) or {"ok": True},
    )
    monkeypatch.setattr(driver_mod, "fire_triggers", lambda *a, **k: None)
    monkeypatch.setenv("WAYFINDER_JOB_DIR", str(store.job_dir(job.id)))
    monkeypatch.setenv("WAYFINDER_JOB_MODE", "live")
    payload = run_scheduled_tick()
    assert captured["mode"] == "live"
    assert not payload.get("guard_events")
