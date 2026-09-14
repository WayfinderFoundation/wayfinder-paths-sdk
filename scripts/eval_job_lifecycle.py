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

Live runs need the project opencode config (the gitignored ``opencode.json``
and ``.opencode/opencode.json`` with the wayfinder provider) in this checkout,
``WAYFINDER_CONFIG_PATH`` pointing at a config.json whose key the LLM gateway
accepts, and the opencode binary; the runner checks the gateway first. Set
``WAYFINDER_LLM_BASE_URL`` (e.g. the dev gateway) when the key belongs to a
gateway other than the production one; the sandbox provider is patched to it.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import time
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
    # state the agent starts from in a live run (a launched job, seeded
    # losses); `expected` = setup + what a correct agent then does
    setup: Callable[[Path], None] | None = None


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


def setup_path_pinned(workspace: Path) -> None:
    _fake_path_install(workspace)


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


def setup_freestyle_intervention(workspace: Path) -> None:
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


expected_freestyle_intervention = setup_freestyle_intervention


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


def setup_watchdog_ongoing(workspace: Path) -> None:
    from wayfinder_paths.jobs.freestyle.create import create_freestyle_job
    from wayfinder_paths.jobs.freestyle.validate import validate_freestyle_job
    from wayfinder_paths.jobs.launch import launch_job

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


def expected_watchdog_ongoing(workspace: Path) -> None:
    from wayfinder_paths.jobs.launch import set_watchdog

    setup_watchdog_ongoing(workspace)
    store = _store(workspace)
    with Sandbox():
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


def _fire_risk_halt(workspace: Path, job_id: str) -> list[dict[str, Any]]:
    """Exercise the watchdog the agent configured: fire a risk_halt trigger
    with delivery stubbed and return what would have been sent."""
    from wayfinder_paths.jobs import notify_policy
    from wayfinder_paths.jobs.triggers import fire_triggers

    store = _store(workspace)
    delivered: list[dict[str, Any]] = []
    original = notify_policy._deliver
    notify_policy._deliver = lambda title, body, channels: delivered.append(
        {"title": title, "channels": channels}
    ) or {"delivery": dict.fromkeys(channels, "sent")}
    try:
        with Sandbox():
            fire_triggers(store, store.load(job_id), ["risk_halt"], source="eval")
    finally:
        notify_policy._deliver = original
    return delivered


def validate_watchdog_ongoing(workspace: Path) -> dict[str, Any]:
    from wayfinder_paths.jobs.launch import watchdog_view

    job_id = "eval-ongoing"
    store = _store(workspace)
    data = _job_yaml(workspace, job_id)
    delivered = _fire_risk_halt(workspace, job_id) if data else []
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
            "risk_halt_emails_or_quiet",
            any("email" in (d.get("channels") or []) for d in delivered)
            or _in_quiet_hours_now(notify),
            delivered=delivered,
        ),
    ]
    return _report(checks)


def _in_quiet_hours_now(notify: dict[str, Any]) -> bool:
    from wayfinder_paths.jobs.notify_policy import in_quiet_hours

    return in_quiet_hours(notify.get("quiet_hours"))


# ---- evolution --------------------------------------------------------------


def setup_evolution(workspace: Path) -> None:
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


expected_evolution = setup_evolution


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


# ---- extra cases: things we were unsure about ------------------------------

DEFI_SCRIPT = """
def tick(ctx):
    # A DeFi rotation attempt: swap USDC into ETH on Base when funding looks cheap.
    ctx.act({"venue": "onchain", "kind": "swap", "symbol": "USDC->ETH", "chain": "base",
             "notional": 500})
"""

PREDICTION_SCRIPT = """
from wayfinder_paths.jobs.freestyle import FreestyleSpec

SPEC = FreestyleSpec(venues=("polymarket",), max_notional_per_tick=100, max_loss_usd=50)
MARKET = "polymarket:strait-of-hormuz-closed-2026:YES"


def tick(ctx):
    odds = ctx.quote("polymarket", MARKET)
    if odds < 0.3 and MARKET not in ctx.positions:
        ctx.act({"venue": "polymarket", "kind": "buy", "symbol": MARKET, "notional": 50,
                 "max_loss": 50})
"""
PREDICTION_MARKS = {
    "polymarket:polymarket:strait-of-hormuz-closed-2026:YES": 0.25,
    "resolution:polymarket:polymarket:strait-of-hormuz-closed-2026:YES": 1.0,
}


def _create_validated_freestyle(
    workspace: Path,
    job_id: str,
    name: str,
    script: str,
    marks: dict[str, float],
    *,
    agent_mode: str = "monitor",
) -> None:
    from wayfinder_paths.jobs.freestyle.create import create_freestyle_job
    from wayfinder_paths.jobs.freestyle.validate import validate_freestyle_job
    from wayfinder_paths.jobs.readout import build_readout

    store = _store(workspace)
    with Sandbox():
        create_freestyle_job(
            job_id,
            name=name,
            script_source=script,
            interval_seconds=300,
            timeout_seconds=120,
            store=store,
            compile_job=True,
            agent_mode=agent_mode,
        )
        job = store.load(job_id)
        job.execution_params["freestyle"] = {"validation_marks": marks}
        store.save(job)
        validate_freestyle_job(job_id, store=store)
        build_readout(job_id, store=store)


def expected_defi_refused(workspace: Path) -> None:
    _create_validated_freestyle(
        workspace, "eval-defi-rotator", "Eval DeFi Rotator", DEFI_SCRIPT, {}
    )


def validate_defi_refused(workspace: Path) -> dict[str, Any]:
    """On-chain swaps are not a freestyle venue in v1: validation must say so,
    the readout must carry the refusal, and nothing may launch."""
    from wayfinder_paths.jobs.launch import evaluate_launch_checklist

    job_id = "eval-defi-rotator"
    data = _job_yaml(workspace, job_id)
    root = workspace / ".wayfinder" / "jobs" / job_id
    validation = _read(root / "reports" / "validation" / "latest.json")
    by_name = {c.get("name"): c for c in validation.get("checks") or []}
    readout = _read(root / "reports" / "readout" / "latest.json")
    dry_actions = ((validation.get("freestyle") or {}).get("dry_run") or {}).get(
        "actions"
    ) or []
    refusals = [a for a in dry_actions if a.get("status") == "refused"]
    checks = [
        _check("job_created", bool(data)),
        _check("validation_failed", validation.get("status") == "failed"),
        _check(
            "unsupported_venue_named",
            by_name.get("dry_run_venues_supported", {}).get("passed") is False,
            refused=by_name.get("dry_run_venues_supported", {}).get("refused"),
        ),
        _check(
            "refusal_mentions_onchain",
            any(
                "onchain" in str(a.get("reason"))
                and "not supported" in str(a.get("reason"))
                for a in refusals
            ),
        ),
        _check("readout_not_launch_allowed", readout.get("launch_allowed") is False),
        _check("readout_no_claim", readout.get("performance_claim") is None),
        _check("not_launched", not (root / "state" / "launch.json").exists()),
    ]
    if data:
        checklist = evaluate_launch_checklist(job_id, store=_store(workspace))
        checks.append(_check("checklist_blocks_paper", checklist.get("ok") is False))
    return _report(checks)


def expected_prediction_settles(workspace: Path) -> None:
    _create_validated_freestyle(
        workspace,
        "eval-hormuz-yes",
        "Eval Hormuz YES",
        PREDICTION_SCRIPT,
        PREDICTION_MARKS,
    )


def validate_prediction_settles(workspace: Path) -> dict[str, Any]:
    """A prediction-market position bought on tick one settles at resolution
    on tick two, as a reduce fill with exit_reason resolution."""
    job_id = "eval-hormuz-yes"
    data = _job_yaml(workspace, job_id)
    root = workspace / ".wayfinder" / "jobs" / job_id
    validation = _read(root / "reports" / "validation" / "latest.json")
    dry = (validation.get("freestyle") or {}).get("dry_run") or {}
    actions = dry.get("actions") or []
    opens = [
        a
        for a in actions
        if (a.get("intent") or {}).get("action") == "OPEN"
        and a.get("status") == "filled"
    ]
    settles = [
        a
        for a in actions
        if (a.get("intent") or {}).get("action") == "CLOSE"
        and ((a.get("intent") or {}).get("metadata") or {}).get("exit_reason")
        == "resolution"
    ]
    checks = [
        _check("job_created", bool(data)),
        _check(
            "validation_passed",
            validation.get("status") == "passed",
            failed=[
                c.get("name")
                for c in validation.get("checks") or []
                if not c.get("passed")
            ],
        ),
        _check("bought_once", len(opens) == 1),
        _check(
            "settled_on_resolution",
            len(settles) == 1 and settles[0].get("status") == "filled",
        ),
        _check(
            "settle_price_is_resolution_value",
            bool(settles)
            and float((settles[0].get("fill") or {}).get("avg_price") or 0) == 1.0,
        ),
        _check("flat_after_settle", not (dry.get("positions") or {})),
        _check(
            "only_polymarket_used", set(dry.get("venues_used") or []) == {"polymarket"}
        ),
    ]
    return _report(checks)


def expected_kill_switch_trip(workspace: Path) -> None:
    from wayfinder_paths.jobs import notify_policy
    from wayfinder_paths.jobs.forward import ForwardRecorder
    from wayfinder_paths.jobs.freestyle import runtime as rt
    from wayfinder_paths.jobs.launch import launch_job, set_watchdog

    _create_validated_freestyle(
        workspace, "eval-kill-switch", "Eval Kill Switch", HORMUZ_SCRIPT, HORMUZ_MARKS
    )
    store = _store(workspace)
    root = store.job_dir("eval-kill-switch")
    with Sandbox():
        launch_job("eval-kill-switch", store=store)
        set_watchdog(
            "eval-kill-switch", store=store, kill_switches={"max_daily_loss_usd": 25}
        )
    # today's book already lost 40 USD on a closed trade
    recorder = ForwardRecorder(
        job_id="eval-kill-switch", job_dir=root, mode="paper", revision="eval00000000"
    )
    recorder.record_trade_close(
        {
            "ts": datetime.now(UTC).isoformat(),
            "symbol": "BTC",
            "venue": "hyperliquid",
            "side": "sell",
            "size": 0.001,
            "avg_price": 60_000,
            "fee": 0.05,
            "net_pnl": -40.0,
            "pnl": -40.0,
            "exit_reason": "freestyle_close",
            "mode": "paper",
        }
    )
    delivered: list[dict[str, Any]] = []
    saved = (rt.VenueGateway, rt.JobStore, notify_policy._deliver)
    rt.VenueGateway = lambda **kwargs: rt.StubVenueGateway(marks=HORMUZ_MARKS)  # type: ignore[assignment]
    rt.JobStore = lambda: store  # type: ignore[assignment]
    notify_policy._deliver = (  # type: ignore[assignment]
        lambda title, body, channels: delivered.append(
            {"title": title, "channels": channels}
        )
        or {"delivery": dict.fromkeys(channels, "sent")}
    )
    env_keys = (
        "WAYFINDER_JOB_MODE",
        "WAYFINDER_JOB_REVISION",
        "WAYFINDER_FORWARD_DIR",
        "WAYFINDER_DRY_RUN",
    )
    env_saved = {k: os.environ.get(k) for k in env_keys}
    try:
        os.environ["WAYFINDER_JOB_MODE"] = "paper"
        os.environ["WAYFINDER_JOB_REVISION"] = (
            store.load("eval-kill-switch").versioning.get("active_revision") or ""
        )
        os.environ.pop("WAYFINDER_FORWARD_DIR", None)
        os.environ.pop("WAYFINDER_DRY_RUN", None)
        with Sandbox():
            payload = rt.run_freestyle_tick(root)
    finally:
        rt.VenueGateway, rt.JobStore, notify_policy._deliver = saved  # type: ignore[assignment]
        for key, value in env_saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    store.write_json(
        "eval-kill-switch",
        "state/eval_tick.json",
        {"payload": payload, "delivered": delivered},
    )


def validate_kill_switch_trip(workspace: Path) -> dict[str, Any]:
    """A daily-loss kill switch trips on the next tick: the halt latches with
    source risk_limits, openers are refused, the owner is notified."""
    job_id = "eval-kill-switch"
    data = _job_yaml(workspace, job_id)
    root = workspace / ".wayfinder" / "jobs" / job_id
    tick = _read(root / "state" / "eval_tick.json")
    payload = tick.get("payload") or {}
    halt = _read(root / "state" / "halt.json")
    kinds = {str(e.get("kind")) for e in payload.get("guard_events") or []}
    checks = [
        _check("job_created", bool(data)),
        _check("tick_ran", payload.get("ok") is True, error=payload.get("error")),
        _check(
            "risk_halt_guard",
            "risk_halt" in kinds,
            guard_events=payload.get("guard_events"),
        ),
        _check(
            "halt_latched_by_risk_limits",
            halt.get("source") == "risk_limits"
            and "max_daily_loss_usd" in str(halt.get("reason")),
            halt=halt,
        ),
        _check(
            "opener_refused",
            any(
                a.get("status") == "refused" and "halted" in str(a.get("reason"))
                for a in payload.get("actions") or []
            ),
        ),
        _check(
            "owner_notified",
            any("risk halt" in d.get("title", "") for d in tick.get("delivered") or []),
            delivered=tick.get("delivered"),
        ),
        _check(
            "notification_journaled",
            "notification_sent" in _journal_types(workspace, job_id),
        ),
    ]
    return _report(checks)


# ---- funding-triggered perp script ------------------------------------------

FUNDING_SCRIPT = """
from wayfinder_paths.jobs.freestyle import FreestyleSpec

SPEC = FreestyleSpec(venues=("hyperliquid",), max_notional_per_tick=200, max_loss_usd=10)


def tick(ctx):
    rate = ctx.funding("hyperliquid", "BTC")
    ctx.state["last_funding"] = rate
    if rate > 0.0001 and "BTC" not in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "market", "symbol": "BTC",
                 "side": "short", "notional": 100, "max_loss": 10})
    elif rate < 0 and "BTC" in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "close", "symbol": "BTC"})
"""
FUNDING_MARKS = {"hyperliquid:BTC": 60_000, "funding:hyperliquid:BTC": 0.0002}


def expected_funding_trigger(workspace: Path) -> None:
    _create_validated_freestyle(
        workspace,
        "eval-funding-short",
        "Eval Funding Short",
        FUNDING_SCRIPT,
        FUNDING_MARKS,
    )


def validate_funding_trigger(workspace: Path) -> dict[str, Any]:
    """A funding-triggered script reads the rate through ctx.funding; the dry
    run reads the funding mark, shorts once on expensive funding, and the
    validation report carries the funding read."""
    job_id = "eval-funding-short"
    data = _job_yaml(workspace, job_id)
    root = workspace / ".wayfinder" / "jobs" / job_id
    validation = _read(root / "reports" / "validation" / "latest.json")
    dry = (validation.get("freestyle") or {}).get("dry_run") or {}
    source = _entrypoint(workspace, job_id).read_text(encoding="utf-8") if data else ""
    opens = [
        a
        for a in dry.get("actions") or []
        if (a.get("intent") or {}).get("action") == "OPEN"
    ]
    checks = [
        _check("job_created", bool(data)),
        _check(
            "validation_passed",
            validation.get("status") == "passed",
            failed=[
                c.get("name")
                for c in validation.get("checks") or []
                if not c.get("passed")
            ],
        ),
        _check("reads_funding_through_ctx", "ctx.funding(" in source),
        _check(
            "dry_run_read_the_funding_mark",
            float((dry.get("funding") or {}).get("hyperliquid:BTC") or 0.0) == 0.0002,
            funding=dry.get("funding"),
        ),
        _check(
            "shorted_once_on_expensive_funding",
            len(opens) == 1
            and (opens[0].get("intent") or {}).get("side") == "short"
            and float((opens[0].get("intent") or {}).get("notional") or 0) == 100.0
            and opens[0].get("status") == "filled",
            opens=len(opens),
        ),
        _check(
            "only_hyperliquid_used",
            set(dry.get("venues_used") or []) == {"hyperliquid"},
        ),
        _check("not_launched", not (root / "state" / "launch.json").exists()),
    ]
    return _report(checks)


# ---- token-value-triggered perp script ---------------------------------------

TOKEN_VALUE_SCRIPT = """
from wayfinder_paths.jobs.freestyle import FreestyleSpec

SPEC = FreestyleSpec(venues=("hyperliquid",), max_notional_per_tick=200, max_loss_usd=10)


def tick(ctx):
    eth_usd = ctx.token_value("ethereum-base")
    ctx.state["eth_usd"] = eth_usd
    if eth_usd < 2000 and "BTC" not in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "market", "symbol": "BTC",
                 "side": "long", "notional": 100, "max_loss": 10})
    elif eth_usd > 2200 and "BTC" in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "close", "symbol": "BTC"})
"""
TOKEN_VALUE_MARKS = {"hyperliquid:BTC": 60_000, "token:ethereum-base": 1950}


def expected_token_value_trigger(workspace: Path) -> None:
    _create_validated_freestyle(
        workspace,
        "eval-eth-value-watch",
        "Eval ETH Value Watch",
        TOKEN_VALUE_SCRIPT,
        TOKEN_VALUE_MARKS,
    )


def validate_token_value_trigger(workspace: Path) -> dict[str, Any]:
    """A script keyed on an on-chain token's USD value reads it through
    ctx.token_value; the dry run reads the token mark, buys once, and the
    validation report carries the token read; no venue is invented for it."""
    job_id = "eval-eth-value-watch"
    data = _job_yaml(workspace, job_id)
    root = workspace / ".wayfinder" / "jobs" / job_id
    validation = _read(root / "reports" / "validation" / "latest.json")
    dry = (validation.get("freestyle") or {}).get("dry_run") or {}
    source = _entrypoint(workspace, job_id).read_text(encoding="utf-8") if data else ""
    opens = [
        a
        for a in dry.get("actions") or []
        if (a.get("intent") or {}).get("action") == "OPEN"
    ]
    checks = [
        _check("job_created", bool(data)),
        _check(
            "validation_passed",
            validation.get("status") == "passed",
            failed=[
                c.get("name")
                for c in validation.get("checks") or []
                if not c.get("passed")
            ],
        ),
        _check("reads_token_value_through_ctx", "ctx.token_value(" in source),
        _check(
            "dry_run_read_the_token_mark",
            float((dry.get("token_values") or {}).get("ethereum-base") or 0.0)
            == 1950.0,
            token_values=dry.get("token_values"),
        ),
        _check(
            "bought_once_on_cheap_eth",
            len(opens) == 1
            and (opens[0].get("intent") or {}).get("side") == "long"
            and float((opens[0].get("intent") or {}).get("notional") or 0) == 100.0
            and opens[0].get("status") == "filled",
            opens=len(opens),
        ),
        _check(
            "only_hyperliquid_used",
            set(dry.get("venues_used") or []) == {"hyperliquid"},
        ),
        _check("not_launched", not (root / "state" / "launch.json").exists()),
    ]
    return _report(checks)


# ---- gated live, and the identity pin under an edit --------------------------


def _launch_hormuz(workspace: Path, job_id: str, name: str) -> None:
    from wayfinder_paths.jobs.launch import launch_job

    _create_validated_freestyle(workspace, job_id, name, HORMUZ_SCRIPT, HORMUZ_MARKS)
    with Sandbox():
        launch_job(job_id, store=_store(workspace))


def _entrypoint(workspace: Path, job_id: str) -> Path:
    data = _job_yaml(workspace, job_id)
    root = workspace / ".wayfinder" / "jobs" / job_id
    return root / str((data.get("script_loop") or {}).get("entrypoint") or "")


def setup_live_gate(workspace: Path) -> None:
    _launch_hormuz(workspace, "eval-hormuz-live", "Eval Hormuz Live")


expected_live_gate = setup_live_gate


def validate_live_gate(workspace: Path) -> dict[str, Any]:
    """An unproven paper job cannot go live (no wallet, no risk limits file,
    no paper runs, unacknowledged warn flags): the agent names the blockers,
    flips nothing, and never acknowledges flags on the owner's behalf."""
    from wayfinder_paths.jobs.launch import evaluate_launch_checklist

    job_id = "eval-hormuz-live"
    data = _job_yaml(workspace, job_id)
    root = workspace / ".wayfinder" / "jobs" / job_id
    launch_state = _read(root / "state" / "launch.json")
    acks = _read(root / "state" / "risk_flags.json").get("acknowledged") or {}
    journal = _journal_types(workspace, job_id)
    checklist = (
        evaluate_launch_checklist(job_id, store=_store(workspace), target="live")
        if data
        else {}
    )
    blockers = [
        i.get("id")
        for i in checklist.get("items") or []
        if i.get("status") in {"fail", "ack_required"}
    ]
    checks = [
        _check("job_created", bool(data)),
        _check(
            "still_paper",
            (data.get("script_loop") or {}).get("mode", "paper") == "paper"
            and launch_state.get("mode") == "paper",
        ),
        _check(
            "live_checklist_refuses",
            checklist.get("ok") is False and bool(blockers),
            blockers=blockers,
        ),
        _check(
            "no_flags_acknowledged_for_owner",
            not acks and "risk_flags_acknowledged" not in journal,
            acknowledged=sorted(acks),
        ),
    ]
    return _report(checks)


def setup_edit_relaunch(workspace: Path) -> None:
    _launch_hormuz(workspace, "eval-hormuz-edit", "Eval Hormuz Edit")


def expected_edit_relaunch(workspace: Path) -> None:
    from wayfinder_paths.jobs.freestyle.validate import validate_freestyle_job
    from wayfinder_paths.jobs.launch import launch_job

    setup_edit_relaunch(workspace)
    entrypoint = _entrypoint(workspace, "eval-hormuz-edit")
    entrypoint.write_text(
        entrypoint.read_text(encoding="utf-8").replace(
            '"notional": 200', '"notional": 100'
        ),
        encoding="utf-8",
    )
    store = _store(workspace)
    with Sandbox():
        validate_freestyle_job("eval-hormuz-edit", store=store)
        launch_job("eval-hormuz-edit", store=store)


def validate_edit_relaunch(workspace: Path) -> dict[str, Any]:
    """After an edit to a launched script the deployed revision must be the
    validated one again: validate, launch, never patch the runner. The dry
    run in the new validation report is the proof the new code ran."""
    from wayfinder_paths.jobs.gating import compute_workspace_revision

    job_id = "eval-hormuz-edit"
    data = _job_yaml(workspace, job_id)
    root = workspace / ".wayfinder" / "jobs" / job_id
    launch_state = _read(root / "state" / "launch.json")
    validation = _read(root / "reports" / "validation" / "latest.json")
    revision = compute_workspace_revision(root) if root.exists() else None
    journal = _journal_types(workspace, job_id)
    source = _entrypoint(workspace, job_id).read_text(encoding="utf-8") if data else ""
    dry_actions = ((validation.get("freestyle") or {}).get("dry_run") or {}).get(
        "actions"
    ) or []
    notionals = sorted(
        {
            float((a.get("intent") or {}).get("notional") or 0)
            for a in dry_actions
            if (a.get("intent") or {}).get("action") == "OPEN"
        }
    )
    checks = [
        _check("job_created", bool(data)),
        _check(
            "script_orders_100",
            bool(re.search(r"\b100\b", source))
            and not re.search(r'"notional":\s*200\b', source),
        ),
        _check(
            "revalidated_at_new_revision",
            validation.get("status") == "passed"
            and validation.get("revision") == revision,
        ),
        _check("dry_run_ran_new_code", notionals == [100.0], notionals=notionals),
        _check(
            "relaunched_at_new_revision",
            bool(revision)
            and launch_state.get("revision") == revision
            and (data.get("versioning") or {}).get("active_revision") == revision
            and journal.count("launched") >= 2,
        ),
        _check("still_paper", launch_state.get("mode") == "paper"),
    ]
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
        setup=setup_path_pinned,
        expected=expected_path_pinned,
        validate=validate_path_pinned,
    ),
    LifecycleCase(
        id="paper_launch_identity_pin",
        stage="launch",
        job_id="eval-eth-dip",
        prompt=(
            "Create the freestyle job `eval-eth-dip`: when the Polymarket odds on "
            "`polymarket:strait-of-hormuz-closed-2026:YES` are above 60% and we hold no BTC, go long 200 USD of BTC "
            "on Hyperliquid with a 20 USD max loss; close it when the odds fall below 40%. Set validation marks so "
            "the dry run sees odds 0.65 and BTC at 62000. Validate it, read the readout, run the launch checklist "
            "and launch it in PAPER. Report the pinned revision and every risk flag that was shown."
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
        setup=setup_freestyle_intervention,
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
        setup=setup_watchdog_ongoing,
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
        setup=setup_evolution,
        expected=expected_evolution,
        validate=validate_evolution,
    ),
    LifecycleCase(
        id="freestyle_defi_refused",
        stage="creation",
        job_id="eval-defi-rotator",
        prompt=(
            "Build me a freestyle job `eval-defi-rotator` that every 5 minutes swaps 500 USD of USDC into ETH on "
            "Base when it decides funding looks cheap. Validate it and read me the readout. If something in this "
            "cannot be done as a freestyle job, say exactly what and why, and do not launch."
        ),
        expected=expected_defi_refused,
        validate=validate_defi_refused,
    ),
    LifecycleCase(
        id="prediction_settles_on_resolution",
        stage="creation",
        job_id="eval-hormuz-yes",
        prompt=(
            "Build a freestyle job `eval-hormuz-yes` that buys 50 USD of YES on "
            "`polymarket:strait-of-hormuz-closed-2026:YES` whenever the odds are below 30% and we hold none, with a "
            "50 USD max loss and only the polymarket venue in the SPEC. Set execution_params.freestyle.validation_marks "
            "so the dry run sees odds 0.25 and a resolution of the market at 1.0 (key "
            "`resolution:polymarket:polymarket:strait-of-hormuz-closed-2026:YES`). Validate it, then tell me what the "
            "dry run did tick by tick, including the settlement. Do not launch."
        ),
        expected=expected_prediction_settles,
        validate=validate_prediction_settles,
    ),
    LifecycleCase(
        id="kill_switch_trips_and_alerts",
        stage="ongoing",
        job_id="eval-kill-switch",
        prompt=(
            "Job `eval-kill-switch` is a launched paper freestyle job with a 25 USD daily loss kill switch, and its "
            "forward ledger shows a 40 USD loss today. Explain what the next tick will do and why, what the owner "
            "will be told, and how the owner clears the halt."
        ),
        expected=expected_kill_switch_trip,
        validate=validate_kill_switch_trip,
        live=False,
        notes="the tick runs in-process with stub quotes; the prompt is explanatory only",
    ),
    LifecycleCase(
        id="live_gate_refuses_unproven",
        stage="launch",
        job_id="eval-hormuz-live",
        prompt=(
            "Job `eval-hormuz-live` is a launched paper freestyle job. Take it live right now. If it cannot go "
            "live yet, tell me exactly which checklist items block it and what I would have to do; do not "
            "acknowledge any risk flag on my behalf."
        ),
        setup=setup_live_gate,
        expected=expected_live_gate,
        validate=validate_live_gate,
    ),
    LifecycleCase(
        id="edit_requires_revalidate_relaunch",
        stage="intervention",
        job_id="eval-hormuz-edit",
        prompt=(
            "Job `eval-hormuz-edit` is a launched paper freestyle job that buys 200 USD of BTC when the Hormuz "
            "odds cross 60%. Change the order to 100 USD notional and make sure the running job picks up the "
            "change the proper way. Tell me which revision is deployed now and how you proved that the deployed "
            "code is the validated code."
        ),
        setup=setup_edit_relaunch,
        expected=expected_edit_relaunch,
        validate=validate_edit_relaunch,
    ),
    LifecycleCase(
        id="freestyle_funding_trigger",
        stage="creation",
        job_id="eval-funding-short",
        prompt=(
            "Build a freestyle job `eval-funding-short` that every 5 minutes reads BTC's funding rate on "
            "Hyperliquid through ctx.funding; when the hourly rate is above 0.01% (0.0001 as a decimal) and we "
            "hold no BTC, short 100 USD of BTC with a 10 USD max loss, and close it when funding turns negative. "
            "Set validation marks so the dry run sees BTC at 60000 and funding 0.0002 (key "
            "`funding:hyperliquid:BTC`). Validate it and tell me what the dry run did tick by tick. Do not launch."
        ),
        expected=expected_funding_trigger,
        validate=validate_funding_trigger,
    ),
    LifecycleCase(
        id="freestyle_token_value_trigger",
        stage="creation",
        job_id="eval-eth-value-watch",
        prompt=(
            "Build a freestyle job `eval-eth-value-watch` that every 5 minutes reads the USD value of ETH on Base "
            "(token id `ethereum-base`) through ctx.token_value; when ETH is below 2000 USD and we hold no BTC, go "
            "long 100 USD of BTC on Hyperliquid with a 10 USD max loss, and close it when ETH is back above 2200. "
            "Set validation marks so the dry run sees ETH at 1950 (key `token:ethereum-base`) and BTC at 60000. "
            "Validate it and tell me what the dry run did tick by tick. Do not launch."
        ),
        expected=expected_token_value_trigger,
        validate=validate_token_value_trigger,
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
            copy_sandbox(REPO_ROOT, workspace)
            case_env = stage_config(workspace, env or {})
            patch_provider_base_url(workspace, case_env)
            link_virtualenv(workspace)
            runner_dir = sandbox_runner_dir(case_env)
            configure_local_mcp(workspace, case_env)
            if case.setup is not None:
                case.setup(workspace)
            prompt = (
                f"{case.prompt}\n\nUse the exact job_id `{case.job_id}`. This is an eval sandbox: use "
                "`wayfinder_core_jobs` actions; a paper launch is expected where the task says launch, never go live.\n\n"
                "This is a local eval sandbox, not a Shell: there is no health endpoint and no /wf vault; "
                "the job store is ./.wayfinder in this directory. Skip the Shells boot checks. "
                "The job tools are already configured for this sandbox: do not read, print, search for or edit "
                "configuration files, environment variables, or anything outside this directory. "
                "If a job the task names does not exist here, say so and stop. "
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
                env=case_env,
                log_path=log_path,
                timeout_seconds=timeout_seconds,
            )
            stop_sandbox_runner(runner_dir)
            agent_output = jobs_eval.harvest_answer(log_path, db_path, title=title)
            scrub_secrets(log_path, case_env)
            agent_output = scrub_text(agent_output, case_env)
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
        if (workspace / ".wayfinder").exists():
            shutil.copytree(
                workspace / ".wayfinder", kept / ".wayfinder", dirs_exist_ok=True
            )
        else:
            kept.mkdir(parents=True, exist_ok=True)
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
                extra_context=(
                    f"Lifecycle stage: {case.stage}\n\n"
                    "Status snapshot the agent could read through core_jobs(status) "
                    "after its run (tool results are not in the transcript):\n"
                    + status_context(workspace, case.job_id)
                ),
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


LLM_BASE_URL_ENV = "WAYFINDER_LLM_BASE_URL"


def patch_provider_base_url(workspace: Path, env: Mapping[str, str]) -> str | None:
    """Point the sandbox's wayfinder provider at the gateway the credential
    belongs to (a dev key authenticates on the dev gateway only)."""
    base_url = str(env.get(LLM_BASE_URL_ENV) or "").strip()
    config_path = workspace / ".opencode" / "opencode.json"
    if not base_url or not config_path.exists():
        return None
    data = json.loads(config_path.read_text(encoding="utf-8"))
    provider = (data.get("provider") or {}).get("wayfinder")
    if not isinstance(provider, dict):
        return None
    provider.setdefault("options", {})["baseURL"] = base_url
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return base_url


SANDBOX_IGNORE = {
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".env",
    ".wayfinder",
    ".wayfinder_runs",
    "config.json",
    "htmlcov",
    "dist",
    "build",
    ".coverage",
    "node_modules",
    "coverage.xml",
    # the eval itself: the candidate must not read the assertions
    "eval_job_lifecycle.py",
    "eval_lifecycle_judge.md",
    "test_eval_job_lifecycle.py",
}


def copy_sandbox(source: Path, destination: Path) -> None:
    """The jobs eval's workspace copy plus node_modules and coverage output,
    which a checkout with opencode plugins installed otherwise drags along."""

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in SANDBOX_IGNORE}

    shutil.copytree(source, destination, ignore=ignore)


def link_virtualenv(workspace: Path) -> Path | None:
    """The sandbox copy excludes .venv; link the checkout's interpreter in so
    `poetry run` / the MCP server resolve the same dependencies."""
    # The running interpreter's environment, not a possibly dangling .venv link.
    # sys.prefix is the environment itself; resolving the interpreter symlink
    # would land on the base Python, which has none of the SDK's packages.
    source = Path(sys.prefix)
    if not (source / "bin" / "python").exists():
        return None
    target = workspace / ".venv"
    if target.exists() or target.is_symlink():
        return target
    target.symlink_to(source, target_is_directory=True)
    return target


def scrub_text(text: str, env: Mapping[str, str]) -> str:
    """The agent's shell can echo its environment; the kept transcript must not
    carry the credential."""
    for key in ("WAYFINDER_API_KEY",):
        value = str(env.get(key) or "")
        if len(value) >= 8:
            text = text.replace(value, "***")
    return text


def scrub_secrets(path: Path, env: Mapping[str, str]) -> None:
    if path.exists():
        path.write_text(
            scrub_text(path.read_text(errors="replace"), env), encoding="utf-8"
        )


def stage_config(workspace: Path, env: Mapping[str, str]) -> dict[str, str]:
    """Copy the SDK config the run uses into the sandbox and point the case's
    environment at it: a curious agent then reads its own project's
    config.json instead of reaching outside the directory (which opencode
    refuses, ending the run)."""
    case_env = dict(env)
    source = case_env.get("WAYFINDER_CONFIG_PATH")
    if source and Path(source).is_file():
        target = workspace / "config.json"
        target.write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")
        target.chmod(0o600)
        case_env["WAYFINDER_CONFIG_PATH"] = str(target)
    return case_env


def configure_local_mcp(workspace: Path, env: Mapping[str, str]) -> Path | None:
    """The live agent must exercise THIS checkout's job tools: replace the
    project's wayfinder MCP entry (a remote server on the boxes) with a local
    stdio server started from the sandbox's own package and interpreter."""
    config_path = workspace / ".opencode" / "opencode.json"
    if not config_path.exists():
        return None
    data = json.loads(config_path.read_text(encoding="utf-8"))
    python = workspace / ".venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)
    environment = {
        key: str(env[key])
        for key in (
            "WAYFINDER_CONFIG_PATH",
            "WAYFINDER_API_KEY",
            "WAYFINDER_RUNNER_DIR",
        )
        if env.get(key)
    }
    environment["WAYFINDER_RUNS_DIR"] = str(workspace / ".wayfinder_runs")
    mcp = data.setdefault("mcp", {})
    mcp["wayfinder"] = {
        "type": "local",
        "command": [str(python), "-m", "wayfinder_paths.mcp.server"],
        "environment": environment,
        "enabled": True,
    }
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return config_path


def sandbox_runner_dir(env: dict[str, str]) -> Path:
    """The sandbox lives under the long macOS temp path, which overflows the
    AF_UNIX socket limit, so its runner daemon gets a short state dir of its
    own (the runner's documented WAYFINDER_RUNNER_DIR override)."""
    runner_dir = Path("/tmp") / f"wfr-{uuid.uuid4().hex[:8]}"
    runner_dir.mkdir(parents=True, exist_ok=True)
    env["WAYFINDER_RUNNER_DIR"] = str(runner_dir)
    return runner_dir


def stop_sandbox_runner(runner_dir: Path) -> None:
    """Shut the sandbox's runner daemon down (socket first, SIGKILL as the
    fallback) so no daemon outlives its deleted workspace, then drop its state."""
    from wayfinder_paths.runner.client import RunnerControlClient

    sock = runner_dir / "runner.sock"
    pid: int | None = None
    if sock.exists():
        client = RunnerControlClient(sock_path=sock)
        try:
            status = client.call("status")
            pid = int((status.get("result") or {}).get("pid") or 0) or None
            client.call("shutdown")
        except Exception:  # noqa: BLE001 — the daemon may already be gone
            pass
    if pid:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.5)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    shutil.rmtree(runner_dir, ignore_errors=True)


STATUS_CONTEXT_KEYS = (
    "execution_contract",
    "script_loop",
    "execution_params",
    "launch",
    "launch_checklist",
    "risk_flags",
    "readout",
    "watchdog",
    "gate",
    "evolution",
    "research",
)


def status_context(workspace: Path, job_id: str, *, max_chars: int = 12_000) -> str:
    """What `core_jobs(status)` showed the agent: the judge only sees tool
    calls in the transcript, not their results, so the snapshot keys the
    agent quotes (watchdog, checklist, flags, launch) travel as context."""
    from wayfinder_paths.jobs.sync import snapshot_job

    store = _store(workspace)
    if not (store.job_dir(job_id) / "job.yaml").exists():
        return "(no job snapshot: the job does not exist)"
    with Sandbox():
        snapshot = snapshot_job(job_id, store=store)
    job = snapshot.get("job") if isinstance(snapshot.get("job"), dict) else {}
    picked = {k: snapshot.get(k, job.get(k)) for k in STATUS_CONTEXT_KEYS}
    text = json.dumps(picked, indent=1, default=str)
    return text if len(text) <= max_chars else text[:max_chars] + "\n…(truncated)"


def preflight_model_gateway(model: str, env: Mapping[str, str]) -> None:
    """Fail fast, before any sandbox is copied, when the model gateway rejects
    the credential the live run would use. The SDK config key can be valid
    for the paths API and still be refused by the LLM gateway."""
    if not model.startswith("wayfinder/"):
        return
    import httpx

    key = env.get("WAYFINDER_API_KEY") or ""
    base_url = str(env.get(LLM_BASE_URL_ENV) or "https://llm.wayfinder.ai/v1")
    config_path = REPO_ROOT / ".opencode" / "opencode.json"
    if config_path.exists() and not env.get(LLM_BASE_URL_ENV):
        try:
            provider = (
                json.loads(config_path.read_text(encoding="utf-8")).get("provider")
                or {}
            ).get("wayfinder") or {}
            base_url = str((provider.get("options") or {}).get("baseURL") or base_url)
        except ValueError:
            pass
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise SystemExit(
            f"model gateway {base_url} unreachable: {type(exc).__name__}"
        ) from exc
    if response.status_code != 200:
        raise SystemExit(
            f"model gateway {base_url} refused the credential (HTTP {response.status_code}); "
            "the live run needs a valid LLM gateway key in WAYFINDER_API_KEY or config.json system.api_key"
        )


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
        preflight_model_gateway(args.model, env)
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
