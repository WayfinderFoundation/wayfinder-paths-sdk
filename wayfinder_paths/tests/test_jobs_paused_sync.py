"""A paused job must sync a `paused` flag so the UI stops showing "Live".

The runner keeps a job's `script_loop.mode` at "live" while it's paused, so the
synced snapshot reconciles the real runner status into scorecard.paused.
"""

from __future__ import annotations

from pathlib import Path

from wayfinder_paths.jobs import sync as sync_mod
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import snapshot_job


def _job(tmp_path: Path) -> tuple[JobStore, WayfinderJob]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "carry", script="strategy.py", interval_seconds=3600, agent_mode="monitor"
    )
    store.create_job(job)
    return store, job


class _FakeBridge:
    def __init__(self, states: dict) -> None:
        # Accepts either {name: status_str} or {name: full_state_dict}.
        self._states = {
            n: (s if isinstance(s, dict) else {"status": s}) for n, s in states.items()
        }

    def __call__(self, *, repo_root=None):  # constructed as RunnerBridge(repo_root=…)
        return self

    def job_states(self) -> dict:
        return self._states

    def job_statuses(self) -> dict[str, str]:
        return {n: str(s.get("status") or "") for n, s in self._states.items()}


def test_paused_script_loop_sets_scorecard_paused(tmp_path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    monkeypatch.setattr(
        sync_mod, "RunnerBridge", _FakeBridge({"carry-script": "PAUSED"})
    )
    assert snapshot_job(job.id, store=store)["scorecard"]["paused"] is True


def test_running_loops_report_not_paused(tmp_path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    monkeypatch.setattr(
        sync_mod,
        "RunnerBridge",
        _FakeBridge({"carry-script": "OK", "carry-agent": "OK"}),
    )
    assert snapshot_job(job.id, store=store)["scorecard"]["paused"] is False


def test_down_runner_leaves_scorecard_untouched(tmp_path, monkeypatch) -> None:
    # Empty status map == unreachable daemon → don't assert paused either way.
    store, job = _job(tmp_path)
    monkeypatch.setattr(sync_mod, "RunnerBridge", _FakeBridge({}))
    assert "paused" not in snapshot_job(job.id, store=store)["scorecard"]
