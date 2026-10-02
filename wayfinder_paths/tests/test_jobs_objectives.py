from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs.activities import (
    ActivityBinding,
    ObjectiveStrategy,
    activity_live_blockers,
    run_activity,
)
from wayfinder_paths.jobs.activity_reporting import objective_snapshot, record_activity
from wayfinder_paths.jobs.freestyle.runtime import run_freestyle_tick
from wayfinder_paths.jobs.freestyle.validate import validate_freestyle_job
from wayfinder_paths.jobs.objective_starters import (
    create_objective_strategy,
    objective_catalog,
)
from wayfinder_paths.jobs.store import JobStore


def create(tmp_path: Path, starter: str = "flop-participation") -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    created = create_objective_strategy(starter, store=store, compile_job=False)
    return store, created["job"]["id"]


def test_objective_catalog_cli_is_read_only() -> None:
    from click.testing import CliRunner

    from wayfinder_paths.jobs.cli import job_cli

    result = CliRunner().invoke(job_cli, ["objective-strategies"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)["result"]
    assert len(rows) == 4
    assert all(row["live_execution_available"] is False for row in rows)


@pytest.mark.parametrize("starter", [row["id"] for row in objective_catalog()])
def test_catalog_creates_normal_strategy_with_honest_dry_run(
    tmp_path: Path, starter: str
) -> None:
    store, jid = create(tmp_path, starter)
    job = store.load(jid)
    assert job.execution_contract == "freestyle_v1"
    assert job.source["kind"] == "objective_strategy"
    assert job.agent_loop.wake_interval_seconds == 604800
    assert "participation_changed" in job.agent_loop.triggers
    result = run_freestyle_tick(store.job_dir(jid), dry_run=True, ticks=3)
    assert result["ok"], result
    assert result["activities"]["main"]["external_verified"] is False
    assert result["fills"] == []
    assert not (
        store.job_dir(jid) / "state/activities/main/participation.json"
    ).exists()
    assert activity_live_blockers(job.execution_params) == []  # no execution enabled
    with pytest.raises(FileExistsError):
        create_objective_strategy(starter, store=store, compile_job=False)


def test_schema_and_live_readiness_cannot_be_enabled_by_configuration(
    tmp_path: Path,
) -> None:
    store, jid = create(tmp_path)
    params = store.load(jid).execution_params
    params["objective_strategy"]["activities"]["main"]["limits"]["enabled"] = True
    assert "no verified live execution" in activity_live_blockers(params)[0]
    spec = params["objective_strategy"]
    spec["activities"]["../escape"] = spec["activities"].pop("main")
    with pytest.raises(ValueError, match="activity names"):
        ObjectiveStrategy.model_validate(spec)


class ReadPort:
    supports_submit = True

    def __init__(self) -> None:
        self.protection: list[bool] = []
        self.submissions = 0

    async def close(self) -> None:
        pass

    async def protect(self, *, dry_run: bool) -> tuple[bool, str]:
        self.protection.append(dry_run)
        return True, "safe"

    async def observe(self) -> tuple[bool, dict[str, Any]]:
        return True, {
            "protocol": "custom",
            "program": "test",
            "rule_revision": "v1",
            "observed_at": time.time(),
            "readiness": "ready",
            "eligible": True,
        }

    async def submit(self, item: Any, *, operation_id: str) -> tuple[bool, str]:
        self.submissions += 1
        raise AssertionError("paper/disabled must not submit")

    async def reconcile(self, **kwargs: Any) -> tuple[bool, str]:
        return False, "not found"


@pytest.mark.asyncio
async def test_paper_and_halted_ticks_never_submit_and_keep_protection(
    tmp_path: Path,
) -> None:
    binding = ActivityBinding.model_validate(
        {
            "capability": "custom.work",
            "limits": {
                "protocol": "custom",
                "program": "test",
                "rule_revision": "v1",
                "enabled": True,
                "cost_unit": "USD",
                "max_total_cost": 10,
                "max_daily_cost": 10,
                "max_operation_cost": 2,
                "work": [
                    {"id": "one", "kind": "contribute", "max_cost": 1, "request": {}}
                ],
            },
        }
    )
    port = ReadPort()
    for mode, halted in [("paper", False), ("live", True)]:
        result = await run_activity(
            binding,
            state_dir=tmp_path,
            now=time.time(),
            mode=mode,
            dry_run=False,
            halted=halted,
            adapter=port,
        )
        assert result["status"] in {"dry_run", "blocked"}
    assert port.submissions == 0
    assert port.protection == [True, False]


def test_reporting_wakes_on_rewards_not_timestamps_or_ordinary_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, jid = create(tmp_path)
    wakes: list[Any] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.triggers.fire_triggers", lambda *a, **k: wakes.append(a)
    )
    snapshot = {
        "status": "idle",
        "observation": {"readiness": "ready", "observed_at": 1, "rewards": []},
    }
    record_activity(store, jid, "main", snapshot)
    record_activity(store, jid, "main", {**snapshot, "operations": 3})
    assert not wakes
    snapshot["observation"]["rewards"] = [
        {"unit": "POINTS", "amount": 10, "status": "confirmed", "observed_at": 2}
    ]
    record_activity(store, jid, "main", snapshot)
    assert len(wakes) == 1
    snapshot["observation"]["rewards"][0]["observed_at"] = 3
    record_activity(store, jid, "main", snapshot)
    assert len(wakes) == 1
    view = objective_snapshot(store, jid, store.load(jid).execution_params)
    assert (
        view and view["activities"]["main"]["observation"]["rewards"][0]["amount"] == 10
    )


def test_objective_validation_and_prompt_do_not_inherit_trading_research(
    tmp_path: Path,
) -> None:
    store, jid = create(tmp_path)
    report = validate_freestyle_job(jid, store=store)
    assert report["status"] == "passed", report
    assert (
        report["freestyle"]["dry_run"]["activities"]["main"]["status"] == "unverified"
    )
    from wayfinder_paths.jobs.worker import _build_worker_prompt_sections

    result = _build_worker_prompt_sections(
        store=store,
        job_id=jid,
        mode="monitor",
        snapshot={"job": store.load(jid).to_dict()},
    )
    assert "PROGRESS CONSTITUTION" not in result["prompt"]
    assert "risk-adjusted" not in result["prompt"]
    assert "ctx.participate" in result["prompt"]
    assert "unknown reward units have no USD value" in result["prompt"]


def test_no_credentials_in_objective_snapshot(tmp_path: Path) -> None:
    store, jid = create(tmp_path, "risex-participation")
    snapshot = objective_snapshot(store, jid, store.load(jid).execution_params)
    assert "JWT" not in json.dumps(snapshot)
    assert "account" not in snapshot["limits"]["main"]


@pytest.mark.parametrize("already_paused", [False, True])
def test_activity_risk_halt_cannot_be_cleared_by_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_paused: bool
) -> None:
    from wayfinder_paths.jobs.halt import clear_halt, read_halt, request_halt

    store, jid = create(tmp_path)
    if already_paused:
        request_halt(store, jid, source="freestyle_script")
    monkeypatch.setattr(
        "wayfinder_paths.jobs.freestyle.runtime.JobStore", lambda: store
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.triggers.fire_triggers", lambda *a, **k: None
    )

    async def breached(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "blocked",
            "risk_alert": True,
            "reason": "constraint breached",
        }

    monkeypatch.setattr("wayfinder_paths.jobs.activities.run_activity", breached)
    result = run_freestyle_tick(store.job_dir(jid))
    assert result["ok"], result
    assert read_halt(store.job_dir(jid))["source"] == "activity_risk"
    with pytest.raises(PermissionError, match="requires"):
        clear_halt(store, jid, by="agent")


def test_activity_reporting_sync_and_risk_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wayfinder_paths.jobs.decision_log import build_decision_log
    from wayfinder_paths.jobs.sync import snapshot_job

    store, jid = create(tmp_path)
    calls: list[Any] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.triggers.fire_triggers",
        lambda *a, **k: calls.append(a[2]),
    )
    payload = {
        "status": "idle",
        "observation": {"readiness": "ready", "eligible": True, "rewards": []},
    }
    record_activity(store, jid, "main", payload)
    record_activity(store, jid, "main", payload)
    assert len(build_decision_log(store, jid)["entries"]) == 1
    payload["cost_paid"] = 1
    record_activity(store, jid, "main", payload)
    assert len(build_decision_log(store, jid)["entries"]) == 2
    assert not calls
    payload["observation"]["eligible"] = False
    record_activity(store, jid, "main", payload)
    payload.update(risk_alert=True, reason="protection failed")
    record_activity(store, jid, "main", payload)
    assert calls == [["participation_changed"], ["risk_halt"]]
    snapshot = snapshot_job(jid, store=store)
    assert snapshot["scorecard"]["objective"]["activities"]["main"]["risk_alert"]


def test_non_trading_templates_refuse_openers_and_preserve_dry_run_activity(
    tmp_path: Path,
) -> None:
    from wayfinder_paths.jobs.risk_flags import risk_flags

    store, jid = create(tmp_path)
    job = store.load(jid)
    entry = store.job_dir(jid) / job.script_loop.entrypoint
    entry.write_text("""
def tick(ctx):
    if not ctx.state.get("observed"):
        ctx.participate("main")
        ctx.state["observed"] = True
        ctx.act({"venue": "hyperliquid", "kind": "market", "symbol": "ETH", "side": "long", "notional": 10})
""")
    result = run_freestyle_tick(store.job_dir(jid), dry_run=True, ticks=3)
    assert result["ok"], result
    assert result["activities"]["main"]["status"] == "unverified"
    assert result["actions"][0]["status"] == "refused"
    assert "trading is disabled" in result["actions"][0]["reason"]
    assert not result["fills"]
    assert {flag["code"] for flag in risk_flags(job, store.job_dir(jid))} == {
        "activity_execution_unverified"
    }
