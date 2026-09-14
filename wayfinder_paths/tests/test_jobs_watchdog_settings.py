"""The long watchdog: watch level, cadence, triggers, notification policy,
kill switches, and the notification hook on job events."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wayfinder_paths.jobs import compiler as compiler_mod
from wayfinder_paths.jobs import notify_policy
from wayfinder_paths.jobs import sync as sync_mod
from wayfinder_paths.jobs.freestyle.validate import validate_freestyle_job
from wayfinder_paths.jobs.launch import launch_job, set_watchdog, watchdog_view
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.notify_policy import (
    in_quiet_hours,
    notify_decision,
    notify_events,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.triggers import fire_triggers

SCRIPT = (
    "from wayfinder_paths.jobs.freestyle import FreestyleSpec\n"
    "SPEC = FreestyleSpec(max_notional_per_tick=100, max_loss_usd=5)\n"
    "def tick(ctx):\n"
    "    if 'ETH' not in ctx.positions:\n"
    "        ctx.act({'venue': 'hyperliquid', 'kind': 'market', 'symbol': 'ETH', 'side': 'long', 'notional': 50, 'max_loss': 5})\n"
)


class _Bridge:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.actions: list[tuple[str, str]] = []

    def __call__(self, *, repo_root=None):
        return self

    def ensure_started(self):
        return {"ok": True}

    def add_or_update_script_job(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True, "result": {"name": kwargs["name"]}}

    def delete(self, name):
        return {"ok": True}

    def pause(self, name):
        self.actions.append(("pause", name))
        return {"ok": True}

    def resume(self, name):
        self.actions.append(("resume", name))
        return {"ok": True}

    def job_states(self) -> dict:
        return {}


def _patch(monkeypatch) -> _Bridge:
    bridge = _Bridge()
    monkeypatch.setattr(compiler_mod, "RunnerBridge", bridge)
    monkeypatch.setattr(sync_mod, "RunnerBridge", bridge)
    monkeypatch.setattr("wayfinder_paths.jobs.application.RunnerBridge", bridge)
    monkeypatch.setattr(sync_mod.WAYFINDER_JOBS_CLIENT, "sync", lambda snapshots: None)
    return bridge


def _freestyle(tmp_path: Path) -> tuple[JobStore, WayfinderJob]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "eth-dip",
        script="workspace/src/eth_dip.py",
        interval_seconds=300,
        timeout_seconds=60,
        execution_contract="freestyle_v1",
        source={"kind": "freestyle"},
        agent_mode="monitor",
    )
    root = store.init_layout(job)
    (root / "workspace" / "src" / "eth_dip.py").write_text(SCRIPT, encoding="utf-8")
    store.save(job)
    return store, job


def test_set_watchdog_changes_level_cadence_triggers_and_alerts(
    tmp_path: Path, monkeypatch
) -> None:
    bridge = _patch(monkeypatch)
    store, job = _freestyle(tmp_path)
    result = set_watchdog(
        job.id,
        store=store,
        watch_level="intervene",
        wake_interval_seconds=1800,
        triggers=["script_failure", "risk_halt"],
        notifications={
            "channels": ["email"],
            "on": ["risk_halt"],
            "quiet_hours": {"start": "22:00", "end": "07:00", "tz": "UTC"},
        },
    )
    view = result["watchdog"]
    assert view["watch_level"] == "intervene" and view["wake_interval_seconds"] == 1800
    assert view["triggers"] == ["risk_halt", "script_failure"]
    assert view["notifications"]["on"] == ["risk_halt"] and view["notifications"][
        "channels"
    ] == ["email"]
    assert result["evolution_eligibility"]["eligible"] is False
    reloaded = store.load(job.id)
    assert (
        reloaded.agent_loop.mode == "intervene" and reloaded.job_kind == "script_agent"
    )
    agent_calls = [c for c in bridge.calls if c["name"].endswith("-agent")]
    assert (
        agent_calls
        and agent_calls[-1]["env"]["WAYFINDER_JOB_AGENT_MODE"] == "intervene"
    )
    with pytest.raises(ValueError):
        set_watchdog(job.id, store=store, triggers=["not_an_event"])
    with pytest.raises(ValueError):
        set_watchdog(job.id, store=store, notifications={"channels": ["pigeon"]})


def test_kill_switches_write_limits_and_relaunch_a_launched_paper_job(
    tmp_path: Path, monkeypatch
) -> None:
    _patch(monkeypatch)
    store, job = _freestyle(tmp_path)
    validate_freestyle_job(job.id, store=store)
    launched = launch_job(job.id, store=store)
    assert launched["launched"]
    first_revision = launched["revision"]
    result = set_watchdog(
        job.id,
        store=store,
        kill_switches={"max_daily_loss_usd": 25, "max_drawdown": 0.1},
    )
    limits = json.loads(
        (store.job_dir(job.id) / "workspace" / "risk_limits.json").read_text()
    )
    assert limits == {"max_daily_loss_usd": 25.0, "max_drawdown": -0.1}
    assert result["relaunch"] and result["relaunch"]["launched"]
    assert result["relaunch"]["revision"] != first_revision
    assert (
        store.load(job.id).versioning["active_revision"]
        == result["relaunch"]["revision"]
    )
    assert (
        watchdog_view(store.load(job.id), store.job_dir(job.id))["kill_switches"][
            "max_drawdown"
        ]
        == -0.1
    )


def test_quiet_hours_and_event_filter_decide_delivery(tmp_path: Path) -> None:
    store, job = _freestyle(tmp_path)
    job.reporting["notify"] = {
        "channels": ["email"],
        "on": ["risk_halt"],
        "quiet_hours": {"start": "22:00", "end": "07:00", "tz": "UTC"},
    }
    assert in_quiet_hours(
        job.reporting["notify"]["quiet_hours"],
        datetime(2026, 9, 14, 23, 30, tzinfo=UTC),
    )
    assert not in_quiet_hours(
        job.reporting["notify"]["quiet_hours"], datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    )
    assert (
        notify_decision(job, "risk_halt", now=datetime(2026, 9, 14, 12, 0, tzinfo=UTC))[
            "deliver"
        ]
        is True
    )
    assert (
        notify_decision(job, "risk_halt", now=datetime(2026, 9, 14, 23, 0, tzinfo=UTC))[
            "deliver"
        ]
        is False
    )
    assert (
        notify_decision(
            job, "script_failure", now=datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
        )["deliver"]
        is False
    )


def test_fire_triggers_delivers_through_the_policy_once_per_spacing(
    tmp_path: Path, monkeypatch
) -> None:
    store, job = _freestyle(tmp_path)
    job.reporting["notify"] = {
        "channels": ["email"],
        "on": ["risk_halt"],
        "quiet_hours": None,
    }
    store.save(job)
    delivered: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        notify_policy,
        "_deliver",
        lambda title, body, channels: delivered.append((title, channels))
        or {"delivery": dict.fromkeys(channels, "sent")},
    )
    fire_triggers(
        store, store.load(job.id), ["risk_halt", "script_failure"], source="test"
    )
    assert delivered == [(f"[{job.name}] risk halt", ["email"])]
    fire_triggers(store, store.load(job.id), ["risk_halt"], source="test")
    assert len(delivered) == 1  # spaced: same event within the hour is not re-sent
    ledger = json.loads(
        (store.job_dir(job.id) / "state" / "notify_ledger.json").read_text()
    )
    assert "risk_halt" in ledger["last_sent"]
    sent = notify_events(
        store,
        store.load(job.id),
        ["risk_halt"],
        source="later",
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert len(sent) == 1 and len(delivered) == 2
