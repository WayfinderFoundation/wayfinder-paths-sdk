"""Heartbeat and issues: runtime truth and owner-facing flags composed from
artifacts the box already keeps — raise-free, sorted, and never reading
ticks.jsonl."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from wayfinder_paths.jobs import health
from wayfinder_paths.jobs import sync as sync_mod
from wayfinder_paths.jobs.freestyle.validate import validate_freestyle_job
from wayfinder_paths.jobs.halt import request_halt
from wayfinder_paths.jobs.launch import launch_job, repin_launch
from wayfinder_paths.jobs.risk_flags import acknowledge_risk_flags
from wayfinder_paths.jobs.sync import snapshot_job
from wayfinder_paths.tests.test_jobs_launch import _freestyle, _patch
from wayfinder_paths.tests.test_jobs_runtime_status import (
    _FakeBridge,
    _job,
    _script_state,
)


def test_runner_down_is_reported_not_raised(tmp_path: Path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    monkeypatch.setattr(sync_mod, "RunnerBridge", _FakeBridge({}))
    snapshot = snapshot_job(job.id, store=store)
    heartbeat = snapshot["heartbeat"]
    assert heartbeat["runner_reachable"] is False
    assert heartbeat["loops"]["script"]["enabled"] is True
    assert heartbeat["loops"]["script"]["runner_status"] is None
    assert heartbeat["halt"]["active"] is False
    assert "sdk_version" in heartbeat
    codes = {issue["code"] for issue in snapshot["issues"]}
    assert "runner_unreachable" in codes
    assert all(issue["code"] in health.ISSUE_CODES for issue in snapshot["issues"])


def test_failing_loop_and_overdue_tick_become_sorted_issues(
    tmp_path: Path, monkeypatch
) -> None:
    store, job = _job(tmp_path)
    past = int((datetime.now(UTC) - timedelta(hours=3)).timestamp())
    state = {
        **_script_state(WAYFINDER_JOB_MODE="paper"),
        "status": "ERROR",
        "next_run_at": past,
        "consecutive_failures": 4,
        "last_error": "boom",
    }
    monkeypatch.setattr(
        sync_mod, "RunnerBridge", _FakeBridge({job.script_loop.runner_job_name: state})
    )
    snapshot = snapshot_job(job.id, store=store)
    issues = {issue["code"]: issue for issue in snapshot["issues"]}
    assert issues["script_loop_error"]["severity"] == "block"
    assert "boom" in issues["script_loop_error"]["message"]
    assert issues["tick_overdue"]["severity"] == "warn"
    assert snapshot["issues"][0]["severity"] == "block"
    loop = snapshot["heartbeat"]["loops"]["script"]
    assert datetime.fromisoformat(loop["next_run_at"]) < datetime.now(UTC)
    assert loop["consecutive_failures"] == 4


def test_mode_mismatch_is_a_blocking_issue(tmp_path: Path, monkeypatch) -> None:
    store, job = _job(tmp_path)  # declared paper, runner env says live
    monkeypatch.setattr(
        sync_mod,
        "RunnerBridge",
        _FakeBridge({job.script_loop.runner_job_name: _script_state()}),
    )
    snapshot = snapshot_job(job.id, store=store)
    issue = next(i for i in snapshot["issues"] if i["code"] == "mode_mismatch")
    assert issue["severity"] == "block" and "live" in issue["message"]


def test_freestyle_launch_halt_and_acknowledged_flags(
    tmp_path: Path, monkeypatch
) -> None:
    _patch(monkeypatch)
    store, job = _freestyle(tmp_path)
    before = snapshot_job(job.id, store=store)
    assert before["heartbeat"]["launch"]["launched"] is False
    assert "not_launched" in {i["code"] for i in before["issues"]}
    assert before["risk_flags"] and all(
        "acknowledged" in f for f in before["risk_flags"]
    )
    assert not any(f["acknowledged"] for f in before["risk_flags"])

    validate_freestyle_job(job.id, store=store)
    assert launch_job(job.id, store=store, script_mode="paper")["launched"]
    launched = snapshot_job(job.id, store=store)
    warn_codes = [f["code"] for f in launched["risk_flags"] if f["severity"] == "warn"]
    assert warn_codes, "the fixture script should carry at least one warn flag"
    acknowledge_risk_flags(store, job.id, warn_codes[:1], by="owner", memo="known")
    request_halt(store, job.id, reason="daily loss cap", source="risk_limits")

    snapshot = snapshot_job(job.id, store=store)
    heartbeat = snapshot["heartbeat"]
    assert heartbeat["launch"]["launched"] is True
    assert heartbeat["launch"]["identity_ok"] is True
    assert heartbeat["halt"]["active"] is True
    assert heartbeat["halt"]["source"] == "risk_limits"
    halted = next(i for i in snapshot["issues"] if i["code"] == "halted")
    assert halted["severity"] == "block"
    assert halted["ref"] == "owner_attention:halt_awaiting_owner_clear"
    acknowledged = {f["code"]: f["acknowledged"] for f in snapshot["risk_flags"]}
    assert acknowledged[warn_codes[0]] is True
    codes = {i["code"] for i in snapshot["issues"]}
    assert "not_launched" not in codes


def test_revision_drift_after_the_pin_blocks_until_repinned(
    tmp_path: Path, monkeypatch
) -> None:
    _patch(monkeypatch)
    store, job = _freestyle(tmp_path)
    validate_freestyle_job(job.id, store=store)
    assert launch_job(job.id, store=store, script_mode="paper")["launched"]
    store.append_journal(
        job.id,
        {
            "type": "revision_drift",
            "error": "revision drift: launched at aaaa, workspace is bbbb",
        },
    )
    snapshot = snapshot_job(job.id, store=store)
    drift = next(i for i in snapshot["issues"] if i["code"] == "revision_drift")
    assert drift["severity"] == "block" and "validate and launch" in drift["fix"]

    repin_launch(store, job.id, revision="bbbb", by="apply:prop-1")
    after = snapshot_job(job.id, store=store)
    assert "revision_drift" not in {i["code"] for i in after["issues"]}
    assert after["heartbeat"]["launch"]["relaunched_at"]


def test_build_issues_flags_missing_and_stale_features(tmp_path: Path) -> None:
    store, job = _job(tmp_path)
    issues = health.build_issues(
        store,
        job.id,
        job,
        heartbeat=None,
        scorecard={"dataset_fetch": {"status": "failed"}},
        features=[
            {"name": "missing", "available": False},
            {
                "name": "stale",
                "available": True,
                "cadence": "1h",
                "age_seconds": 3 * 3600,
                "latest_timestamp": "2026-09-15T00:00:00+00:00",
            },
            {"name": "fresh", "available": True, "cadence": "1h", "age_seconds": 600},
        ],
        risk_flags=None,
        launch_checklist=None,
        proposals=[
            {
                "proposal_id": "prop-1",
                "application": {
                    "status": "applying",
                    "started_at": (
                        datetime.now(UTC) - timedelta(minutes=20)
                    ).isoformat(),
                },
            }
        ],
    )
    codes = [i["code"] for i in issues]
    assert codes.count("feed_stale") == 2
    assert "fresh" not in " ".join(i["message"] for i in issues)
    assert "dataset_fetch_failed" in codes
    stalled = next(i for i in issues if i["code"] == "apply_stalled")
    assert "prop-1" in stalled["message"] and stalled["severity"] == "warn"


def test_snapshot_survives_a_broken_detector(tmp_path: Path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    monkeypatch.setattr(sync_mod, "RunnerBridge", _FakeBridge({}))

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(health, "build_issues", boom)
    snapshot = snapshot_job(job.id, store=store)
    assert snapshot["issues"] == [] and snapshot["heartbeat"] is not None
    monkeypatch.setattr(health, "build_heartbeat", boom)
    snapshot = snapshot_job(job.id, store=store)
    assert snapshot["heartbeat"] is None and snapshot["issues"] == []
