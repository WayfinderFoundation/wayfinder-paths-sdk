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
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.risk_flags import acknowledge_risk_flags
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import apply_script_mode, snapshot_job
from wayfinder_paths.tests.test_jobs_launch import _freestyle, _patch
from wayfinder_paths.tests.test_jobs_risk_limits import _write_governance
from wayfinder_paths.tests.test_jobs_runtime_status import (
    _FakeBridge,
    _job,
    _script_state,
)
from wayfinder_paths.tests.test_jobs_script_mode import _patch_bridges

# majors-5m-lab's owner ceilings: only max_drawdown and max_leverage are
# enforced at runtime; the other two feed evolution gates.
MAJORS_HARD_CONSTRAINTS = {
    "max_drawdown": 0.15,
    "max_drawdown_pct": 0.15,
    "max_tail_loss": 0.1,
    "max_leverage": 3.0,
}


def _jobs_v1(tmp_path: Path) -> tuple[JobStore, WayfinderJob]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "majors",
        script="workspace/src/strategy.py",
        interval_seconds=300,
        timeout_seconds=120,
        execution_contract="jobs_v1",
        agent_mode="intervene",
    )
    job.execution_params["wallet_label"] = "majors"
    store.create_job(job)
    return store, store.load(job.id)


def _owner_switched_live(tmp_path: Path, monkeypatch) -> tuple[JobStore, WayfinderJob]:
    """The majors-5m-lab shape: the owner flipped script mode to live through
    apply_script_mode (no launch checklist, so no state/launch.json) and the
    runner ticks the script loop live."""
    store, job = _jobs_v1(tmp_path)
    _write_governance(tmp_path, job.id, MAJORS_HARD_CONSTRAINTS)
    _patch_bridges(monkeypatch)
    monkeypatch.setattr(sync_mod.WAYFINDER_JOBS_CLIENT, "sync", lambda snapshots: None)
    monkeypatch.setattr(
        sync_mod,
        "evaluate_live_gate",
        lambda *a, **k: {"live_ready": True, "reasons": []},
    )
    apply_script_mode(job.id, "live", store=store, set_by="owner")
    job = store.load(job.id)
    monkeypatch.setattr(
        sync_mod,
        "RunnerBridge",
        _FakeBridge({job.script_loop.runner_job_name: _script_state()}),
    )
    return store, job


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


def test_reverted_apply_is_a_warn_issue_for_a_week(tmp_path: Path) -> None:
    store, job = _job(tmp_path)
    fresh = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    stale = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    issues = health.build_issues(
        store,
        job.id,
        job,
        heartbeat=None,
        scorecard=None,
        features=None,
        risk_flags=None,
        launch_checklist=None,
        proposals=[
            {
                "proposal_id": "prop-fresh",
                "application": {
                    "status": "failed",
                    "reverted": {"ts": fresh, "reason": "backtest OOM blocked it"},
                },
            },
            {
                "proposal_id": "prop-stale",
                "application": {
                    "status": "canceled",
                    "reverted": {"ts": stale, "reason": "old news"},
                },
            },
            {"proposal_id": "prop-clean", "application": {"status": "applied"}},
        ],
    )
    reverted = [i for i in issues if i["code"] == "apply_reverted"]
    assert len(reverted) == 1
    assert "prop-fresh" in reverted[0]["message"]
    assert "OOM" in reverted[0]["message"]
    assert reverted[0]["severity"] == "warn"
    assert reverted[0]["ref"] == "owner_attention:apply_reverted"
    assert reverted[0]["since"] == fresh
    assert "apply_reverted" in health.ISSUE_CODES


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


def test_unqueued_wake_is_flagged_without_masking_the_last_check(
    tmp_path: Path,
) -> None:
    store, job = _job(tmp_path)
    checked = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    failed = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    heartbeat = {
        "loops": {"agent": {"enabled": True, "consecutive_failures": 0}},
        "runner_reachable": True,
    }
    issues = health.build_issues(
        store,
        job.id,
        job,
        heartbeat=heartbeat,
        scorecard={
            "last_agent_check_at": checked,
            "last_agent_wake_error": "OpenCode server unavailable",
            "last_agent_wake_error_at": failed,
        },
        features=None,
        risk_flags=None,
        launch_checklist=None,
        proposals=[],
    )
    failed_issue = next(i for i in issues if i["code"] == "agent_wake_failed")
    assert "OpenCode server unavailable" in failed_issue["message"]
    assert failed_issue["since"] == failed

    # A later real check clears it.
    issues = health.build_issues(
        store,
        job.id,
        job,
        heartbeat=heartbeat,
        scorecard={
            "last_agent_check_at": datetime.now(UTC).isoformat(),
            "last_agent_wake_error": "OpenCode server unavailable",
            "last_agent_wake_error_at": failed,
        },
        features=None,
        risk_flags=None,
        launch_checklist=None,
        proposals=[],
    )
    assert "agent_wake_failed" not in [i["code"] for i in issues]


def test_owner_switched_live_job_reads_as_launched_and_live(
    tmp_path: Path, monkeypatch
) -> None:
    store, job = _owner_switched_live(tmp_path, monkeypatch)
    assert not (store.job_dir(job.id) / "state" / "launch.json").exists()

    snapshot = snapshot_job(job.id, store=store)
    launch = snapshot["heartbeat"]["launch"]
    assert launch["launched"] is True
    assert launch["source"] == "running" and launch["mode"] == "live"
    issues = {issue["code"]: issue for issue in snapshot["issues"]}
    assert "not_launched" not in issues

    checklist = snapshot["launch_checklist"]
    assert checklist["target"] == "live"
    assert "mode_is_paper" not in {item["id"] for item in checklist["items"]}
    # The real live gate fails here (no validation report): that is a true
    # issue and must name the gate, never "leave live".
    failing = issues["launch_checklist_failing"]
    assert "leave live" not in failing["message"]
    assert "validation" in failing["message"]

    risk = issues["risk_flags_unacknowledged"]
    assert risk["severity"] == "warn"
    assert risk["message"].startswith(
        "running live without: a daily-loss cap, a gross-exposure cap"
    )
    assert "drawdown" not in risk["message"]
    assert "venue-side stop" not in risk["message"]
    assert risk["fix"].startswith('set_watchdog(kill_switches={"max_daily_loss_usd"')
    assert risk["fix"].endswith(". Or acknowledge the risk on the Launch tab.")

    watchdog = snapshot["watchdog"]
    assert watchdog["kill_switches"] == {"max_drawdown": -0.15}
    assert watchdog["kill_switch_sources"] == {"max_drawdown": "owner"}
    assert watchdog["leverage_ceiling"] == {"value": 3.0, "source": "owner"}


def test_never_started_jobs_v1_job_is_still_not_launched(
    tmp_path: Path, monkeypatch
) -> None:
    store, job = _jobs_v1(tmp_path)
    monkeypatch.setattr(sync_mod, "RunnerBridge", _FakeBridge({}))
    snapshot = snapshot_job(job.id, store=store)
    launch = snapshot["heartbeat"]["launch"]
    assert launch["launched"] is False and launch["source"] is None
    assert "not_launched" in {issue["code"] for issue in snapshot["issues"]}
    assert snapshot["launch_checklist"]["target"] == "paper"


def _flag(code: str, severity: str = "warn") -> dict:
    return {
        "code": code,
        "severity": severity,
        "message": code,
        "fix": f"fix {code}",
        "scope": "risk_limits",
        "acknowledged": False,
    }


def test_risk_issue_orders_caps_first_and_speaks_the_phase(tmp_path: Path) -> None:
    store, job = _job(tmp_path)
    catalogue_order = [
        _flag("no_timeout"),
        _flag("no_stop_loss"),
        _flag("no_native_stop"),
        _flag("no_max_drawdown"),
        _flag("no_max_daily_loss"),
        _flag("no_position_cap", "info"),
        _flag("unbounded_notional"),
        {**_flag("no_consecutive_loss_pause"), "acknowledged": True},
    ]

    def risk_issue(mode: str) -> dict:
        issues = health.build_issues(
            store,
            job.id,
            job,
            heartbeat=None,
            scorecard={"mode": mode},
            features=None,
            risk_flags=catalogue_order,
            launch_checklist=None,
            proposals=None,
        )
        return next(i for i in issues if i["code"] == "risk_flags_unacknowledged")

    paper = risk_issue("paper")
    assert paper["severity"] == "warn"
    assert paper["message"] == (
        "unacknowledged risk flags: no_max_drawdown, no_max_daily_loss, "
        "unbounded_notional, no_stop_loss, no_native_stop, no_timeout"
    )
    assert paper["fix"] == "fix no_max_drawdown"

    live = risk_issue("live")
    assert live["severity"] == "warn"
    assert live["message"] == (
        "running live without: a drawdown cap, a daily-loss cap, "
        "a gross-exposure cap, a stop-loss, a venue-side stop, a tick timeout"
    )
    assert live["fix"] == (
        "fix no_max_drawdown. Or acknowledge the risk on the Launch tab."
    )

    catalogue_order.append(_flag("leverage_above_governance", "block"))
    blocked = risk_issue("live")
    assert blocked["severity"] == "block"
    assert blocked["message"].startswith(
        "running live without: leverage within the owner ceiling, a drawdown cap"
    )
