"""Fit-the-box: MCP-first tool-call doctrine, steal-aware load shedding,
runner-gap alerting, and disk-pressure alerting.

Motivating forensics (production Fly box, ~0.25 sustained vCPU under 81-93%
CPU steal): agents shelled out to `wayfinder <tool>` CLI wrappers for 1s API
reads — each cold-imports the whole SDK (~90s CPU, 150-250MB) and 24 of 29
OOM-killed processes were these; agents then misread their own `timeout`
kills (exit 124) as "backend DOWN". The watchdog's gate re-stamp ran a full
backtest under that steal, holding the heavy-compute lock 45-60 min. And
during the OOM cascade the LIVE job's script loop went dark 65 minutes with
zero alerting. Separately, the box's 2GB /wf volume silently filled to 100%
— boot rsync died half-way, opencode crash-looped, runnerd stayed down, and
live loops went dark ~25 minutes with no alert at 90/95/100%. Under test:
the doctrine lands in the worker prompt, the watchdog sheds deferrable load
under steal, and a dark loop or a filling volume is journaled and (for live
jobs) wakes the agent.
"""

from __future__ import annotations

import json
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs.failures import (
    classify_failure,
    cpu_steal_pct,
    disk_used_pct,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.triggers import ALWAYS_WAKE_EVENTS
from wayfinder_paths.jobs.watchdog import (
    _check_disk_usage,
    _check_loop_gap,
    _recover_stale_gate,
    recover_stalled_applications,
)
from wayfinder_paths.jobs.worker import _compute_status_block


def _make_store(
    tmp_path: Path, job_id: str = "box-demo", *, interval_seconds: int | None = 300
) -> tuple[JobStore, WayfinderJob]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        job_id,
        script="workspace/src/strategy.py",
        interval_seconds=interval_seconds,
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(job)
    return store, job


def _journal_events(store: JobStore, job_id: str, event_type: str) -> list[dict]:
    path = store.job_dir(job_id) / "journal.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return [row for row in rows if row.get("type") == event_type]


def _write_forward_runs(store: JobStore, job_id: str, ts: datetime) -> None:
    path = store.job_dir(job_id) / "results" / "forward" / "runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ts": ts.isoformat(), "status": "ok"}) + "\n")


# ── failure taxonomy ─────────────────────────────────────────────────────────


def test_exit_124_is_infrastructure() -> None:
    for text in (
        "wayfinder research: exit 124",
        "subprocess returned exit status 124",
        "command timed out after 300 seconds",
    ):
        assert classify_failure(text) == "infrastructure", text
    assert classify_failure("thesis refuted by walk-forward") == "evidence"


def test_cpu_steal_pct_never_raises() -> None:
    steal = cpu_steal_pct(sample_seconds=0.01)
    # Linux: a float percentage; macOS/no-procfs: None. Never an exception.
    assert steal is None or 0.0 <= steal <= 100.0


# ── compute_status: box truth carries steal + load ───────────────────────────


def test_compute_status_block_has_steal_and_loadavg(tmp_path: Path) -> None:
    store, job = _make_store(tmp_path, "box-status")
    block = _compute_status_block(store.job_dir(job.id))
    assert "cpu_steal_pct" in block  # None off-Linux, float share on Linux
    assert "loadavg" in block
    basis = block["_basis"]
    assert "cpu_steal_pct above ~60" in basis
    assert "NEVER interpret local timeouts" in basis


def test_worker_prompt_carries_mcp_first_doctrine(tmp_path: Path) -> None:
    from wayfinder_paths.jobs.worker import prepare_job_worker_prompt

    store, job = _make_store(tmp_path, "box-prompt")
    sections = prepare_job_worker_prompt(store=store, job_id=job.id, mode="intervene")
    stable = sections["stable_prefix"]
    assert "MCP-FIRST" in stable
    assert "cold-import" in stable
    assert "NEVER `wayfinder <tool-name>` CLI subprocesses" in stable
    assert "LOCAL CPU STARVATION" in stable
    # Dynamic context carries the mechanical steal/load numbers.
    assert "cpu_steal_pct" in sections["prompt"]
    assert "loadavg" in sections["prompt"]


# ── watchdog: steal-aware gate-restamp deferral ──────────────────────────────


@pytest.fixture
def stale_gate(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Force a red gate that is PURELY revision mismatch, and record any
    restamp (backtest chain) attempts."""
    attempts: list[str] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.gating.evaluate_live_gate",
        lambda job_id, store=None: {
            "live_ready": False,
            "reasons": ["backtest is for revision aabbccdd11, workspace is ffeedd22"],
        },
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.gating.compute_workspace_revision",
        lambda root: "ffeedd22",
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.job.backtest_execution_job",
        lambda job_id, store=None: attempts.append("backtest"),
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.preflight.run_preflight",
        lambda job_id, store=None: attempts.append("preflight"),
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.validation.validate_execution_job",
        lambda job_id, store=None: attempts.append("validate"),
    )
    return attempts


def test_restamp_deferred_under_cpu_steal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_gate: list[str]
) -> None:
    store, job = _make_store(tmp_path, "box-steal-hi")
    monkeypatch.setattr("wayfinder_paths.jobs.watchdog.cpu_steal_pct", lambda: 90.0)
    event = _recover_stale_gate(store, job.id, [], allow_restamp=True)
    assert event is not None
    assert event["action"] == "restamp_deferred_load"
    assert event["cpu_steal_pct"] == 90.0
    assert stale_gate == []  # no backtest launched under load
    journaled = _journal_events(store, job.id, "restamp_deferred_load")
    assert len(journaled) == 1
    assert journaled[0]["cpu_steal_pct"] == 90.0


def test_restamp_proceeds_when_steal_low(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_gate: list[str]
) -> None:
    store, job = _make_store(tmp_path, "box-steal-lo")
    monkeypatch.setattr("wayfinder_paths.jobs.watchdog.cpu_steal_pct", lambda: 10.0)
    event = _recover_stale_gate(store, job.id, [], allow_restamp=True)
    assert event is not None
    assert event["action"] == "gate_restamp"
    assert stale_gate == ["backtest", "preflight", "validate"]
    assert not _journal_events(store, job.id, "restamp_deferred_load")


# ── watchdog: runner-gap alerting ────────────────────────────────────────────


@pytest.fixture
def trigger_wakes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_fire(store: Any, job: Any, events: list[str], *, source: str) -> None:
        calls.append({"job_id": job.id, "events": events, "source": source})

    monkeypatch.setattr("wayfinder_paths.jobs.triggers.fire_triggers", fake_fire)
    return calls


def test_loop_gap_journaled_once_per_outage(
    tmp_path: Path, trigger_wakes: list[dict[str, Any]]
) -> None:
    store, job = _make_store(tmp_path, "box-gap")
    now = datetime.now(UTC)
    _write_forward_runs(store, job.id, now - timedelta(seconds=3600))

    event = _check_loop_gap(store, job, now)
    assert event is not None
    assert event["action"] == "runner_loop_gap"
    assert event["gap_seconds"] >= 3 * 300
    assert event["interval"] == 300
    assert _check_loop_gap(store, job, now) is None  # deduped second pass
    journaled = _journal_events(store, job.id, "runner_loop_gap")
    assert len(journaled) == 1
    assert journaled[0]["mode"] == "paper"
    assert not trigger_wakes  # paper mode alerts, never wakes

    # Loop resumes: recovery journaled once and the marker clears.
    _write_forward_runs(store, job.id, now)
    event = _check_loop_gap(store, job, now)
    assert event is not None and event["action"] == "runner_loop_recovered"
    assert _check_loop_gap(store, job, now) is None
    assert len(_journal_events(store, job.id, "runner_loop_recovered")) == 1


def test_loop_gap_live_mode_fires_trigger_wake(
    tmp_path: Path, trigger_wakes: list[dict[str, Any]]
) -> None:
    store, job = _make_store(tmp_path, "box-gap-live")
    job.script_loop.mode = "live"
    store.save(job)
    now = datetime.now(UTC)
    _write_forward_runs(store, job.id, now - timedelta(hours=2))

    event = _check_loop_gap(store, job, now)
    assert event is not None and event["mode"] == "live"
    assert trigger_wakes == [
        {"job_id": job.id, "events": ["runner_loop_gap"], "source": "watchdog"}
    ]
    assert "runner_loop_gap" in ALWAYS_WAKE_EVENTS


def test_loop_gap_quiet_paths(tmp_path: Path) -> None:
    # Fresh telemetry → no event; no telemetry at all → startup, no event.
    store, job = _make_store(tmp_path, "box-gap-quiet")
    now = datetime.now(UTC)
    assert _check_loop_gap(store, job, now) is None
    _write_forward_runs(store, job.id, now - timedelta(seconds=60))
    assert _check_loop_gap(store, job, now) is None
    # Disabled script loop is never scanned.
    agent_only = WayfinderJob.new("box-agent-only", agent_mode="intervene")
    store.save(agent_only)
    assert _check_loop_gap(store, agent_only, now) is None


def test_loop_gap_wired_into_watchdog_pass(
    tmp_path: Path, trigger_wakes: list[dict[str, Any]]
) -> None:
    store, job = _make_store(tmp_path, "box-gap-pass")
    now = datetime.now(UTC)
    _write_forward_runs(store, job.id, now - timedelta(hours=1))
    result = recover_stalled_applications(store=store, now=now)
    gap_events = [
        e for e in result["recovered"] if e.get("action") == "runner_loop_gap"
    ]
    assert len(gap_events) == 1
    assert gap_events[0]["job_id"] == job.id
    assert not [e for e in result["errors"] if "loop_gap" in str(e.get("error"))]


# ── watchdog: disk-pressure alerting ─────────────────────────────────────────

_DiskUsage = namedtuple("_DiskUsage", "total used free")


def _patch_disk(
    monkeypatch: pytest.MonkeyPatch, *, total_mb: int, used_mb: int
) -> None:
    total = total_mb * 1024 * 1024
    used = used_mb * 1024 * 1024
    monkeypatch.setattr(
        "shutil.disk_usage", lambda path: _DiskUsage(total, used, total - used)
    )


def test_disk_pressure_journaled_once_with_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trigger_wakes: list[dict[str, Any]],
) -> None:
    store, job = _make_store(tmp_path, "box-disk")
    now = datetime.now(UTC)
    _patch_disk(monkeypatch, total_mb=2000, used_mb=1900)  # 95% used

    event = _check_disk_usage(store, job, now)
    assert event is not None
    assert event["action"] == "disk_pressure"
    journaled = _journal_events(store, job.id, "disk_pressure")
    assert len(journaled) == 1
    row = journaled[0]
    assert row["pct_used"] == 95.0
    assert row["free_mb"] == 100
    assert row["total_mb"] == 2000
    assert row["threshold_pct"] == 85
    assert row["mode"] == "paper"
    assert not trigger_wakes  # paper mode alerts, never wakes

    # Same pressure next pass → debounced, no second journal entry.
    assert _check_disk_usage(store, job, now + timedelta(minutes=5)) is None
    assert len(_journal_events(store, job.id, "disk_pressure")) == 1


def test_disk_pressure_realert_on_rise_then_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job = _make_store(tmp_path, "box-disk-rise")
    now = datetime.now(UTC)
    _patch_disk(monkeypatch, total_mb=2000, used_mb=1720)  # 86%
    assert _check_disk_usage(store, job, now) is not None
    # +2 points inside the window → same pressure episode, suppressed.
    _patch_disk(monkeypatch, total_mb=2000, used_mb=1760)  # 88%
    assert _check_disk_usage(store, job, now + timedelta(hours=1)) is None
    # A ≥5-point climb re-alerts even inside the window.
    _patch_disk(monkeypatch, total_mb=2000, used_mb=1840)  # 92%
    event = _check_disk_usage(store, job, now + timedelta(hours=2))
    assert event is not None and event["pct_used"] == 92.0
    assert len(_journal_events(store, job.id, "disk_pressure")) == 2
    # Past the re-alert window the episode re-alerts without a climb.
    event = _check_disk_usage(store, job, now + timedelta(hours=9))
    assert event is not None
    assert len(_journal_events(store, job.id, "disk_pressure")) == 3
    # Space freed → recovery journaled once and the marker clears.
    _patch_disk(monkeypatch, total_mb=2000, used_mb=1000)  # 50%
    event = _check_disk_usage(store, job, now + timedelta(hours=10))
    assert event is not None and event["action"] == "disk_pressure_recovered"
    assert _check_disk_usage(store, job, now + timedelta(hours=10)) is None
    assert len(_journal_events(store, job.id, "disk_pressure_recovered")) == 1


def test_disk_pressure_live_mode_fires_trigger_wake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trigger_wakes: list[dict[str, Any]],
) -> None:
    store, job = _make_store(tmp_path, "box-disk-live")
    job.script_loop.mode = "live"
    store.save(job)
    _patch_disk(monkeypatch, total_mb=2000, used_mb=1900)

    event = _check_disk_usage(store, job, datetime.now(UTC))
    assert event is not None and event["mode"] == "live"
    assert trigger_wakes == [
        {"job_id": job.id, "events": ["disk_pressure"], "source": "watchdog"}
    ]
    assert "disk_pressure" in ALWAYS_WAKE_EVENTS


def test_disk_pressure_below_threshold_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job = _make_store(tmp_path, "box-disk-quiet")
    _patch_disk(monkeypatch, total_mb=2000, used_mb=1000)  # 50%
    assert _check_disk_usage(store, job, datetime.now(UTC)) is None
    assert not _journal_events(store, job.id, "disk_pressure")
    assert not (store.job_dir(job.id) / "state" / "disk_pressure_alert.json").exists()


def test_disk_pressure_env_threshold_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job = _make_store(tmp_path, "box-disk-env")
    now = datetime.now(UTC)
    _patch_disk(monkeypatch, total_mb=2000, used_mb=1200)  # 60%
    assert _check_disk_usage(store, job, now) is None  # default 85 not breached
    monkeypatch.setenv("WAYFINDER_DISK_ALERT_PCT", "50")
    event = _check_disk_usage(store, job, now)
    assert event is not None and event["threshold_pct"] == 50
    assert _journal_events(store, job.id, "disk_pressure")[0]["threshold_pct"] == 50


def test_disk_pressure_wired_into_watchdog_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trigger_wakes: list[dict[str, Any]],
) -> None:
    store, job = _make_store(tmp_path, "box-disk-pass")
    _patch_disk(monkeypatch, total_mb=2000, used_mb=1900)
    result = recover_stalled_applications(store=store, now=datetime.now(UTC))
    disk_events = [e for e in result["recovered"] if e.get("action") == "disk_pressure"]
    assert len(disk_events) == 1
    assert disk_events[0]["job_id"] == job.id
    assert not [e for e in result["errors"] if "disk" in str(e.get("error"))]


def test_disk_used_pct_helper_and_compute_status_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job = _make_store(tmp_path, "box-disk-status")
    _patch_disk(monkeypatch, total_mb=2000, used_mb=1500)  # 75%
    assert disk_used_pct(tmp_path) == 75.0
    block = _compute_status_block(store.job_dir(job.id))
    assert block["disk_used_pct"] == 75.0
    assert "disk_used_pct above" in block["_basis"]

    def _boom(path: Any) -> _DiskUsage:
        raise OSError("statvfs failed")

    monkeypatch.setattr("shutil.disk_usage", _boom)
    assert disk_used_pct(tmp_path) is None  # never raises
