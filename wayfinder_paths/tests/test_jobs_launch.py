"""The launch checklist and launch: identity pins, risk flags, acknowledgment,
and the contract dispatch that keeps jobs_v1 byte-identical."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wayfinder_paths.jobs import compiler as compiler_mod
from wayfinder_paths.jobs import sync as sync_mod
from wayfinder_paths.jobs.contracts import evaluate_live_readiness
from wayfinder_paths.jobs.freestyle.validate import validate_freestyle_job
from wayfinder_paths.jobs.launch import (
    LAUNCH_STATE_PATH,
    evaluate_launch_checklist,
    launch_job,
    launch_status,
)
from wayfinder_paths.jobs.models import (
    LIFECYCLE_CONTRACTS,
    WayfinderJob,
    coerce_execution_contract,
)
from wayfinder_paths.jobs.risk_flags import acknowledge_risk_flags, risk_flags
from wayfinder_paths.jobs.store import JobStore

SCRIPT = """
from wayfinder_paths.jobs.freestyle import FreestyleSpec

SPEC = FreestyleSpec(max_notional_per_tick=100, max_loss_usd=5, halt_when={"max_drawdown": -0.2})

def tick(ctx):
    if "ETH" not in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "market", "symbol": "ETH", "side": "long",
                 "notional": 50, "max_loss": 5})
"""


class _CaptureBridge:
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


def _patch(monkeypatch) -> _CaptureBridge:
    bridge = _CaptureBridge()
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
        source={"kind": "freestyle", "origin": "inline"},
    )
    root = store.init_layout(job)
    (root / "workspace" / "src" / "eth_dip.py").write_text(SCRIPT, encoding="utf-8")
    store.save(job)
    return store, job


def test_contract_round_trip_and_coercion() -> None:
    assert coerce_execution_contract("freestyle_v1") == "freestyle_v1"
    assert coerce_execution_contract("path_v1") == "path_v1"
    assert coerce_execution_contract("bogus") == "legacy"
    job = WayfinderJob.new(
        "x",
        script="workspace/src/x.py",
        interval_seconds=60,
        execution_contract="freestyle_v1",
        source={"kind": "freestyle"},
    )
    data = job.to_dict()
    assert data["execution_contract"] == "freestyle_v1" and data["source"] == {
        "kind": "freestyle"
    }
    assert WayfinderJob.from_dict(data).source == {"kind": "freestyle"}
    plain = WayfinderJob.new("y", script="workspace/src/y.py", interval_seconds=60)
    assert (
        "source" not in plain.to_dict()
    )  # existing jobs keep their revision byte-for-byte
    assert LIFECYCLE_CONTRACTS == {"jobs_v1", "freestyle_v1", "path_v1"}


def test_checklist_refuses_without_validation_then_passes_at_revision(
    tmp_path: Path,
) -> None:
    store, job = _freestyle(tmp_path)
    before = evaluate_launch_checklist(job.id, store=store)
    assert before["ok"] is False
    assert any(
        i["id"] == "validation_present" and i["status"] == "fail"
        for i in before["items"]
    )

    validate_freestyle_job(job.id, store=store)
    after = evaluate_launch_checklist(job.id, store=store)
    assert after["ok"] is True, after["reasons"]
    assert after["identity"]["validation"] == after["revision"]
    assert any(
        i["id"] == "mechanical_dry_run" and i["status"] == "pass"
        for i in after["items"]
    )
    # warn flags are shown, never a paper blocker
    assert any(
        i["id"].startswith("risk:") and i["status"] == "warn" for i in after["items"]
    )

    # editing the workspace after validation is a stale identity
    root = store.job_dir(job.id)
    (root / "workspace" / "src" / "eth_dip.py").write_text(
        SCRIPT + "\n# tweak\n", encoding="utf-8"
    )
    stale = evaluate_launch_checklist(job.id, store=store)
    assert stale["ok"] is False
    assert any(
        i["id"] == "validation_at_revision" and i["status"] == "fail"
        for i in stale["items"]
    )


def test_launch_pins_the_revision_bakes_it_and_resumes_loops(
    tmp_path: Path, monkeypatch
) -> None:
    bridge = _patch(monkeypatch)
    store, job = _freestyle(tmp_path)
    validate_freestyle_job(job.id, store=store)
    result = launch_job(job.id, store=store)
    assert result["launched"], result["checklist"]["reasons"]
    revision = result["revision"]
    assert store.load(job.id).versioning["active_revision"] == revision
    baked = [c for c in bridge.calls if c["name"].endswith("-script")][-1]
    assert baked["env"]["WAYFINDER_JOB_REVISION"] == revision
    assert baked["env"]["WAYFINDER_JOB_EXECUTION_CONTRACT"] == "freestyle_v1"
    assert ("resume", job.script_loop.runner_job_name) in bridge.actions
    launch_state = json.loads((store.job_dir(job.id) / LAUNCH_STATE_PATH).read_text())
    assert launch_state["mode"] == "paper" and launch_state["revision"] == revision
    assert "no_max_daily_loss" in launch_state["flags_shown"]


def test_live_launch_needs_acknowledged_flags_wallet_limits_and_runs(
    tmp_path: Path, monkeypatch
) -> None:
    _patch(monkeypatch)
    store, job = _freestyle(tmp_path)
    validate_freestyle_job(job.id, store=store)
    blocked = launch_job(job.id, store=store, script_mode="live", confirm_live=True)
    assert blocked["launched"] is False
    ids = {i["id"]: i["status"] for i in blocked["checklist"]["items"]}
    assert ids["wallet_label"] == "fail" and ids["risk_limits_file"] == "fail"
    assert ids["min_paper_runs"] == "fail"
    assert any(status == "ack_required" for status in ids.values())

    readiness = evaluate_live_readiness(job.id, store=store)
    assert readiness["live_ready"] is False and readiness["reasons"]

    root = store.job_dir(job.id)
    (root / "workspace" / "risk_limits.json").write_text(
        json.dumps({"max_daily_loss_usd": 25, "max_drawdown": -0.1}), encoding="utf-8"
    )
    validate_freestyle_job(job.id, store=store)  # workspace changed -> re-validate
    job = store.load(job.id)
    job.execution_params["wallet_label"] = "main"
    store.save(job)
    (root / "results" / "forward").mkdir(parents=True, exist_ok=True)
    (root / "results" / "forward" / "summary.json").write_text(
        json.dumps({"runs": {"count": 25}}), encoding="utf-8"
    )
    flags = {f["code"]: f for f in risk_flags(store.load(job.id), root)}
    warn_codes = [code for code, f in flags.items() if f["severity"] == "warn"]
    acknowledge_risk_flags(
        store, job.id, warn_codes, by="owner", memo="read the readout"
    )
    checklist = evaluate_launch_checklist(job.id, store=store, target="live")
    assert checklist["ok"] is True, checklist["reasons"]
    assert evaluate_live_readiness(job.id, store=store)["live_ready"] is True


def test_block_flags_cannot_be_acknowledged(tmp_path: Path) -> None:
    store, job = _freestyle(tmp_path)
    root = store.job_dir(job.id)
    (root / "constitution.yaml").write_text(
        "hard_constraints:\n  max_leverage: 3\n", encoding="utf-8"
    )
    job.execution_params["leverage"] = 10
    store.save(job)
    flags = {f["code"]: f for f in risk_flags(store.load(job.id), root)}
    assert flags["leverage_above_governance"]["severity"] == "block"
    with pytest.raises(ValueError):
        acknowledge_risk_flags(store, job.id, ["leverage_above_governance"], by="owner")
    checklist = evaluate_launch_checklist(job.id, store=store)
    assert any(
        i["id"] == "risk:leverage_above_governance" and i["status"] == "fail"
        for i in checklist["items"]
    )


def test_jobs_v1_native_stop_flag_follows_the_engine_policy(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "carry",
        script="workspace/src/carry.py",
        interval_seconds=60,
        execution_contract="jobs_v1",
    )
    store.save(job)
    root = store.job_dir(job.id)

    # Unset on hyperliquid: the engine places a venue stop by default.
    flags = {f["code"]: f for f in risk_flags(store.load(job.id), root)}
    assert "no_native_stop" not in flags
    # The job-level flag cannot conjure a stop price: a strategy that never
    # emits one is still flagged.
    assert flags["no_stop_loss"]["severity"] == "warn"

    job.execution_params["native_stop_required"] = False
    store.save(job)
    flags = {f["code"]: f for f in risk_flags(store.load(job.id), root)}
    assert flags["no_native_stop"]["severity"] == "warn"
    assert "native_stop_required: false" in flags["no_native_stop"]["message"]
    checklist = evaluate_launch_checklist(job.id, store=store, target="live")
    assert any(
        i["id"] == "risk:no_native_stop" and i["status"] == "ack_required"
        for i in checklist["items"]
    )

    job.execution_params["native_stop_required"] = True
    store.save(job)
    flags = {f["code"]: f for f in risk_flags(store.load(job.id), root)}
    assert "no_native_stop" not in flags

    del job.execution_params["native_stop_required"]
    job.execution_spec = {"venues": ["hyperliquid_spot"]}
    store.save(job)
    flags = {f["code"]: f for f in risk_flags(store.load(job.id), root)}
    assert flags["no_native_stop"]["severity"] == "info"
    assert "once per tick" in flags["no_native_stop"]["message"]


def test_launch_status_truth_table() -> None:
    active = {"script": {"enabled": True, "runner_status": "ACTIVE"}}
    pinned = {
        "mode": "paper",
        "launched_at": "2026-08-01T00:00:00+00:00",
        "by": "owner",
    }

    via_launch = launch_status(pinned, active, "live")
    assert via_launch == {
        "launched": True,
        "source": "launch",
        "mode": "paper",
        "launched_at": "2026-08-01T00:00:00+00:00",
        "by": "owner",
    }
    running = launch_status(None, active, "live")
    assert running["launched"] is True
    assert running["source"] == "running" and running["mode"] == "live"
    assert running["launched_at"] is None and running["by"] is None

    not_launched = {
        "launched": False,
        "source": None,
        "mode": None,
        "launched_at": None,
        "by": None,
    }
    for loops in (
        {"script": {"enabled": True, "runner_status": "PAUSED"}},
        {"script": {"enabled": True, "runner_status": None}},
        {"script": {"enabled": False, "runner_status": "ACTIVE"}},
        {},
    ):
        assert launch_status(None, loops, "live") == not_launched


def test_live_target_checklist_never_asks_to_go_paper(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "carry",
        script="workspace/src/carry.py",
        interval_seconds=60,
        execution_contract="jobs_v1",
    )
    job.script_loop.mode = "live"
    store.save(job)

    paper = evaluate_launch_checklist(job.id, store=store, target="paper")
    assert any(
        i["id"] == "mode_is_paper" and i["status"] == "fail" for i in paper["items"]
    )
    gate = {
        "live_ready": False,
        "reasons": ["validation report is for revision a, workspace is b"],
    }
    live = evaluate_launch_checklist(job.id, store=store, target="live", live_gate=gate)
    ids = {i["id"] for i in live["items"]}
    assert "mode_is_paper" not in ids
    live_gate_item = next(i for i in live["items"] if i["id"] == "live_gate")
    assert live_gate_item["status"] == "fail"
    assert live_gate_item["detail"] == gate["reasons"][0]


def test_jobs_v1_readiness_still_is_the_live_gate(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "carry",
        script="workspace/src/carry.py",
        interval_seconds=60,
        execution_contract="jobs_v1",
    )
    store.save(job)
    gate = evaluate_live_readiness(job.id, store=store)
    assert gate["live_ready"] is False
    assert any("validation" in reason for reason in gate["reasons"])
    checklist = evaluate_launch_checklist(job.id, store=store)
    assert checklist["kind"] == "jobs_v1"
    assert any(i["id"] == "risk:no_stop_loss" for i in checklist["items"])


def test_owner_drawdown_ceiling_satisfies_the_live_risk_limits_item(
    tmp_path: Path,
) -> None:
    store, job = _freestyle(tmp_path)
    root = store.job_dir(job.id)

    def risk_limits_item() -> dict:
        checklist = evaluate_launch_checklist(job.id, store=store, target="live")
        return next(i for i in checklist["items"] if i["id"] == "risk_limits_file")

    assert risk_limits_item()["status"] == "fail"
    (root / "constitution.yaml").write_text(
        "hard_constraints:\n  max_drawdown: 0.15\n", encoding="utf-8"
    )
    assert risk_limits_item()["status"] == "pass"
