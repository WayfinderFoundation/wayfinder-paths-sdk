"""The frontend's per-job Conversations list links wake sessions via
`reports.{mode}.session_id`. The wake agent overwrites reports/{mode}/latest.json
with its own structured finding, dropping session_id/created_at — so the SDK
keeps a durable sidecar and snapshot_job backfills from it.
"""

from __future__ import annotations

from pathlib import Path

from wayfinder_paths.jobs import sync as sync_mod
from wayfinder_paths.jobs import worker as worker_mod
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import _report_with_session, snapshot_job
from wayfinder_paths.jobs.worker import _write_report


def _job(tmp_path: Path) -> tuple[JobStore, WayfinderJob]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "carry", script="strategy.py", interval_seconds=3600, agent_mode="monitor"
    )
    store.create_job(job)
    return store, job


class _FakeBridge:
    def __call__(self, *, repo_root=None):
        return self

    def job_states(self) -> dict:
        return {}

    def job_statuses(self) -> dict[str, str]:
        return {}


def _clobber(store: JobStore, job_id: str, mode: str) -> None:
    """Simulate the wake agent overwriting latest.json with its own finding —
    no session_id, timestamp under a different key."""
    store.write_json(
        job_id,
        f"reports/{mode}/latest.json",
        {"mode": mode, "health": "green", "checked_at": "2026-07-13T23:08:45Z"},
    )


def test_write_report_persists_session_sidecar(tmp_path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    monkeypatch.setattr(worker_mod, "sync_all_jobs", lambda *a, **k: None)
    _write_report(
        store=store,
        job_id=job.id,
        mode="monitor",
        status="green",
        summary="queued",
        session_id="ses_abc",
        queued=True,
        error=None,
    )
    sidecar = store.read_json(job.id, "reports/monitor/session.json", default=None)
    assert sidecar["session_id"] == "ses_abc"
    assert sidecar["created_at"]


def test_write_report_keeps_sidecar_when_session_missing(tmp_path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    monkeypatch.setattr(worker_mod, "sync_all_jobs", lambda *a, **k: None)
    _write_report(
        store=store,
        job_id=job.id,
        mode="monitor",
        status="green",
        summary="ok",
        session_id="ses_good",
        queued=True,
        error=None,
    )
    # A later wake that fails to get a session must NOT blank the good pointer.
    _write_report(
        store=store,
        job_id=job.id,
        mode="monitor",
        status="yellow",
        summary="no session",
        session_id=None,
        queued=False,
        error="down",
    )
    sidecar = store.read_json(job.id, "reports/monitor/session.json", default=None)
    assert sidecar["session_id"] == "ses_good"


def test_snapshot_backfills_session_from_sidecar(tmp_path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    monkeypatch.setattr(sync_mod, "RunnerBridge", _FakeBridge())
    store.write_json(
        job.id,
        "reports/monitor/session.json",
        {"session_id": "ses_wake", "created_at": "2026-07-13T23:08:45Z"},
    )
    _clobber(store, job.id, "monitor")  # agent finding, no session_id
    report = snapshot_job(job.id, store=store)["reports"]["monitor"]
    assert report["session_id"] == "ses_wake"
    assert report["created_at"] == "2026-07-13T23:08:45Z"
    assert report["health"] == "green"  # the agent's finding is preserved


def test_snapshot_prefers_report_session_over_sidecar(tmp_path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    monkeypatch.setattr(sync_mod, "RunnerBridge", _FakeBridge())
    store.write_json(
        job.id,
        "reports/monitor/session.json",
        {"session_id": "ses_stale", "created_at": "2026-07-01T00:00:00Z"},
    )
    store.write_json(
        job.id,
        "reports/monitor/latest.json",
        {
            "mode": "monitor",
            "session_id": "ses_fresh",
            "created_at": "2026-07-14T00:00:00Z",
        },
    )
    report = snapshot_job(job.id, store=store)["reports"]["monitor"]
    assert report["session_id"] == "ses_fresh"


def test_snapshot_includes_apply_report(tmp_path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    monkeypatch.setattr(sync_mod, "RunnerBridge", _FakeBridge())
    store.write_json(
        job.id,
        "reports/apply/session.json",
        {"session_id": "ses_apply", "created_at": "2026-07-13T15:06:34Z"},
    )
    _clobber(store, job.id, "apply")
    reports = snapshot_job(job.id, store=store)["reports"]
    assert "apply" in reports
    assert reports["apply"]["session_id"] == "ses_apply"


def test_report_with_session_missing_returns_none(tmp_path) -> None:
    store, job = _job(tmp_path)
    assert _report_with_session(store, job.id, "monitor") is None
