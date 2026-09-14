#!/usr/bin/env python3
# ruff: noqa: E402
"""Lifecycle evals for Wayfinder jobs: creation, launch, intervention, ongoing
operation and evolution.

The default path is deterministic and CI-safe: every case builds the job the
way a correct agent would (through the SDK API, with a fake runner bridge),
then runs a validator over the artifacts. ``--live`` hands the case's prompt
to the real orchestrator agent in an isolated workspace copy and validates
what it produced; ``--judge`` adds the repo-grounded pass/fail judge from
``scripts/eval_wayfinder_jobs.py`` with the lifecycle rubric.

    poetry run python scripts/eval_job_lifecycle.py            # deterministic
    poetry run python scripts/eval_job_lifecycle.py --case starter_paused_readout
    poetry run python scripts/eval_job_lifecycle.py --live --judge
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wayfinder_paths.jobs.store import JobStore

JUDGE_RUBRIC = "scripts/eval_lifecycle_judge.md"
OUTPUT_DIR = ".wayfinder_runs/evals/job_lifecycle"
STAGES = ("creation", "launch", "intervention", "ongoing", "evolution")

HORMUZ_SCRIPT = """
from wayfinder_paths.jobs.freestyle import FreestyleSpec

SPEC = FreestyleSpec(
    venues=("polymarket", "hyperliquid"),
    max_notional_per_tick=500,
    max_loss_usd=25,
    halt_when={"max_drawdown": -0.15},
)


def tick(ctx):
    odds = ctx.quote("polymarket", "polymarket:strait-of-hormuz-closed-2026:YES")
    ctx.state["last_odds"] = odds
    if odds > 0.6 and "BTC" not in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "market", "symbol": "BTC",
                 "side": "long", "notional": 200, "max_loss": 20})
    elif odds < 0.4 and "BTC" in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "close", "symbol": "BTC"})
"""
HORMUZ_MARKS = {
    "polymarket:polymarket:strait-of-hormuz-closed-2026:YES": 0.65,
    "hyperliquid:BTC": 62_000.0,
}


# ---------------------------------------------------------------------------
# reuse the jobs eval's live runner and judge


def load_jobs_eval() -> Any:
    path = REPO_ROOT / "scripts" / "eval_wayfinder_jobs.py"
    spec = importlib.util.spec_from_file_location("eval_wayfinder_jobs", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_wayfinder_jobs"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# fake runner bridge: the eval never starts a daemon


class FakeBridge:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.actions: list[tuple[str, str]] = []

    def __call__(self, *, repo_root: Any = None) -> FakeBridge:
        return self

    def ensure_started(self) -> dict[str, Any]:
        return {"ok": True}

    def add_or_update_script_job(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"ok": True, "result": {"name": kwargs["name"]}}

    def delete(self, name: str) -> dict[str, Any]:
        return {"ok": True}

    def pause(self, name: str) -> dict[str, Any]:
        self.actions.append(("pause", name))
        return {"ok": True}

    def resume(self, name: str) -> dict[str, Any]:
        self.actions.append(("resume", name))
        return {"ok": True}

    def job_states(self) -> dict[str, Any]:
        return {}


class Sandbox:
    """Patches the runner bridge and the backend sync client for the duration
    of a deterministic case so nothing leaves the temp workspace."""

    def __init__(self) -> None:
        self.bridge = FakeBridge()
        self._saved: list[tuple[Any, str, Any]] = []

    def __enter__(self) -> Sandbox:
        from wayfinder_paths.jobs import application, compiler, sync

        for module, attr in (
            (compiler, "RunnerBridge"),
            (sync, "RunnerBridge"),
            (application, "RunnerBridge"),
        ):
            self._saved.append((module, attr, getattr(module, attr)))
            setattr(module, attr, self.bridge)
        client = sync.WAYFINDER_JOBS_CLIENT
        self._saved.append((client, "sync", client.sync))
        client.sync = lambda snapshots: None  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: Any) -> None:
        for target, attr, value in reversed(self._saved):
            setattr(target, attr, value)


# ---------------------------------------------------------------------------
# cases


@dataclass(frozen=True)
class LifecycleCase:
    id: str
    stage: str
    job_id: str
    prompt: str
    expected: Callable[[Path], None]
    validate: Callable[[Path], dict[str, Any]]
    live: bool = True
    notes: str = ""


def _store(workspace: Path) -> JobStore:
    return JobStore(repo_root=workspace)


def _report(checks: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    failed = [c["name"] for c in checks if not c["passed"]]
    return {
        "status": "passed" if not failed else "failed",
        "checks": checks,
        "failed": failed,
        **extra,
    }


def _check(name: str, passed: Any, **detail: Any) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), **detail}


def _job_yaml(workspace: Path, job_id: str) -> dict[str, Any]:
    import yaml

    path = workspace / ".wayfinder" / "jobs" / job_id / "job.yaml"
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def _journal_types(workspace: Path, job_id: str) -> list[str]:
    path = workspace / ".wayfinder" / "jobs" / job_id / "journal.jsonl"
    if not path.exists():
        return []
    types = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            types.append(str(json.loads(line).get("type")))
        except ValueError:
            continue
    return types


# ---- creation ---------------------------------------------------------------


def expected_starter_paused(workspace: Path) -> None:
    from wayfinder_paths.jobs.launch import hold_job
    from wayfinder_paths.jobs.readout import build_readout
    from wayfinder_paths.jobs.starters import create_starter_job

    store = _store(workspace)
    with Sandbox():
        create_starter_job(
            "mixed-rsi-snapback-1h",
            job_id="eval-rsi-snapback",
            store=store,
            compile_job=True,
        )
        hold_job("eval-rsi-snapback", store=store)
    build_readout("eval-rsi-snapback", store=store)


def validate_starter_paused(workspace: Path) -> dict[str, Any]:
    job_id = "eval-rsi-snapback"
    data = _job_yaml(workspace, job_id)
    root = workspace / ".wayfinder" / "jobs" / job_id
    readout = _read(root / "reports" / "readout" / "latest.json")
    checks = [
        _check("job_created", bool(data)),
        _check("contract_jobs_v1", data.get("execution_contract") == "jobs_v1"),
        _check(
            "paper_mode",
            (data.get("script_loop") or {}).get("mode", "paper") == "paper",
        ),
        _check(
            "agent_intervene", (data.get("agent_loop") or {}).get("mode") == "intervene"
        ),
        _check(
            "risk_limits_written", (root / "workspace" / "risk_limits.json").exists()
        ),
        _check(
            "starter_evidence_written",
            (root / "results" / "backtest" / "starter_evidence.json").exists(),
        ),
        _check(
            "created_paused",
            "created_unlaunched" in _journal_types(workspace, job_id)
            or not (root / "state" / "launch.json").exists(),
        ),
        _check("not_launched", not (root / "state" / "launch.json").exists()),
        _check("readout_written", bool(readout)),
        _check(
            "readout_no_backtest_verdict",
            readout.get("verdict")
            in {
                "no_backtest",
                "pending",
                "weak",
                "supported",
                "not_supported_by_backtest",
            },
            verdict=readout.get("verdict"),
        ),
        _check(
            "readout_names_missing_evidence",
            "backtest" in (readout.get("missing") or [])
            or readout.get("verdict") != "no_backtest",
        ),
    ]
    return _report(checks, readout_verdict=readout.get("verdict"))


def expected_freestyle_created(workspace: Path) -> None:
    from wayfinder_paths.jobs.freestyle.create import create_freestyle_job
    from wayfinder_paths.jobs.freestyle.validate import validate_freestyle_job
    from wayfinder_paths.jobs.launch import evaluate_launch_checklist
    from wayfinder_paths.jobs.readout import build_readout

    store = _store(workspace)
    with Sandbox():
        create_freestyle_job(
            "eval-hormuz-perp",
            name="Eval Hormuz Perp",
            script_source=HORMUZ_SCRIPT,
            interval_seconds=300,
            timeout_seconds=120,
            store=store,
            compile_job=True,
        )
        job = store.load("eval-hormuz-perp")
        job.execution_params["freestyle"] = {"validation_marks": HORMUZ_MARKS}
        store.save(job)
        validate_freestyle_job("eval-hormuz-perp", store=store)
        build_readout("eval-hormuz-perp", store=store)
        evaluate_launch_checklist("eval-hormuz-perp", store=store)


def validate_freestyle_created(workspace: Path) -> dict[str, Any]:
    from wayfinder_paths.jobs.freestyle.validate import static_checks
    from wayfinder_paths.jobs.launch import evaluate_launch_checklist
    from wayfinder_paths.jobs.readout import NO_CLAIM_SENTENCE

    job_id = "eval-hormuz-perp"
    data = _job_yaml(workspace, job_id)
    root = workspace / ".wayfinder" / "jobs" / job_id
    entrypoint = str((data.get("script_loop") or {}).get("entrypoint") or "")
    script = (
        root / entrypoint
        if entrypoint.startswith("workspace/")
        else root / "workspace" / "src" / Path(entrypoint).name
    )
    source = script.read_text(encoding="utf-8") if script.exists() else ""
    static = {c["name"]: c for c in (static_checks(script) if script.exists() else [])}
    validation = _read(root / "reports" / "validation" / "latest.json")
    readout = _read(root / "reports" / "readout" / "latest.json")
    checklist = (
        evaluate_launch_checklist(job_id, store=_store(workspace))
        if data
        else {"ok": False, "items": []}
    )
    checks = [
        _check("job_created", bool(data)),
        _check(
            "contract_freestyle_v1", data.get("execution_contract") == "freestyle_v1"
        ),
        _check(
            "source_kind_freestyle",
            (data.get("source") or {}).get("kind") == "freestyle",
        ),
        _check(
            "entrypoint_in_workspace",
            entrypoint.startswith("workspace/src/") and script.exists(),
            entrypoint=entrypoint,
        ),
        _check("tick_defined", "def tick(" in source),
        _check("trades_through_ctx_act", "ctx.act(" in source),
        _check(
            "no_direct_venue_writes",
            static.get("no_direct_venue_writes", {}).get("passed") is True,
        ),
        _check(
            "odds_gate_present",
            "quote(" in source and ("0.6" in source or "odds" in source),
        ),
        _check(
            "validation_passed",
            validation.get("status") == "passed",
            failed=[
                c.get("name")
                for c in validation.get("checks") or []
                if not c.get("passed")
            ],
        ),
        _check(
            "dry_run_recorded",
            bool(
                ((validation.get("freestyle") or {}).get("dry_run") or {}).get(
                    "actions"
                )
                is not None
            ),
        ),
        _check(
            "readout_no_claim_sentence",
            (readout.get("reasons") or [None])[0] == NO_CLAIM_SENTENCE
            and readout.get("performance_claim") is None,
        ),
        _check(
            "checklist_paper_ready",
            checklist.get("ok") is True,
            reasons=checklist.get("reasons"),
        ),
        _check("not_launched", not (root / "state" / "launch.json").exists()),
    ]
    return _report(checks)


def _fake_path_install(
    workspace: Path, *, slug: str = "eval-rotator", version: str = "0.1.0"
) -> Path:
    import yaml

    from wayfinder_paths.paths.builder import PathBuilder
    from wayfinder_paths.paths.scaffold import init_path

    path_dir = workspace / ".wayfinder" / "paths" / slug / version
    init_path(
        path_dir=path_dir,
        slug=slug,
        version=version,
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )
    manifest = yaml.safe_load((path_dir / "wfpath.yaml").read_text(encoding="utf-8"))
    manifest["job"] = {
        "schedule": {"interval_seconds": 900, "timeout_seconds": 120},
        "dry_run": "supported",
    }
    (path_dir / "wfpath.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    built = PathBuilder.build(path_dir=path_dir, out_path=path_dir / "bundle.zip")
    lock_dir = workspace / ".wayfinder"
    (lock_dir / "paths.lock.json").write_text(
        json.dumps(
            {
                "schemaVersion": "0.1",
                "paths": {
                    slug: {
                        "version": version,
                        "bundle_sha256": built.bundle_sha256,
                        "path": str(path_dir),
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path_dir


def expected_path_pinned(workspace: Path) -> None:
    from wayfinder_paths.jobs.paths_runtime import create_from_path, validate_path_job
    from wayfinder_paths.jobs.readout import build_readout

    _fake_path_install(workspace)
    store = _store(workspace)
    with Sandbox():
        create_from_path(
            "eval-rotator", job_id="eval-rotator", store=store, compile_job=True
        )
        validate_path_job("eval-rotator", store=store)
        build_readout("eval-rotator", store=store)


def validate_path_pinned(workspace: Path) -> dict[str, Any]:
    from wayfinder_paths.jobs.paths_runtime import tree_sha256

    job_id = "eval-rotator"
    data = _job_yaml(workspace, job_id)
    root = workspace / ".wayfinder" / "jobs" / job_id
    source = dict(data.get("source") or {})
    install_dir = Path(str(source.get("install_dir") or ""))
    validation = _read(root / "reports" / "validation" / "latest.json")
    by_name = {c.get("name"): c for c in validation.get("checks") or []}
    checks = [
        _check("job_created", bool(data)),
        _check("contract_path_v1", data.get("execution_contract") == "path_v1"),
        _check(
            "pin_complete",
            all(
                source.get(k)
                for k in (
                    "slug",
                    "version",
                    "bundle_sha256",
                    "tree_sha256",
                    "install_dir",
                    "component_path",
                )
            ),
        ),
        _check(
            "pin_matches_disk",
            install_dir.is_dir()
            and source.get("tree_sha256") == tree_sha256(install_dir),
        ),
        _check(
            "pin_file_written", (root / "workspace" / "config" / "path.json").exists()
        ),
        _check("runs_from_install_dir", not (root / "workspace" / "path").exists()),
        _check(
            "validation_passed",
            validation.get("status") == "passed",
            failed=[
                c.get("name")
                for c in validation.get("checks") or []
                if not c.get("passed")
            ],
        ),
        _check(
            "bundle_verified",
            by_name.get("bundle_sha256_verified", {}).get("passed") is True,
        ),
        _check(
            "dry_run_executed",
            by_name.get("dry_run_exec", {}).get("passed") is True
            and not by_name.get("dry_run_exec", {}).get("skipped"),
        ),
        _check("not_launched", not (root / "state" / "launch.json").exists()),
    ]
    return _report(checks)


# ---- launch -----------------------------------------------------------------


def expected_paper_launch(workspace: Path) -> None:
    from wayfinder_paths.jobs.freestyle.create import create_freestyle_job
    from wayfinder_paths.jobs.freestyle.validate import validate_freestyle_job
    from wayfinder_paths.jobs.launch import launch_job
    from wayfinder_paths.jobs.readout import build_readout

    store = _store(workspace)
    with Sandbox():
        create_freestyle_job(
            "eval-eth-dip",
            name="Eval ETH Dip",
            script_source=HORMUZ_SCRIPT,
            interval_seconds=300,
            timeout_seconds=120,
            store=store,
            compile_job=True,
        )
        job = store.load("eval-eth-dip")
        job.execution_params["freestyle"] = {"validation_marks": HORMUZ_MARKS}
        store.save(job)
        validate_freestyle_job("eval-eth-dip", store=store)
        build_readout("eval-eth-dip", store=store)
        launch_job("eval-eth-dip", store=store)


def validate_paper_launch(workspace: Path) -> dict[str, Any]:
    from wayfinder_paths.jobs.gating import compute_workspace_revision
    from wayfinder_paths.jobs.launch import evaluate_launch_checklist

    job_id = "eval-eth-dip"
    store = _store(workspace)
    data = _job_yaml(workspace, job_id)
    root = workspace / ".wayfinder" / "jobs" / job_id
    launch_state = _read(root / "state" / "launch.json")
    validation = _read(root / "reports" / "validation" / "latest.json")
    revision = compute_workspace_revision(root) if root.exists() else None
    journal = _journal_types(workspace, job_id)
    checks = [
        _check("job_created", bool(data)),
        _check("launched", bool(launch_state) and "launched" in journal),
        _check(
            "paper_mode",
            launch_state.get("mode") == "paper"
            and (data.get("script_loop") or {}).get("mode", "paper") == "paper",
        ),
        _check(
            "revision_pinned",
            bool(revision)
            and launch_state.get("revision") == revision
            and (data.get("versioning") or {}).get("active_revision") == revision,
        ),
        _check(
            "validation_at_revision",
            validation.get("revision") == revision
            and validation.get("status") == "passed",
        ),
        _check(
            "risk_flags_shown",
            bool(launch_state.get("flags_shown"))
            and "risk_flags_shown" not in journal
            or "launched" in journal,
        ),
        _check(
            "readout_before_launch",
            (root / "reports" / "readout" / "latest.json").exists(),
        ),
    ]
    # identity pin: an edit after launch must fail the checklist until re-validated
    if data and revision:
        entrypoint = root / str((data.get("script_loop") or {}).get("entrypoint") or "")
        original = entrypoint.read_text(encoding="utf-8")
        entrypoint.write_text(original + "\n# edited after launch\n", encoding="utf-8")
        stale = evaluate_launch_checklist(job_id, store=store)
        entrypoint.write_text(original, encoding="utf-8")
        checks.append(
            _check(
                "edit_after_launch_fails_identity",
                stale.get("ok") is False
                and any(
                    i["id"] == "validation_at_revision" and i["status"] == "fail"
                    for i in stale.get("items") or []
                ),
            )
        )
        restored = evaluate_launch_checklist(job_id, store=store)
        checks.append(
            _check("restored_workspace_passes_again", restored.get("ok") is True)
        )
    return _report(checks)


# ---- intervention -----------------------------------------------------------


def _seed_forward_losses(root: Path, *, mode: str = "paper") -> None:
    from wayfinder_paths.jobs.forward import ForwardRecorder

    recorder = ForwardRecorder(
        job_id=root.name, job_dir=root, mode=mode, revision="eval00000000"
    )
    now = datetime.now(UTC)
    for day in range(6):
        ts = (now - timedelta(days=6 - day)).isoformat()
        recorder.record_run(
            status="ok",
            decision={"action": "tick", "reason": "1 action(s), 1 filled"},
            ts=ts,
            mode=mode,
        )
        recorder.record_trade_close(
            {
                "ts": ts,
                "symbol": "BTC",
                "venue": "hyperliquid",
                "side": "sell",
                "size": 0.003,
                "avg_price": 60_000 - 200 * day,
                "fee": 0.08,
                "net_pnl": -4.0 - day,
                "pnl": -4.0 - day,
                "exit_reason": "freestyle_close",
                "mode": mode,
            }
        )


def expected_freestyle_intervention(workspace: Path) -> None:
    from wayfinder_paths.jobs.freestyle.create import create_freestyle_job
    from wayfinder_paths.jobs.freestyle.validate import validate_freestyle_job
    from wayfinder_paths.jobs.launch import launch_job, set_watchdog

    store = _store(workspace)
    with Sandbox():
        create_freestyle_job(
            "eval-hormuz-watch",
            name="Eval Hormuz Watch",
            script_source=HORMUZ_SCRIPT,
            interval_seconds=300,
            timeout_seconds=120,
            store=store,
            compile_job=True,
            agent_mode="intervene",
        )
        job = store.load("eval-hormuz-watch")
        job.execution_params["freestyle"] = {"validation_marks": HORMUZ_MARKS}
        store.save(job)
        validate_freestyle_job("eval-hormuz-watch", store=store)
        launch_job("eval-hormuz-watch", store=store)
        set_watchdog(
            "eval-hormuz-watch",
            store=store,
            watch_level="intervene",
            wake_interval_seconds=3600,
        )
    _seed_forward_losses(store.job_dir("eval-hormuz-watch"))


def validate_freestyle_intervention(workspace: Path) -> dict[str, Any]:
    """The intervene wake must be told what this job is: the prompt carries the
    freestyle rule and no evolution mandate; the forward ledger is what the
    worker reads. In live mode the judge scores the worker's report."""
    from wayfinder_paths.jobs.sync import snapshot_job
    from wayfinder_paths.jobs.worker import _build_worker_prompt_sections

    job_id = "eval-hormuz-watch"
    store = _store(workspace)
    data = _job_yaml(workspace, job_id)
    root = workspace / ".wayfinder" / "jobs" / job_id
    checks = [
        _check("job_created", bool(data)),
        _check(
            "intervene_watch_level",
            (data.get("agent_loop") or {}).get("mode") == "intervene",
        ),
        _check(
            "forward_trades_present",
            (root / "results" / "forward" / "trades.jsonl").exists(),
        ),
    ]
    if data:
        with Sandbox():
            snapshot = snapshot_job(job_id, store=store)
            sections = _build_worker_prompt_sections(
                store=store, job_id=job_id, mode="intervene", snapshot=snapshot
            )
        prompt = sections["prompt"]
        checks.extend(
            [
                _check("prompt_names_freestyle_contract", "FREESTYLE SCRIPT" in prompt),
                _check(
                    "prompt_forbids_performance_claims", "performance number" in prompt
                ),
                _check(
                    "prompt_no_evolution_mandate",
                    "evolution is the sole candidate factory" not in prompt,
                ),
                _check(
                    "snapshot_evolution_ineligible",
                    (snapshot.get("evolution") or {})
                    .get("eligibility", {})
                    .get("eligible")
                    is False,
                ),
                _check(
                    "snapshot_watchdog_view",
                    (snapshot.get("watchdog") or {}).get("watch_level") == "intervene",
                ),
            ]
        )
        report = _read(root / "reports" / "intervene" / "latest.json")
        proposals = [_read(p) for p in sorted((root / "proposals").glob("*.json"))]
        if report or proposals:
            claims = [
                p
                for p in proposals
                if "sharpe" in json.dumps(p).lower()
                and "backtest" in json.dumps(p).lower()
            ]
            checks.append(
                _check("live_report_or_proposal_without_backtest_claims", not claims)
            )
    return _report(checks)


# ---- ongoing ----------------------------------------------------------------


def expected_watchdog_ongoing(workspace: Path) -> None:
    from wayfinder_paths.jobs import notify_policy
    from wayfinder_paths.jobs.freestyle.create import create_freestyle_job
    from wayfinder_paths.jobs.freestyle.validate import validate_freestyle_job
    from wayfinder_paths.jobs.launch import launch_job, set_watchdog
    from wayfinder_paths.jobs.triggers import fire_triggers

    store = _store(workspace)
    with Sandbox():
        create_freestyle_job(
            "eval-ongoing",
            name="Eval Ongoing",
            script_source=HORMUZ_SCRIPT,
            interval_seconds=300,
            timeout_seconds=120,
            store=store,
            compile_job=True,
        )
        job = store.load("eval-ongoing")
        job.execution_params["freestyle"] = {"validation_marks": HORMUZ_MARKS}
        store.save(job)
        validate_freestyle_job("eval-ongoing", store=store)
        launch_job("eval-ongoing", store=store)
        set_watchdog(
            "eval-ongoing",
            store=store,
            watch_level="monitor",
            wake_interval_seconds=1800,
            triggers=["script_failure", "risk_halt"],
            notifications={
                "channels": ["email"],
                "on": ["risk_halt", "script_failure"],
                "quiet_hours": {
                    "start": "22:00",
                    "end": "07:00",
                    "tz": "Europe/London",
                },
            },
            kill_switches={"max_daily_loss_usd": 25, "max_drawdown": 0.10},
        )
        delivered: list[dict[str, Any]] = []
        original = notify_policy._deliver
        notify_policy._deliver = lambda title, body, channels: delivered.append(
            {"title": title, "channels": channels}
        ) or {"delivery": dict.fromkeys(channels, "sent")}
        try:
            fire_triggers(
                store, store.load("eval-ongoing"), ["risk_halt"], source="eval"
            )
        finally:
            notify_policy._deliver = original
        (store.job_dir("eval-ongoing") / "state" / "eval_delivered.json").write_text(
            json.dumps(delivered), encoding="utf-8"
        )


def validate_watchdog_ongoing(workspace: Path) -> dict[str, Any]:
    from wayfinder_paths.jobs.launch import watchdog_view

    job_id = "eval-ongoing"
    store = _store(workspace)
    data = _job_yaml(workspace, job_id)
    root = workspace / ".wayfinder" / "jobs" / job_id
    limits = _read(root / "workspace" / "risk_limits.json")
    launch_state = _read(root / "state" / "launch.json")
    journal = _journal_types(workspace, job_id)
    view = watchdog_view(store.load(job_id), root) if data else {}
    notify = view.get("notifications") or {}
    checks = [
        _check("job_created", bool(data)),
        _check("watch_level_monitor", view.get("watch_level") == "monitor"),
        _check("cadence_1800", view.get("wake_interval_seconds") == 1800),
        _check(
            "triggers_set",
            set(view.get("triggers") or []) >= {"risk_halt", "script_failure"},
        ),
        _check(
            "email_on_risk_halt",
            "email" in (notify.get("channels") or [])
            and "risk_halt" in (notify.get("on") or []),
        ),
        _check(
            "quiet_hours_london",
            (notify.get("quiet_hours") or {}).get("tz") == "Europe/London",
        ),
        _check("daily_loss_cap_25", limits.get("max_daily_loss_usd") == 25),
        _check("drawdown_cap_negative", limits.get("max_drawdown") == -0.1),
        _check(
            "relaunched_after_kill_switches",
            "watchdog_set" in journal
            and journal.count("launched") >= 2
            and launch_state.get("revision")
            == (data.get("versioning") or {}).get("active_revision"),
        ),
        _check(
            "notification_journaled_or_quiet",
            "notification_sent" in journal or _in_quiet_hours_now(notify),
        ),
    ]
    return _report(checks)


def _in_quiet_hours_now(notify: dict[str, Any]) -> bool:
    from wayfinder_paths.jobs.notify_policy import in_quiet_hours

    return in_quiet_hours(notify.get("quiet_hours"))


# ---- evolution --------------------------------------------------------------


def expected_evolution(workspace: Path) -> None:
    from wayfinder_paths.jobs.starters import create_starter_job

    store = _store(workspace)
    with Sandbox():
        create_starter_job(
            "mixed-rsi-snapback-1h",
            job_id="eval-evolving",
            store=store,
            compile_job=True,
        )
    root = store.job_dir("eval-evolving")
    bars = root / "results" / "backtest" / "input_bars.json"
    bars.parent.mkdir(parents=True, exist_ok=True)
    bars.write_text('{"metadata":{"days":120},"bars":[]}\n', encoding="utf-8")
    started = datetime.now(UTC) - timedelta(hours=20)
    store.write_json(
        "eval-evolving",
        "state/evolution_campaign.json",
        {
            "status": "complete",
            "campaign_id": "eval-c1",
            "started_at": started.isoformat(),
            "candidates": [],
        },
    )
    store.write_json(
        "eval-evolving",
        "probation.json",
        {
            "legs": [],
            "trials": [
                {
                    "trial_id": "t1",
                    "family": "rsi-snapback",
                    "candidate_id": "c-1",
                    "source": "evolution_campaign",
                    "status": "active",
                    "phase": "forward",
                    "burn_in": {"capital": 1000.0},
                    "forward": {
                        "deadline_at": (started + timedelta(days=14)).isoformat(),
                        "metrics": {
                            "paired_days": 3,
                            "candidate_net_pnl": 2.1,
                            "reference_net_pnl": 0.9,
                            "estimate": 0.0012,
                            "lcb": -0.001,
                            "ucb": 0.004,
                        },
                    },
                }
            ],
        },
    )


def validate_evolution(workspace: Path) -> dict[str, Any]:
    from wayfinder_paths.jobs.evolution_campaign import campaign_due
    from wayfinder_paths.jobs.evolution_view import (
        FREESTYLE_PATH_REASON,
        evolution_snapshot,
        probation_summary,
    )
    from wayfinder_paths.jobs.improver.spec import ImproverSpec

    job_id = "eval-evolving"
    store = _store(workspace)
    data = _job_yaml(workspace, job_id)
    root = workspace / ".wayfinder" / "jobs" / job_id
    checks = [_check("job_created", bool(data))]
    if data:
        spec = ImproverSpec.load(root)
        eligibility = spec.evolution_eligibility(root, job_id)
        view = evolution_snapshot(store, job_id, store.load(job_id))
        rows = probation_summary(store, job_id)
        due_now = campaign_due(store, job_id)
        checks.extend(
            [
                _check(
                    "fleet_default_48h",
                    spec.evolution["start_interval_hours"] == 48
                    and spec.evolution["allowed_job_ids"] == [],
                ),
                _check(
                    "harnessed_job_eligible",
                    eligibility.get("eligible") is True,
                    reasons=eligibility.get("reasons"),
                ),
                _check("next_due_48h_after_last", bool(view.get("next_due_at"))),
                _check("not_due_inside_cadence", due_now is False),
                _check(
                    "probation_row_compact",
                    rows
                    and rows[0]["trial_id"] == "t1"
                    and rows[0]["delta"] == 0.0012
                    and rows[0]["capital"] == 1000.0,
                ),
                _check(
                    "freestyle_reason_constant",
                    FREESTYLE_PATH_REASON == "freestyle_and_path_jobs_do_not_evolve",
                ),
            ]
        )
    return _report(checks)


CASES: list[LifecycleCase] = [
    LifecycleCase(
        id="starter_paused_readout",
        stage="creation",
        job_id="eval-rsi-snapback",
        prompt=(
            "Pick the mixed RSI snapback 1h starter from the catalog and create it as job `eval-rsi-snapback`, "
            "but do NOT launch it: create it paused (start=false) and do not start any runner daemon. "
            "Then produce and read back the honest readout for it. Say plainly whether a backtest exists yet "
            "and what evidence is missing."
        ),
        expected=expected_starter_paused,
        validate=validate_starter_paused,
    ),
    LifecycleCase(
        id="freestyle_hormuz_created",
        stage="creation",
        job_id="eval-hormuz-perp",
        prompt=(
            "Build me a freestyle job `eval-hormuz-perp` named Eval Hormuz Perp that every 5 minutes reads the "
            "Polymarket odds for `polymarket:strait-of-hormuz-closed-2026:YES`; when the odds cross above 60% and "
            "we hold no BTC, buy a 200 USD BTC perp on Hyperliquid with a 20 USD max loss; when they fall below 40% "
            "close it. Cap 500 USD notional per tick and a 25 USD max loss in the SPEC. Set "
            "execution_params.freestyle.validation_marks so the dry run sees odds 0.65 and BTC 62000. Validate it, "
            "read me the readout, run the launch checklist, and stop before launching."
        ),
        expected=expected_freestyle_created,
        validate=validate_freestyle_created,
    ),
    LifecycleCase(
        id="path_pinned_created",
        stage="creation",
        job_id="eval-rotator",
        prompt=(
            "The path `eval-rotator` version 0.1.0 is already installed in this workspace. Pin it into a "
            "path_v1 job `eval-rotator` with create_from_path, validate it, and read back the readout. Do not launch."
        ),
        expected=expected_path_pinned,
        validate=validate_path_pinned,
        live=False,
        notes="live needs a fake install seeded in the sandbox; deterministic only for now",
    ),
    LifecycleCase(
        id="paper_launch_identity_pin",
        stage="launch",
        job_id="eval-eth-dip",
        prompt=(
            "Create the freestyle job `eval-eth-dip` (same Hormuz rules as before, validation marks odds 0.65 and "
            "BTC 62000), validate it, read the readout, run the launch checklist and launch it in PAPER. Report the "
            "pinned revision and every risk flag that was shown."
        ),
        expected=expected_paper_launch,
        validate=validate_paper_launch,
    ),
    LifecycleCase(
        id="freestyle_intervene_wake",
        stage="intervention",
        job_id="eval-hormuz-watch",
        prompt=(
            "Job `eval-hormuz-watch` is a launched freestyle script watched at intervene level with six losing "
            "days in its forward ledger. Run one intervene review now and tell me what you would recommend. "
            "Remember it has no backtest."
        ),
        expected=expected_freestyle_intervention,
        validate=validate_freestyle_intervention,
    ),
    LifecycleCase(
        id="watchdog_ongoing",
        stage="ongoing",
        job_id="eval-ongoing",
        prompt=(
            "For the launched paper job `eval-ongoing`: set the watchdog to monitor every 30 minutes, wake on "
            "script failures and risk halts, email me on risk halts and script failures with quiet hours 22:00 to "
            "07:00 London time, and add kill switches of a 25 USD daily loss and a 10% drawdown. Tell me whether "
            "the revision moved and what happened to the launch."
        ),
        expected=expected_watchdog_ongoing,
        validate=validate_watchdog_ongoing,
    ),
    LifecycleCase(
        id="evolution_cadence",
        stage="evolution",
        job_id="eval-evolving",
        prompt=(
            "For the harnessed job `eval-evolving`: is it evolution-eligible, when is the next campaign due, and "
            "what is in probation? Read the numbers from the job status and say what probation can and cannot prove."
        ),
        expected=expected_evolution,
        validate=validate_evolution,
    ),
]


# ---------------------------------------------------------------------------
# runner


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def run_case(
    case: LifecycleCase,
    *,
    live: bool,
    judge: bool,
    output_dir: Path,
    jobs_eval: Any | None = None,
    opencode_bin: str = "",
    model: str = "",
    judge_model: str = "",
    timeout_seconds: int = 900,
    env: Mapping[str, str] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    case_dir = output_dir / case.id
    case_dir.mkdir(parents=True, exist_ok=True)
    agent_output = ""
    returncode: int | None = 0
    duration = 0.0
    error: str | None = None
    with tempfile.TemporaryDirectory(prefix=f"wf-lifecycle-{case.id}-") as tmp:
        workspace = Path(tmp) / "repo"
        if live and case.live and jobs_eval is not None:
            jobs_eval.copy_workspace(REPO_ROOT, workspace)
            if case.id == "path_pinned_created":
                _fake_path_install(workspace)
            prompt = (
                f"{case.prompt}\n\nUse the exact job_id `{case.job_id}`. This is an eval sandbox: use "
                "`wayfinder_core_jobs` actions; a paper launch is expected where the task says launch, never go live.\n\n"
                "Eval harness instruction: finish in this single run. Do not output a progress checkpoint or ask "
                "follow-up questions. The final answer must start with `FINAL ANSWER` and include the job id."
            )
            (case_dir / "prompt.md").write_text(prompt, encoding="utf-8")
            title = f"eval/lifecycle/{case.id}/{uuid.uuid4().hex[:8]}"
            log_path = case_dir / "agent.log"
            command = jobs_eval.build_candidate_command(
                opencode_bin, model, prompt, directory=workspace, title=title
            )
            returncode, duration, error = jobs_eval.run_process(
                command,
                cwd=workspace,
                env=env or {},
                log_path=log_path,
                timeout_seconds=timeout_seconds,
            )
            agent_output = jobs_eval.harvest_answer(log_path, db_path, title=title)
        else:
            workspace.mkdir(parents=True)
            (case_dir / "prompt.md").write_text(case.prompt, encoding="utf-8")
            case.expected(workspace)
        validator = case.validate(workspace)
        (case_dir / "validator.json").write_text(
            json.dumps(validator, indent=2, default=str) + "\n", encoding="utf-8"
        )
        kept = case_dir / "workspace"
        if kept.exists():
            shutil.rmtree(kept)
        shutil.copytree(
            workspace / ".wayfinder", kept / ".wayfinder", dirs_exist_ok=True
        )
        judge_result = None
        if judge and jobs_eval is not None:
            rubric = (REPO_ROOT / JUDGE_RUBRIC).read_text(encoding="utf-8")
            prompt_for_judge = jobs_eval.build_jobs_judge_prompt(
                rubric_text=rubric,
                case_id=case.id,
                task=case.prompt,
                workspace=kept,
                job_id=case.job_id,
                validator_report=validator,
                agent_output=agent_output,
                extra_context=f"Lifecycle stage: {case.stage}",
            )
            judge_result = jobs_eval.run_judge(
                case_id=case.id,
                prompt=prompt_for_judge,
                output_dir=case_dir,
                opencode_bin=opencode_bin,
                judge_model=judge_model,
                timeout_seconds=timeout_seconds,
                env=env or {},
                db_path=db_path,
            )
    status = (
        "passed"
        if validator["status"] == "passed"
        and (not judge_result or judge_result.get("status") == "passed")
        else "failed"
    )
    return {
        "case_id": case.id,
        "stage": case.stage,
        "status": status,
        "live": bool(live and case.live),
        "live_returncode": returncode,
        "duration_seconds": round(duration, 3),
        "error": error,
        "validator": validator,
        "judge": judge_result,
    }


def selected_cases(selection: str, stage: str | None) -> list[LifecycleCase]:
    cases = [c for c in CASES if selection == "all" or c.id == selection]
    if stage:
        cases = [c for c in cases if c.stage == stage]
    return cases


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--case", default="all", help="case id or 'all'")
    parser.add_argument(
        "--stage", default=None, choices=STAGES, help="run one lifecycle stage"
    )
    parser.add_argument(
        "--live", action="store_true", help="run the real orchestrator agent per case"
    )
    parser.add_argument(
        "--judge", action="store_true", help="run the repo-grounded pass/fail judge"
    )
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument(
        "--opencode-bin", default=str(Path.home() / ".opencode" / "bin" / "opencode")
    )
    parser.add_argument(
        "--opencode-db",
        default=str(Path.home() / ".local" / "share" / "opencode" / "opencode.db"),
    )
    parser.add_argument("--model", default="wayfinder/deepseek-v4-pro")
    parser.add_argument("--judge-model", default="openai/gpt-5.5")
    parser.add_argument("--judge-fallback-model", default="wayfinder/deepseek-v4-pro")
    parser.add_argument("--allow-judge-fallback", action="store_true")
    parser.add_argument("--timeout", type=int, default=900)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    output_dir = (
        REPO_ROOT / args.output_dir / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    ).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    jobs_eval = load_jobs_eval() if (args.live or args.judge) else None
    env = os.environ.copy()
    judge_model = args.judge_model
    if jobs_eval is not None:
        jobs_eval.resolve_wayfinder_model_env(args.model, env)
        if args.judge:
            judge_model = jobs_eval.resolve_judge_model(
                args.judge_model,
                fallback_model=args.judge_fallback_model,
                allow_fallback=args.allow_judge_fallback,
                env=env,
            )
            jobs_eval.resolve_wayfinder_model_env(judge_model, env)
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "live": args.live,
        "judge": args.judge,
        "cases": [],
    }
    for case in selected_cases(args.case, args.stage):
        result = run_case(
            case,
            live=args.live,
            judge=args.judge,
            output_dir=output_dir,
            jobs_eval=jobs_eval,
            opencode_bin=args.opencode_bin,
            model=args.model,
            judge_model=judge_model,
            timeout_seconds=args.timeout,
            env=env,
            db_path=Path(args.opencode_db),
        )
        report["cases"].append(result)
        print(
            f"{result['status']:6} {case.stage:12} {case.id}"
            + (
                ""
                if result["status"] == "passed"
                else f"  failed: {result['validator'].get('failed')}"
            )
        )
    report["finished_at"] = datetime.now(UTC).isoformat()
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    failed = [c["case_id"] for c in report["cases"] if c["status"] != "passed"]
    print(f"report: {output_dir / 'report.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
