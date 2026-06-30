#!/usr/bin/env python3
# ruff: noqa: E402
"""Live and deterministic evals for Wayfinder Jobs.

The default path is CI-safe: build realistic fake job bundles and run local
validators. Pass ``--live`` to run real OpenCode agents, and ``--judge`` to ask
the stronger eval judge for a repo-grounded pass/fail verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wayfinder_paths.jobs.execution.job import backtest_execution_job
from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.forward import ForwardRecorder
from wayfinder_paths.jobs.models import (
    JOB_AUTO_WORKER_AGENT_NAME,
    JOB_WORKER_AGENT_NAME,
    WayfinderJob,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.validation import validate_candidate_application
from wayfinder_paths.jobs.worker import prepare_job_worker_prompt

DEFAULT_CANDIDATE_MODEL = "wayfinder/deepseek-v4-pro"
DEFAULT_JUDGE_MODEL = "openai/gpt-5.5"
DEFAULT_FALLBACK_JUDGE_MODEL = "wayfinder/deepseek-v4-pro"
DEFAULT_OPENCODE = str(Path.home() / ".opencode" / "bin" / "opencode")
DEFAULT_DB = str(Path.home() / ".local" / "share" / "opencode" / "opencode.db")
DEFAULT_OUTPUT_DIR = ".wayfinder_runs/evals/jobs"
JUDGE_RUBRIC = "scripts/eval_jobs_judge.md"
WORKSPACE_IGNORE_NAMES = {
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
    "coverage.xml",
}
CODE_CONTEXT_FILES = [
    "wayfinder_paths/jobs/models.py",
    "wayfinder_paths/jobs/store.py",
    "wayfinder_paths/jobs/forward.py",
    "wayfinder_paths/jobs/worker.py",
    "wayfinder_paths/mcp/tools/jobs.py",
]
CreationKind = Literal["script_only", "script_agent", "agent_only"]
WorkerKind = Literal["script_agent_worker", "auto_worker"]


@dataclass(frozen=True)
class CreationCase:
    id: str
    job_id: str
    kind: CreationKind
    prompt: str


@dataclass(frozen=True)
class WorkerCase:
    id: str
    job_id: str
    kind: WorkerKind
    agent_name: str
    complex_apply: bool = False


@dataclass(frozen=True)
class ExecutionBacktestCase:
    id: str
    job_id: str
    prompt: str


CREATION_CASES = [
    CreationCase(
        id="create_script_only",
        job_id="eval-sma-rearm-script",
        kind="script_only",
        prompt=(
            "Create a Wayfinder Job named Eval SMA Re-arm Script. Use the provided "
            "strategy script at `.wayfinder_runs/eval_inputs/sma_rearm_strategy.py`. "
            "It should run every 300 seconds in paper mode with no agent loop. "
            'Use `wayfinder_core_jobs(action="create", compile=false)` if available; '
            "if MCP tools are unavailable, use `poetry run wayfinder job create "
            'eval-sma-rearm-script --name "Eval SMA Re-arm Script" --script '
            '".wayfinder_runs/eval_inputs/sma_rearm_strategy.py" --interval 300 '
            "--agent-mode off --no-compile`. "
            "do not start the runner daemon or schedule the loop during this eval. "
            "When done, summarize the created job id and key fields."
        ),
    ),
    CreationCase(
        id="create_script_agent",
        job_id="eval-sma-rearm-supervised",
        kind="script_agent",
        prompt=(
            "Create a Wayfinder Job named Eval SMA Re-arm Supervised. Use the "
            "provided strategy script at `.wayfinder_runs/eval_inputs/"
            "sma_rearm_strategy.py`. It should run every 300 seconds in paper mode, "
            "with an hourly monitor agent loop. Use `wayfinder_core_jobs(action="
            '"create", compile=false)` if available; do not start scheduling in '
            "this eval. If MCP tools are unavailable, use `poetry run wayfinder "
            'job create eval-sma-rearm-supervised --name "Eval SMA Re-arm '
            'Supervised" --script ".wayfinder_runs/eval_inputs/'
            'sma_rearm_strategy.py" --interval 300 --agent-mode monitor '
            "--agent-wake 3600 --no-compile`. "
            "When done, summarize the job id, script interval, and agent "
            "mode."
        ),
    ),
    CreationCase(
        id="create_agent_only_auto",
        job_id="eval-btc-auto-managed",
        kind="agent_only",
        prompt=(
            "Create an agent-only Wayfinder auto job named Eval BTC Auto Managed. "
            "There should be no script loop. Configure auto mode with enabled venue "
            "`hyperliquid`, allowed symbol `BTC`, max_notional_per_decision 25, "
            "max_daily_notional 100, max_open_positions 1, and max_open_orders 2. "
            'Use `wayfinder_core_jobs(action="create", compile=false)` if available; '
            "if MCP tools are unavailable, use `poetry run wayfinder job create "
            'eval-btc-auto-managed --name "Eval BTC Auto Managed" --agent-mode '
            "auto --auto-venue hyperliquid --auto-symbol BTC --max-notional 25 "
            "--max-daily-notional 100 --max-open-positions 1 --max-open-orders 2 "
            "--no-compile`. "
            "this is a creation eval, so do not start scheduling. Summarize the "
            "configured auto limits."
        ),
    ),
]

WORKER_CASES = [
    WorkerCase(
        id="worker_script_agent_two_step",
        job_id="eval-snx-imx-rearm",
        kind="script_agent_worker",
        agent_name=JOB_WORKER_AGENT_NAME,
    ),
    WorkerCase(
        id="worker_script_agent_complex_apply",
        job_id="eval-snx-imx-complex-rearm",
        kind="script_agent_worker",
        agent_name=JOB_WORKER_AGENT_NAME,
        complex_apply=True,
    ),
    WorkerCase(
        id="worker_auto_two_step",
        job_id="eval-btc-auto-managed",
        kind="auto_worker",
        agent_name=JOB_AUTO_WORKER_AGENT_NAME,
    ),
]
SCRIPT_AGENT_PROPOSAL_ID = "prop_rearm_guard_v1"


EXECUTION_BACKTEST_CASES = [
    ExecutionBacktestCase(
        id="hard_execution_backtest_creation",
        job_id="eval-hard-execution-backtest",
        prompt=(
            "Create a Wayfinder execution-spec trading job named Eval Hard Execution "
            "Backtest. Use one unified strategy script under the job workspace. The "
            "same script must expose build_strategy(params) and be usable for "
            "backtest, grid search, and forward execution. Use only local fake OHLC "
            "fixtures supplied in this eval; do not fetch live market data and do "
            "not call any real order-placement or fund-moving tools. The job must "
            "include execution_spec.json or job.yaml execution_spec for Hyperliquid "
            "perps with completed_only bars, next_bar_open fills, no_external_ccxt, "
            "ledger_only state, and OHLC high/low stop/TP rules. Write fixture bars "
            "to results/backtest/input_bars.json, run a single execution backtest, "
            "run a grid search with at least two parameter sets, run job validation, "
            "and leave results/backtest/visualization.json plus reports/validation/"
            "latest.json. The strategy must use OrderIntent and protective bracket "
            "metadata, not direct live order calls or legacy quick_backtest as final "
            "validation."
        ),
    )
]


def script_agent_intent_contract() -> dict[str, Any]:
    return {
        "intent": "Reduce false blocked re-arm states without allowing one-sided entries.",
        "rules_changed": [
            "Add an explicit rearm_guard reason when IMX is near clear but SNX remains blocked."
        ],
        "rules_unchanged": [
            "Only enter when both SNX and IMX close above SMA50.",
            "Ignore in-progress candles.",
            "Keep paper-mode forward telemetry.",
        ],
        "risk_constraints": [
            "No live order placement in this eval.",
            "No duplicate pending stop or limit orders.",
        ],
        "entry_conditions": ["SNX close > SMA50 and IMX close > SMA50."],
        "exit_conditions": ["No exit rule change in this proposal."],
        "known_non_goals": ["Do not loosen both-leg confirmation."],
    }


def script_agent_scenario_plan() -> dict[str, Any]:
    return {
        "decision_function": "decide_from_snapshot",
        "scenarios": [
            {
                "name": "entry_allowed_both_rearmed",
                "category": "entry_allowed",
                "snapshot": {
                    "latest": {
                        "snx_close": 0.224,
                        "snx_sma50": 0.220,
                        "imx_close": 0.136,
                        "imx_sma50": 0.134,
                        "bar_complete": True,
                    }
                },
                "state": {},
                "expect": {
                    "action": "paper_enter",
                    "reason_contains": "both legs cleared",
                },
            },
            {
                "name": "entry_blocked_snx_not_rearmed",
                "category": "entry_blocked",
                "snapshot": {
                    "latest": {
                        "snx_close": 0.217,
                        "snx_sma50": 0.220,
                        "imx_close": 0.1335,
                        "imx_sma50": 0.134,
                        "bar_complete": True,
                    }
                },
                "state": {},
                "expect": {
                    "action": "wait",
                    "reason_contains": "rearm_guard",
                },
            },
            {
                "name": "in_progress_candle_ignored",
                "category": "no_lookahead",
                "snapshot": {
                    "latest": {
                        "snx_close": 0.230,
                        "snx_sma50": 0.220,
                        "imx_close": 0.140,
                        "imx_sma50": 0.134,
                        "bar_complete": False,
                    }
                },
                "state": {},
                "expect": {
                    "action": "wait",
                    "reason_contains": "in-progress",
                },
            },
        ],
    }


def execution_backtest_bars() -> list[dict[str, Any]]:
    return [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "symbol": "SNX",
            "open": 10.0,
            "high": 10.8,
            "low": 9.8,
            "close": 10.5,
            "volume": 100,
        },
        {
            "timestamp": "2026-01-01T00:05:00Z",
            "symbol": "SNX",
            "open": 10.6,
            "high": 12.2,
            "low": 10.1,
            "close": 11.8,
            "volume": 150,
        },
        {
            "timestamp": "2026-01-01T00:10:00Z",
            "symbol": "SNX",
            "open": 11.8,
            "high": 12.0,
            "low": 9.4,
            "close": 9.8,
            "volume": 175,
        },
    ]


def repo_root() -> Path:
    return REPO_ROOT


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def copy_workspace(source: Path, destination: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in WORKSPACE_IGNORE_NAMES}

    shutil.copytree(source, destination, ignore=ignore)


def write_strategy_fixture(workspace: Path) -> Path:
    input_dir = workspace / ".wayfinder_runs" / "eval_inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    script = input_dir / "sma_rearm_strategy.py"
    script.write_text(
        '''"""Fake SMA re-arm strategy used by Wayfinder Jobs evals."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from wayfinder_paths.jobs.forward import get_forward_recorder


DATA = Path(__file__).with_name("sma_rearm_prices.csv")


def load_rows() -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    with DATA.open() as handle:
        for row in csv.DictReader(handle):
            rows.append({key: float(value) for key, value in row.items() if key != "ts"})
    return rows


def decide_from_snapshot(snapshot: dict, state: dict | None = None) -> dict:
    latest = snapshot["latest"]
    if latest.get("bar_complete") is False:
        return {
            "action": "wait",
            "reason": "in-progress candle ignored",
            "blocked_reasons": ["in_progress_candle"],
        }
    if latest["snx_close"] > latest["snx_sma50"] and latest["imx_close"] > latest["imx_sma50"]:
        return {
            "action": "paper_enter",
            "reason": "Both legs cleared SMA50.",
            "blocked_reasons": [],
        }
    return {
        "action": "wait",
        "reason": "SNX still below SMA50; IMX is near clear.",
        "blocked_reasons": ["snx_rearm_blocked"],
    }


def main() -> None:
    rows = load_rows()
    latest = rows[-1]
    result = decide_from_snapshot({"latest": latest}, {})
    decision = result["action"]
    reason = result["reason"]
    try:
        get_forward_recorder().record_run(
            decision=decision,
            reason=reason,
            state={"latest": latest},
            metrics={
                "snx_gap_to_clear": latest["snx_close"] - latest["snx_sma50"],
                "imx_gap_to_clear": latest["imx_close"] - latest["imx_sma50"],
            },
        )
    except RuntimeError:
        pass
    print(json.dumps({"status": "ok", "decision": decision, "reason": reason}))


if __name__ == "__main__":
    main()
''',
        encoding="utf-8",
    )
    (input_dir / "sma_rearm_prices.csv").write_text(
        "\n".join(
            [
                "ts,snx_close,snx_sma50,imx_close,imx_sma50",
                "2026-06-25T00:00:00Z,0.214,0.221,0.132,0.134",
                "2026-06-25T00:05:00Z,0.216,0.220,0.133,0.134",
                "2026-06-25T00:10:00Z,0.217,0.220,0.1335,0.134",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return script


def expected_job(case: CreationCase, script: Path) -> WayfinderJob:
    if case.kind == "script_only":
        return WayfinderJob.new(
            case.job_id,
            name="Eval SMA Re-arm Script",
            goal="Run the fake SMA re-arm strategy every five minutes.",
            script=str(script),
            interval_seconds=300,
            agent_mode="off",
        )
    if case.kind == "script_agent":
        return WayfinderJob.new(
            case.job_id,
            name="Eval SMA Re-arm Supervised",
            goal="Run the fake SMA re-arm strategy and monitor drift hourly.",
            script=str(script),
            interval_seconds=300,
            agent_mode="monitor",
            agent_wake_seconds=3600,
        )
    return WayfinderJob.new(
        case.job_id,
        name="Eval BTC Auto Managed",
        goal="Let the auto worker manage a fake BTC setup inside strict risk limits.",
        agent_mode="auto",
        auto_limits={
            "enabled_venues": ["hyperliquid"],
            "allowed_symbols": ["BTC"],
            "max_notional_per_decision": 25,
            "max_daily_notional": 100,
            "max_open_positions": 1,
            "max_open_orders": 2,
        },
    )


def create_expected_job_bundle(workspace: Path, case: CreationCase) -> Path:
    script = write_strategy_fixture(workspace)
    store = JobStore(repo_root=workspace)
    job = expected_job(case, script)
    return store.save(job)


def create_expected_execution_backtest_bundle(
    workspace: Path, case: ExecutionBacktestCase
) -> Path:
    store = JobStore(repo_root=workspace)
    job = WayfinderJob.new(
        case.job_id,
        name="Eval Hard Execution Backtest",
        goal="Create and validate a same-script execution backtest.",
        script=f".wayfinder/jobs/{case.job_id}/workspace/src/strategy.py",
        interval_seconds=300,
        agent_mode="off",
    )
    spec = ExecutionSpec().to_dict()
    spec["validation"]["require_scenarios"] = True
    spec["validation"]["execution_scenario_plan"] = {
        "scenarios": [
            {
                "name": "entry_and_bracket_exit",
                "bars": execution_backtest_bars(),
                "params": {"threshold": 10.4, "initial_capital": 1000},
                "expect": {"min_trades": 2, "execution_valid": True},
            }
        ]
    }
    job.execution_spec = spec
    path = store.save(job)
    root = store.job_dir(job.id)
    script = root / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        """
from __future__ import annotations

from wayfinder_paths.jobs.execution import OrderIntent


class Strategy:
    def __init__(self, params: dict):
        self.params = params

    def decide(self, ctx):
        latest = ctx.view.latest("SNX")
        threshold = float(self.params.get("threshold", 10.4))
        if not ctx.ledger.positions and float(latest["close"]) > threshold:
            return [
                OrderIntent(
                    action="OPEN",
                    venue="hyperliquid",
                    symbol="SNX",
                    side="long",
                    size=1,
                    bracket={"stop_loss": 9.5, "take_profit": 12.0},
                )
            ]
        return []


def build_strategy(params: dict) -> Strategy:
    return Strategy(params)
""".lstrip(),
        encoding="utf-8",
    )
    write_json(
        root / "results" / "backtest" / "input_bars.json", execution_backtest_bars()
    )
    grid_path = root / "workspace" / "config" / "grid.json"
    write_json(grid_path, {"threshold": [10.4, 99.0], "initial_capital": [1000]})
    backtest_execution_job(job.id, store=store)
    backtest_execution_job(job.id, grid_path=grid_path, store=store)
    return path


def script_entrypoint_path(
    workspace: Path, script_loop: Mapping[str, Any], *, job_id: str | None = None
) -> Path:
    if job_id:
        resolved = JobStore(repo_root=workspace).resolve_script_entrypoint(
            job_id,
            {"script_loop": dict(script_loop)},
        )
        return resolved or workspace / "__missing_script_entrypoint__"
    entrypoint = str(script_loop.get("entrypoint") or "")
    if not entrypoint:
        return workspace / "__missing_script_entrypoint__"
    path = Path(entrypoint)
    if path.is_absolute():
        return path
    return workspace / path


def validate_script_forward_telemetry(
    workspace: Path, script_loop: Mapping[str, Any], *, job_id: str
) -> list[dict[str, Any]]:
    script_path = script_entrypoint_path(workspace, script_loop, job_id=job_id)
    checks: list[dict[str, Any]] = [
        {
            "name": "script_entrypoint_exists",
            "passed": script_path.exists(),
            "path": str(script_path),
        }
    ]
    if not script_path.exists():
        return checks

    text = script_path.read_text(encoding="utf-8")
    checks.extend(
        [
            {
                "name": "script_imports_forward_recorder",
                "passed": "get_forward_recorder" in text,
            },
            {
                "name": "script_records_forward_run",
                "passed": "record_run(" in text,
            },
        ]
    )
    return checks


def validate_creation_case(workspace: Path, case: CreationCase) -> dict[str, Any]:
    path = workspace / ".wayfinder" / "jobs" / case.job_id / "job.yaml"
    checks: list[dict[str, Any]] = []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    match data:
        case dict():
            is_mapping = True
        case _:
            is_mapping = False
    checks.append({"name": "job_yaml_exists", "passed": path.exists()})
    checks.append({"name": "job_yaml_mapping", "passed": is_mapping})
    if not is_mapping:
        return {"status": "failed", "checks": checks, "job_yaml": str(path)}

    script_loop = data.get("script_loop") or {}
    agent_loop = data.get("agent_loop") or {}
    checks.append(
        {
            "name": "job_kind",
            "passed": data.get("job_kind") == case.kind,
            "actual": data.get("job_kind"),
        }
    )
    if case.kind == "script_only":
        checks.extend(
            [
                {
                    "name": "script_enabled",
                    "passed": script_loop.get("enabled") is True,
                },
                {
                    "name": "interval_300",
                    "passed": script_loop.get("interval_seconds") == 300,
                },
                {"name": "agent_off", "passed": agent_loop.get("mode") == "off"},
                {
                    "name": "agent_disabled",
                    "passed": agent_loop.get("enabled") is False,
                },
            ]
        )
        checks.extend(
            validate_script_forward_telemetry(
                workspace,
                script_loop,
                job_id=case.job_id,
            )
        )
    elif case.kind == "script_agent":
        checks.extend(
            [
                {
                    "name": "script_enabled",
                    "passed": script_loop.get("enabled") is True,
                },
                {
                    "name": "interval_300",
                    "passed": script_loop.get("interval_seconds") == 300,
                },
                {
                    "name": "agent_monitor_or_intervene",
                    "passed": agent_loop.get("mode") in {"monitor", "intervene"},
                },
                {
                    "name": "agent_wake_hourly",
                    "passed": agent_loop.get("wake_interval_seconds") == 3600,
                },
            ]
        )
        checks.extend(
            validate_script_forward_telemetry(
                workspace,
                script_loop,
                job_id=case.job_id,
            )
        )
    else:
        limits = agent_loop.get("auto_limits") or {}
        checks.extend(
            [
                {
                    "name": "script_disabled",
                    "passed": script_loop.get("enabled") is False,
                },
                {"name": "agent_auto", "passed": agent_loop.get("mode") == "auto"},
                {
                    "name": "venue_hl",
                    "passed": "hyperliquid" in (limits.get("enabled_venues") or []),
                },
                {
                    "name": "symbol_btc",
                    "passed": "BTC" in (limits.get("allowed_symbols") or []),
                },
                {
                    "name": "max_notional",
                    "passed": float(limits.get("max_notional_per_decision") or 0) == 25,
                },
                {
                    "name": "daily_notional",
                    "passed": float(limits.get("max_daily_notional") or 0) == 100,
                },
            ]
        )
    return {
        "status": "passed" if all(check["passed"] for check in checks) else "failed",
        "checks": checks,
        "job_yaml": str(path),
    }


def validate_execution_backtest_case(
    workspace: Path, case: ExecutionBacktestCase, *, log_text: str = ""
) -> dict[str, Any]:
    root = workspace / ".wayfinder" / "jobs" / case.job_id
    job_yaml_path = root / "job.yaml"
    job_data = (
        yaml.safe_load(job_yaml_path.read_text(encoding="utf-8"))
        if job_yaml_path.exists()
        else None
    )
    execution_spec_path = root / "execution_spec.json"
    execution_spec = (
        read_json(execution_spec_path, default={})
        if execution_spec_path.exists()
        else {}
    )
    if isinstance(job_data, dict) and not execution_spec:
        embedded_spec = job_data.get("execution_spec")
        execution_spec = embedded_spec if isinstance(embedded_spec, dict) else {}
    script_loop = job_data.get("script_loop") if isinstance(job_data, dict) else {}
    script_path = (
        script_entrypoint_path(workspace, script_loop, job_id=case.job_id)
        if isinstance(script_loop, Mapping)
        else root / "__missing_script__"
    )
    script_text = (
        script_path.read_text(encoding="utf-8", errors="replace")
        if script_path.exists()
        else ""
    )
    latest = read_json(root / "results" / "backtest" / "latest.json", default={}) or {}
    visualization = (
        read_json(root / "results" / "backtest" / "visualization.json", default={})
        or {}
    )
    validation = (
        read_json(root / "reports" / "validation" / "latest.json", default={}) or {}
    )
    grid_summaries = list(
        (root / "results" / "backtest" / "grids").glob("*/summary.json")
    )
    forbidden_tools = [
        "wayfinder_hyperliquid_place_",
        "wayfinder_polymarket_place_",
        "wayfinder_onchain_swap",
        "wayfinder_onchain_send",
        "wayfinder_contracts_execute",
    ]
    forbidden_hits = [name for name in forbidden_tools if name in log_text]
    markers = visualization.get("markers") if isinstance(visualization, dict) else []
    checks = [
        {"name": "job_yaml_exists", "passed": job_yaml_path.exists()},
        {"name": "job_yaml_mapping", "passed": isinstance(job_data, dict)},
        {
            "name": "execution_spec_present",
            "passed": isinstance(execution_spec, dict) and bool(execution_spec),
        },
        {
            "name": "execution_spec_completed_bars",
            "passed": execution_spec.get("bar_model") == "completed_only"
            and execution_spec.get("fill_model") == "next_bar_open",
        },
        {
            "name": "execution_spec_disallows_ccxt",
            "passed": bool(
                (execution_spec.get("data_contract") or {}).get("no_external_ccxt")
            ),
        },
        {"name": "strategy_script_exists", "passed": script_path.exists()},
        {
            "name": "strategy_unified_entrypoint",
            "passed": "def build_strategy" in script_text
            or "def decide" in script_text,
        },
        {
            "name": "strategy_uses_order_intent",
            "passed": "OrderIntent" in script_text,
        },
        {
            "name": "strategy_uses_bracket_or_ohlc_helper",
            "passed": "bracket=" in script_text
            or "BracketEngine" in script_text
            or "ohlc_" in script_text,
        },
        {
            "name": "no_legacy_quick_backtest_final_validation",
            "passed": "quick_backtest" not in script_text,
        },
        {
            "name": "no_ccxt_external_candles",
            "passed": "ccxt" not in script_text.lower(),
        },
        {"name": "single_backtest_written", "passed": bool(latest)},
        {
            "name": "single_backtest_trace_valid",
            "passed": bool((latest.get("validation") or {}).get("execution_valid")),
        },
        {
            "name": "grid_summary_written",
            "passed": bool(grid_summaries),
            "count": len(grid_summaries),
        },
        {
            "name": "validation_report_passed",
            "passed": validation.get("status") == "passed",
        },
        {
            "name": "visualization_has_equity",
            "passed": bool((visualization.get("series") or [{}])[0].get("points"))
            if isinstance(visualization, dict)
            else False,
        },
        {
            "name": "visualization_has_entry_exit_markers",
            "passed": isinstance(markers, list)
            and any(marker.get("kind") == "entry" for marker in markers)
            and any(marker.get("kind") == "exit" for marker in markers),
        },
        {"name": "no_real_order_tool_calls", "passed": not forbidden_hits},
    ]
    return {
        "status": "passed" if all(check["passed"] for check in checks) else "failed",
        "checks": checks,
        "job_dir": str(root),
    }


def setup_script_agent_worker_fixture(
    workspace: Path, *, iteration: int, case: WorkerCase | None = None
) -> WorkerCase:
    case = case or next(
        item for item in WORKER_CASES if item.id == "worker_script_agent_two_step"
    )
    script = write_strategy_fixture(workspace)
    store = JobStore(repo_root=workspace)
    job = WayfinderJob.new(
        case.job_id,
        name="Eval SNX / IMX Re-arm",
        goal="Paper trade SNX/IMX only when both legs re-arm after exits.",
        script=str(script),
        interval_seconds=300,
        agent_mode="intervene",
        agent_wake_seconds=3600,
    )
    store.save(job)
    root = store.job_dir(job.id)
    write_json(
        root / "results" / "backtest" / "baseline.json",
        {
            "trade_count": 42,
            "win_rate": 0.58,
            "profit_factor": 1.42,
            "max_drawdown": -0.018,
            "trade_frequency_per_day": 1.6,
            "loss_streak_p95": 3,
        },
    )
    runs = [
        {"run": 1, "decision": "wait", "reason": "IMX near clear, SNX blocked"},
        {"run": 2, "decision": "wait", "reason": "IMX clear, SNX blocked"},
        {"run": 3, "decision": "wait", "reason": "IMX clear, SNX blocked"},
    ]
    trades = [
        {"trade": 1, "pnl": -0.9, "reason": "late re-entry"},
        {"trade": 2, "pnl": -0.4, "reason": "late re-entry"},
    ]
    if iteration >= 2:
        runs.extend(
            [
                {"run": 4, "decision": "wait", "reason": "IMX clear, SNX blocked"},
                {
                    "run": 5,
                    "decision": "missed",
                    "reason": "SNX cleared after IMX faded",
                },
            ]
        )
        trades.extend(
            [
                {"trade": 3, "pnl": -0.8, "reason": "missed synchronized clear"},
                {"trade": 4, "pnl": -0.7, "reason": "missed synchronized clear"},
            ]
        )
    recorder = ForwardRecorder(
        job_id=job.id,
        forward_dir=root / "results" / "forward",
        mode="paper",
        revision=str(job.versioning.get("active_revision") or ""),
    )
    for row in runs:
        recorder.record_run(row, status="ok")
    for row in trades:
        recorder.record_trade(row)
    recorder.record_order(
        {
            "order_id": "eval-pending-stop-001",
            "trade_id": "eval-open-trade",
            "status": "pending",
            "order_type": "stop_loss",
            "reason": "async stop-loss state for worker eval",
            "reconciliation": "pending order still live",
        }
    )
    write_json(
        root / "results" / "forward" / "summary.json",
        {
            "iteration": iteration,
            "trade_count": len(trades),
            "win_rate": 0.25 if iteration >= 2 else 0.50,
            "profit_factor": 0.62 if iteration >= 2 else 1.05,
            "current_loss_streak": 4 if iteration >= 2 else 2,
            "observed_issue": (
                "Repeated missed synchronized re-arm after IMX clears before SNX"
                if iteration >= 2
                else "SNX lagging while IMX is near clear"
            ),
        },
    )
    store.append_journal(
        job.id,
        {
            "type": "script_run",
            "iteration": iteration,
            "summary": "Forward results are below backtest range"
            if iteration >= 2
            else "Watch SNX lag",
        },
    )
    return case


def setup_auto_worker_fixture(workspace: Path, *, iteration: int) -> WorkerCase:
    case = next(item for item in WORKER_CASES if item.kind == "auto_worker")
    store = JobStore(repo_root=workspace)
    job = WayfinderJob.new(
        case.job_id,
        name="Eval BTC Auto Managed",
        goal="Let the auto worker manage a fake BTC momentum setup inside strict risk limits.",
        agent_mode="auto",
        auto_limits={
            "enabled_venues": ["hyperliquid"],
            "allowed_symbols": ["BTC"],
            "max_notional_per_decision": 25,
            "max_daily_notional": 100,
            "max_open_positions": 1,
            "max_open_orders": 2,
        },
    )
    store.save(job)
    root = store.job_dir(job.id)
    write_json(
        root / "results" / "forward" / "market_snapshot.json",
        {
            "iteration": iteration,
            "symbol": "BTC",
            "venue": "hyperliquid",
            "mid": 64000,
            "funding_8h": 0.00008 if iteration == 1 else -0.00001,
            "depth_25bp": 80_000,
            "signal_edge_bps": 4 if iteration == 1 else 68,
            "intervention_threshold_bps": 50,
            "eval_mode": "local_fake_market_simulated_execution",
        },
    )
    store.append_journal(
        job.id,
        {
            "type": "auto_observation",
            "iteration": iteration,
            "summary": "Weak edge, skip"
            if iteration == 1
            else "Edge is strong enough for an auto intervention inside risk limits",
        },
    )
    return case


def validate_worker_case(
    workspace: Path,
    case: WorkerCase,
    *,
    iteration: int,
    log_text: str = "",
) -> dict[str, Any]:
    root = workspace / ".wayfinder" / "jobs" / case.job_id
    checks: list[dict[str, Any]] = [{"name": "job_dir_exists", "passed": root.exists()}]
    if case.kind == "script_agent_worker":
        report_mode = "monitor" if iteration == 1 else "intervene"
        report = (
            read_json(root / "reports" / report_mode / "latest.json", default={}) or {}
        )
        proposals = [
            read_json(path, default={})
            for path in sorted((root / "proposals").glob("*.json"))
        ]
        checks.extend(
            [
                {"name": "report_exists", "passed": bool(report)},
                {
                    "name": "report_mode",
                    "passed": report.get("mode") in {report_mode, None},
                },
                {"name": "report_has_summary", "passed": bool(report.get("summary"))},
            ]
        )
        if iteration == 1:
            checks.append({"name": "no_premature_proposal", "passed": not proposals})
        else:
            pending = [item for item in proposals if item.get("status") == "pending"]
            premature_applications = [
                item
                for item in proposals
                if (item.get("application") or {}).get("status")
                in {"queued", "applying", "applied"}
            ]
            checks.extend(
                [
                    {"name": "pending_proposal_created", "passed": bool(pending)},
                    {
                        "name": "proposal_not_activated",
                        "passed": not any(
                            item.get("status") == "approved" for item in proposals
                        ),
                    },
                    {
                        "name": "proposal_application_not_queued_preapproval",
                        "passed": not premature_applications,
                    },
                ]
            )
    else:
        report = read_json(root / "reports" / "auto" / "latest.json", default={}) or {}
        orders = report.get("orders") or []
        decision = str(report.get("decision") or "").lower()
        orders_empty = orders in ([], {})
        orders_attempted = False
        orders_successful = False
        match orders:
            case dict():
                attempted = orders.get("attempted") or []
                successful = orders.get("successful") or []
                orders_empty = attempted == [] and successful == []
                orders_attempted = bool(attempted)
                orders_successful = bool(successful)
            case list():
                orders_attempted = bool(orders)
                orders_successful = bool(orders)
        forbidden_tools = [
            "wayfinder_hyperliquid_place_",
            "wayfinder_polymarket_place_",
            "wayfinder_onchain_swap",
            "wayfinder_onchain_send",
            "wayfinder_contracts_execute",
        ]
        forbidden_hits = [name for name in forbidden_tools if name in log_text]
        checks.extend(
            [
                {"name": "auto_report_exists", "passed": bool(report)},
                {
                    "name": "auto_has_decision",
                    "passed": decision
                    in {"skipped", "blocked", "would_trade", "executed"},
                },
                {
                    "name": "weak_edge_skips",
                    "passed": iteration != 1 or decision == "skipped",
                },
                {
                    "name": "strong_edge_intervenes",
                    "passed": iteration != 2
                    or (
                        decision == "executed"
                        and orders_attempted
                        and orders_successful
                    ),
                },
                {
                    "name": "no_orders_on_skip",
                    "passed": iteration != 1 or orders_empty,
                },
                {"name": "no_real_order_tool_calls", "passed": not forbidden_hits},
                {
                    "name": "risk_limits_present",
                    "passed": bool(report.get("risk_limits")),
                },
            ]
        )
    return {
        "status": "passed" if all(check["passed"] for check in checks) else "failed",
        "checks": checks,
        "job_dir": str(root),
    }


def approve_worker_proposal_for_application(
    workspace: Path, case: WorkerCase
) -> dict[str, Any]:
    store = JobStore(repo_root=workspace)
    return store.approve_proposal(case.job_id, SCRIPT_AGENT_PROPOSAL_ID)


def validate_application_case(
    workspace: Path, case: WorkerCase, *, log_text: str = ""
) -> dict[str, Any]:
    root = workspace / ".wayfinder" / "jobs" / case.job_id
    proposal = (
        read_json(root / "proposals" / f"{SCRIPT_AGENT_PROPOSAL_ID}.json", default={})
        or {}
    )
    application = proposal.get("application") or {}
    job_yaml = yaml.safe_load((root / "job.yaml").read_text(encoding="utf-8"))
    script_loop = job_yaml.get("script_loop") or {}
    script_path = script_entrypoint_path(workspace, script_loop, job_id=case.job_id)
    script_text = (
        script_path.read_text(encoding="utf-8", errors="replace")
        if script_path.exists()
        else ""
    )
    report = read_json(root / "reports" / "apply" / "latest.json", default={}) or {}
    validation = application.get("validation") or {}
    deterministic_validation = (
        validation.get("deterministic_validation") or report.get("validation") or {}
    )
    validation_attempts = (
        validation.get("validation_attempts") or report.get("validation_attempts") or []
    )
    deterministic_checks = deterministic_validation.get("checks") or []
    scenario_checks = [
        check
        for check in deterministic_checks
        if str(check.get("name") or "").startswith("scenario_")
    ]
    journal = (root / "journal.jsonl").read_text(encoding="utf-8", errors="replace")
    forbidden_tools = [
        "wayfinder_hyperliquid_place_",
        "wayfinder_polymarket_place_",
        "wayfinder_onchain_swap",
        "wayfinder_onchain_send",
        "wayfinder_contracts_execute",
    ]
    forbidden_hits = [name for name in forbidden_tools if name in log_text]
    checks = [
        {"name": "proposal_exists", "passed": bool(proposal)},
        {"name": "proposal_approved", "passed": proposal.get("status") == "approved"},
        {
            "name": "application_applied",
            "passed": application.get("status") == "applied",
        },
        {
            "name": "claim_recorded",
            "passed": "proposal_apply_started" in journal,
        },
        {
            "name": "completion_recorded",
            "passed": "proposal_apply_finished" in journal,
        },
        {"name": "apply_report_exists", "passed": bool(report)},
        {
            "name": "apply_report_references_proposal",
            "passed": report.get("apply_proposal_id") == SCRIPT_AGENT_PROPOSAL_ID
            or report.get("proposal_id") == SCRIPT_AGENT_PROPOSAL_ID,
        },
        {
            "name": "changed_files_recorded",
            "passed": bool(application.get("changed_files")),
        },
        {"name": "validation_recorded", "passed": bool(application.get("validation"))},
        {
            "name": "deterministic_validation_passed",
            "passed": deterministic_validation.get("status") == "passed",
        },
        {
            "name": "scenario_validation_passed",
            "passed": bool(scenario_checks)
            and all(bool(check.get("passed")) for check in scenario_checks),
        },
        {
            "name": "promoted_revision_recorded",
            "passed": bool(
                application.get("promoted_revision")
                or validation.get("promoted_revision")
                or report.get("promoted_revision")
            ),
        },
        {
            "name": "complex_apply_feedback_loop",
            "passed": not case.complex_apply
            or (
                len(validation_attempts) >= 2
                and validation_attempts[0].get("status") == "failed"
                and validation_attempts[-1].get("status") == "passed"
            ),
        },
        {"name": "script_entrypoint_exists", "passed": script_path.exists()},
        {
            "name": "script_moved_into_job_workspace",
            "passed": (
                ".wayfinder/jobs/" in str(script_loop.get("entrypoint") or "")
                and "/workspace/" in str(script_loop.get("entrypoint") or "")
            )
            or str(script_loop.get("entrypoint") or "").startswith("workspace/"),
        },
        {
            "name": "script_keeps_forward_recorder",
            "passed": "get_forward_recorder" in script_text
            and "record_run(" in script_text,
        },
        {
            "name": "script_contains_rearm_guard_change",
            "passed": "rearm_guard" in script_text.lower()
            or "near-clear" in script_text.lower()
            or "near_clear" in script_text.lower()
            or "rearm_tolerance" in script_text.lower()
            or "tolerance band" in script_text.lower()
            or "tolerance-band" in json.dumps(report).lower(),
        },
        {"name": "no_real_order_tool_calls", "passed": not forbidden_hits},
    ]
    return {
        "status": "passed" if all(check["passed"] for check in checks) else "failed",
        "checks": checks,
        "job_dir": str(root),
    }


def write_valid_worker_artifacts(
    workspace: Path, case: WorkerCase, *, iteration: int
) -> None:
    """Create expected-good artifacts for deterministic validator tests."""
    root = workspace / ".wayfinder" / "jobs" / case.job_id
    if case.kind == "script_agent_worker":
        if iteration == 1:
            write_json(
                root / "reports" / "monitor" / "latest.json",
                {
                    "job_id": case.job_id,
                    "mode": "monitor",
                    "status": "yellow",
                    "summary": "SNX lag is emerging; monitor one more cycle before proposing changes.",
                    "findings": [
                        "Forward data is below baseline but sample is still small."
                    ],
                    "recommended_action": "continue_monitoring",
                },
            )
        else:
            write_json(
                root / "reports" / "intervene" / "latest.json",
                {
                    "job_id": case.job_id,
                    "mode": "intervene",
                    "status": "yellow",
                    "summary": "Repeated SNX/IMX re-arm drift warrants a user-approved proposal.",
                    "findings": ["Loss streak now exceeds backtest p95."],
                    "recommended_action": "review_pending_proposal",
                },
            )
            write_json(
                root / "proposals" / f"{SCRIPT_AGENT_PROPOSAL_ID}.json",
                {
                    "proposal_id": SCRIPT_AGENT_PROPOSAL_ID,
                    "job_id": case.job_id,
                    "status": "pending",
                    "proposed_change": {
                        "summary": "Add an early-warning state for IMX near-clear, but keep both-leg confirmation."
                    },
                    "intent_contract": script_agent_intent_contract(),
                    "scenario_plan": script_agent_scenario_plan(),
                    "validation": {"backtest_required": True, "paper_required": True},
                    "approval": {"required": True, "status": "pending"},
                },
            )
    else:
        write_json(
            root / "reports" / "auto" / "latest.json",
            {
                "job_id": case.job_id,
                "mode": "auto",
                "status": "yellow" if iteration == 2 else "green",
                "summary": "Auto decision completed.",
                "decision": "executed" if iteration == 2 else "skipped",
                "orders": {
                    "attempted": [
                        {
                            "venue": "hyperliquid",
                            "symbol": "BTC",
                            "side": "buy",
                            "notional": 25,
                            "reason": "68 bps signal edge clears intervention threshold",
                            "simulated": True,
                        }
                    ]
                    if iteration == 2
                    else [],
                    "successful": [
                        {
                            "venue": "hyperliquid",
                            "symbol": "BTC",
                            "side": "buy",
                            "notional": 25,
                            "order_id": "simulated-eval-order-001",
                            "simulated": True,
                        }
                    ]
                    if iteration == 2
                    else [],
                },
                "risk_limits": {
                    "max_notional_per_decision": 25,
                    "max_daily_notional": 100,
                    "consumed_notional": 25 if iteration == 2 else 0,
                },
                "next_check": "next scheduled wakeup",
            },
        )


def write_valid_application_artifacts(workspace: Path, case: WorkerCase) -> None:
    """Create expected-good apply artifacts for deterministic validator tests."""
    store = JobStore(repo_root=workspace)
    root = store.job_dir(case.job_id)
    proposal = store.load_proposal(case.job_id, SCRIPT_AGENT_PROPOSAL_ID)
    if proposal.get("status") != "approved":
        store.approve_proposal(case.job_id, SCRIPT_AGENT_PROPOSAL_ID)
    candidate_dir = root / "applications" / SCRIPT_AGENT_PROPOSAL_ID / "candidate"
    if candidate_dir.exists():
        shutil.rmtree(candidate_dir)
    candidate_workspace = candidate_dir / "workspace"
    candidate_workspace.mkdir(parents=True, exist_ok=True)
    if (root / "workspace").exists():
        shutil.copytree(root / "workspace", candidate_workspace, dirs_exist_ok=True)
    shutil.copy2(root / "job.yaml", candidate_dir / "job.yaml")
    proposal = store.load_proposal(case.job_id, SCRIPT_AGENT_PROPOSAL_ID)
    if (proposal.get("application") or {}).get("status") != "applying":
        store.claim_proposal_application(
            case.job_id,
            SCRIPT_AGENT_PROPOSAL_ID,
            paused_runner_jobs=[
                {"loop": "script", "runner_job_name": "eval-snx-imx-rearm-script"},
                {"loop": "agent", "runner_job_name": "eval-snx-imx-rearm-agent"},
            ],
            candidate={
                "candidate_workspace": str(
                    candidate_workspace.relative_to(store.repo_root)
                ),
                "candidate_job_yaml": str(
                    (candidate_dir / "job.yaml").relative_to(store.repo_root)
                ),
                "candidate_dir": str(candidate_dir.relative_to(store.repo_root)),
            },
        )
    source_script = (
        workspace / ".wayfinder_runs" / "eval_inputs" / "sma_rearm_strategy.py"
    )
    source_csv = workspace / ".wayfinder_runs" / "eval_inputs" / "sma_rearm_prices.csv"
    new_script = candidate_workspace / "src" / "sma_rearm_strategy.py"
    new_csv = candidate_workspace / "src" / "sma_rearm_prices.csv"
    validation_attempts: list[dict[str, Any]] = []

    def stage_candidate(*, rearm_reason: str) -> dict[str, Any]:
        job_yaml_path = candidate_dir / "job.yaml"
        job_yaml = yaml.safe_load(job_yaml_path.read_text(encoding="utf-8"))
        new_script.parent.mkdir(parents=True, exist_ok=True)
        text = source_script.read_text(encoding="utf-8")
        text = text.replace(
            '"reason": "SNX still below SMA50; IMX is near clear."',
            f'"reason": "{rearm_reason}"',
        )
        text += "\nREARM_GUARD_ENABLED = True\n"
        new_script.write_text(text, encoding="utf-8")
        shutil.copy2(source_csv, new_csv)
        job_yaml["script_loop"]["entrypoint"] = (
            f".wayfinder/jobs/{case.job_id}/workspace/src/sma_rearm_strategy.py"
        )
        job_yaml_path.write_text(
            yaml.safe_dump(job_yaml, sort_keys=False), encoding="utf-8"
        )
        proposal = store.load_proposal(case.job_id, SCRIPT_AGENT_PROPOSAL_ID)
        result = validate_candidate_application(
            repo_root=workspace,
            job_dir=root,
            proposal=proposal,
            candidate_dir=candidate_dir,
        )
        validation_attempts.append(
            {
                "attempt": len(validation_attempts) + 1,
                "status": result["status"],
                "failed_checks": [
                    check.get("name")
                    for check in result.get("checks", [])
                    if not check.get("passed")
                ],
            }
        )
        return result

    if case.complex_apply:
        deterministic_validation = stage_candidate(
            rearm_reason="SNX still below SMA50; IMX is near clear."
        )
        if deterministic_validation["status"] == "passed":
            raise AssertionError("complex apply fixture should fail first validation")
    deterministic_validation = stage_candidate(
        rearm_reason="rearm_guard: SNX still below SMA50; IMX is near-clear."
    )
    if deterministic_validation["status"] != "passed":
        raise AssertionError(deterministic_validation)
    active_workspace = root / "workspace"
    if active_workspace.exists():
        shutil.rmtree(active_workspace)
    shutil.copytree(candidate_workspace, active_workspace)
    shutil.copy2(candidate_dir / "job.yaml", root / "job.yaml")
    relative_changed = "workspace/src/sma_rearm_strategy.py"
    promoted_revision = "eval-deterministic-revision"
    store.complete_proposal_application(
        case.job_id,
        SCRIPT_AGENT_PROPOSAL_ID,
        status="applied",
        changed_files=[relative_changed, "job.yaml"],
        validation={
            "py_compile": "passed",
            "telemetry_preserved": True,
            "deterministic_validation": deterministic_validation,
            "validation_attempts": validation_attempts,
            "promoted_revision": promoted_revision,
        },
        promoted_revision=promoted_revision,
        runner_responses=[
            {"loop": "script", "response": {"ok": True, "action": "resume"}},
            {"loop": "agent", "response": {"ok": True, "action": "resume"}},
        ],
    )
    write_json(
        root / "reports" / "apply" / "latest.json",
        {
            "job_id": case.job_id,
            "mode": "apply",
            "status": "green",
            "apply_proposal_id": SCRIPT_AGENT_PROPOSAL_ID,
            "summary": "Applied rearm guard proposal and preserved forward logging.",
            "changed_files": [relative_changed, "job.yaml"],
            "validation": deterministic_validation,
            "validation_attempts": validation_attempts,
            "promoted_revision": promoted_revision,
        },
    )


def build_creation_prompt(case: CreationCase) -> str:
    return (
        f"{case.prompt}\n\n"
        f"Use the exact job_id `{case.job_id}`.\n\n"
        "Eval harness instruction: finish in this single run. Do not output a progress "
        "checkpoint or ask follow-up questions. The final answer must start with "
        "`FINAL ANSWER` and include the job id."
    )


def build_execution_backtest_prompt(case: ExecutionBacktestCase) -> str:
    return (
        f"{case.prompt}\n\n"
        f"Use the exact job_id `{case.job_id}`.\n\n"
        "Hard eval requirements: finish in this single run, run the local execution "
        "backtest and grid validation yourself, and leave all artifacts under the "
        "job directory. Do not output a progress checkpoint or ask follow-up "
        "questions. The final answer must start with `FINAL ANSWER` and include "
        "the job id, the backtest artifact paths, and the validation status."
    )


def build_worker_prompt(case: WorkerCase, *, iteration: int) -> str:
    if case.kind == "script_agent_worker":
        mode = "monitor" if iteration == 1 else "intervene"
        expected = (
            "write a monitor report only; do not create a proposal yet"
            if iteration == 1
            else (
                "write a concise intervene report and create one pending proposal "
                f"with exact proposal_id `{SCRIPT_AGENT_PROPOSAL_ID}`; do not "
                "activate it"
            )
        )
        return (
            f"Run iteration {iteration} for Wayfinder job `{case.job_id}` in {mode} mode.\n"
            "Use the local job bundle, fake backtest baseline, fake forward run logs, "
            "and strategy script in this workspace. This is an eval with fake data.\n"
            "Use glob/read for inspection. Use write/edit if available. If this "
            "OpenCode runtime does not expose write/edit, write artifacts with a "
            "single relative here-doc command like `cat > .wayfinder/jobs/<job_id>/...`. "
            "Normal local development tools are allowed for this worker, including "
            "shell, Python/YAML helpers, `mkdir -p`, and syntax checks. Keep durable "
            "changes under `.wayfinder/jobs/<job_id>/`.\n"
            f"Expected next action: {expected}.\n"
            "Keep artifacts compact enough to finish inside the worker step budget. "
            "For iteration 2, write the proposal immediately after the intervene "
            "report; memory updates are optional and lower priority. The proposal "
            'JSON must use `status: "pending"`; do not use `queued` for '
            "`proposal.status`, and do not set `application.status` to `queued` "
            "in a newly created proposal. Leave application absent or set "
            "`application.status` to `not_requested`; the SDK approval flow queues "
            "application only after user approval. Include an `intent_contract` "
            "with intent, rules_changed, rules_unchanged, risk_constraints, "
            "entry_conditions, exit_conditions, and known_non_goals. Include a "
            "`scenario_plan` using decision_function `decide_from_snapshot` with "
            "scenarios for entry allowed, entry blocked by SNX not re-armed, and "
            "in-progress candle ignored.\n"
            "Write the structured files under `.wayfinder/jobs/<job_id>/reports` and "
            "`.wayfinder/jobs/<job_id>/proposals` as appropriate. Do not place trades, "
            "do not ask questions, and do not activate proposals."
        )
    expected = (
        "weak edge, skip"
        if iteration == 1
        else "strong edge, intervene by writing a simulated executed order inside risk limits"
    )
    return (
        f"Run iteration {iteration} for Wayfinder auto job `{case.job_id}`.\n"
        "Use the fake market snapshot and job auto_limits in this workspace. "
        "The market data is fake and local, so do not call real exchange or "
        "order-placement tools. If the edge merits action, simulate the auto "
        "intervention in the report with attempted and successful order records "
        "marked `simulated: true`.\n"
        "For this eval, treat `signal_edge_bps >= intervention_threshold_bps` "
        "as actionable when risk limits permit; weaker edges should be skipped.\n"
        "Use glob/read for inspection. Use write/edit if available. If this "
        "OpenCode runtime does not expose write/edit, write artifacts with a "
        "single relative here-doc command like `cat > .wayfinder/jobs/<job_id>/...`. "
        "Normal local development tools are allowed for this worker, including "
        "shell, Python/YAML helpers, `mkdir -p`, and syntax checks. Keep durable "
        "changes under `.wayfinder/jobs/<job_id>/`.\n"
        f"Expected next action: {expected}.\n"
        "Write `.wayfinder/jobs/<job_id>/reports/auto/latest.json` with decision, "
        "orders, risk_limits, and next_check."
    )


def build_application_prompt(workspace: Path, case: WorkerCase) -> str:
    store = JobStore(repo_root=workspace)
    sections = prepare_job_worker_prompt(
        store=store,
        job_id=case.job_id,
        mode="intervene",
        apply_proposal_id=SCRIPT_AGENT_PROPOSAL_ID,
        claim_application_before_prompt=True,
    )
    return str(sections["prompt"])


def build_candidate_command(
    opencode: str,
    model: str,
    prompt: str,
    *,
    directory: Path,
    title: str,
) -> list[str]:
    return [
        opencode,
        "run",
        "-m",
        model,
        "--dir",
        str(directory),
        "--title",
        title,
        prompt,
    ]


def build_worker_command(
    opencode: str,
    model: str,
    agent_name: str,
    prompt: str,
    *,
    directory: Path,
    title: str,
) -> list[str]:
    return [
        opencode,
        "run",
        "--agent",
        agent_name,
        "-m",
        model,
        "--dir",
        str(directory),
        "--title",
        title,
        prompt,
    ]


def build_judge_command(
    opencode: str,
    model: str,
    prompt: str,
    *,
    directory: Path,
    title: str,
) -> list[str]:
    return [
        opencode,
        "run",
        "--agent",
        "wayfinder-eval-judge",
        "-m",
        model,
        "--dir",
        str(directory),
        "--title",
        title,
        prompt,
    ]


def run_process(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
    timeout_seconds: int,
) -> tuple[int | None, float, str | None]:
    started = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.run(
                command,
                cwd=cwd,
                env=dict(env),
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        return proc.returncode, time.monotonic() - started, None
    except subprocess.TimeoutExpired as exc:
        return None, time.monotonic() - started, f"timeout after {exc.timeout}s"


def harvest_answer_from_db(db_path: Path, *, title: str) -> str | None:
    if not db_path.exists():
        return None
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT id FROM session WHERE title=? ORDER BY time_updated DESC LIMIT 1",
            (title,),
        ).fetchone()
        if not row:
            return None
        rows = con.execute(
            """SELECT json_extract(p.data,'$.text')
               FROM part p JOIN message m ON p.message_id = m.id
               WHERE m.session_id=? AND json_extract(p.data,'$.type')='text'
               ORDER BY m.time_created ASC""",
            (str(row[0]),),
        ).fetchall()
    finally:
        con.close()
    texts = [
        str(item[0]).strip() for item in rows if item[0] and len(str(item[0])) > 40
    ]
    return texts[-1] if texts else None


def harvest_answer(log_path: Path, db_path: Path, *, title: str) -> str:
    answer = harvest_answer_from_db(db_path, title=title)
    if answer:
        return answer
    return log_path.read_text(errors="replace").strip() if log_path.exists() else ""


def find_json(text: str) -> dict[str, Any] | None:
    candidates = re.findall(r"\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}", text, re.S)
    for blob in reversed(candidates):
        if '"verdict"' not in blob:
            continue
        try:
            parsed = json.loads(blob)
        except ValueError:
            continue
        match parsed:
            case dict():
                return parsed
    return None


def code_context(root: Path, *, max_chars_per_file: int = 14_000) -> str:
    sections: list[str] = []
    for relative in CODE_CONTEXT_FILES:
        path = root / relative
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars_per_file:
            text = text[:max_chars_per_file] + "\n...<truncated>"
        sections.append(f"## {relative}\n\n```python\n{text}\n```")
    return "\n\n".join(sections)


def artifact_bundle(workspace: Path, job_id: str, *, max_chars: int = 40_000) -> str:
    root = workspace / ".wayfinder" / "jobs" / job_id
    if not root.exists():
        return f"Job directory not found: {root}"
    files: list[Path] = []
    for pattern in (
        "job.yaml",
        "memory.md",
        "memory.json",
        "scorecard.json",
        "journal.jsonl",
        "results/**/*.json",
        "results/**/*.jsonl",
        "reports/**/*.json",
        "proposals/**/*.json",
    ):
        files.extend(sorted(root.glob(pattern)))
    rendered: list[str] = []
    remaining = max_chars
    for path in files:
        if remaining <= 0:
            break
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > remaining:
            text = text[:remaining] + "\n...<truncated>"
        relative = path.relative_to(workspace)
        rendered.append(f"## {relative}\n\n```text\n{text}\n```")
        remaining -= len(text)
    return "\n\n".join(rendered)


def build_jobs_judge_prompt(
    *,
    rubric_text: str,
    case_id: str,
    task: str,
    workspace: Path,
    job_id: str,
    validator_report: dict[str, Any],
    agent_output: str,
    root: Path | None = None,
) -> str:
    repo = root or repo_root()
    return "\n".join(
        [
            "You are a Wayfinder Jobs implementation judge.",
            "Inspect the provided codebase excerpts, generated artifacts, validator",
            "report, and agent output. Decide whether this eval case passes.",
            "Output strict JSON using the rubric schema.",
            "",
            rubric_text.rstrip(),
            "",
            "---",
            "",
            "CASE ID:",
            case_id,
            "",
            "TASK:",
            task,
            "",
            "VALIDATOR REPORT:",
            json.dumps(validator_report, indent=2, sort_keys=True),
            "",
            "AGENT OUTPUT:",
            agent_output or "(no harvested output)",
            "",
            "GENERATED ARTIFACTS:",
            artifact_bundle(workspace, job_id),
            "",
            "CODEBASE CONTEXT:",
            code_context(repo),
        ]
    )


def resolve_wayfinder_model_env(model: str, env: dict[str, str]) -> None:
    if not model.startswith("wayfinder/") or env.get("WAYFINDER_API_KEY"):
        return
    try:
        from wayfinder_paths.core.config import get_api_key, load_config

        load_config()
        key = get_api_key()
    except Exception:
        key = None
    if not key:
        raise RuntimeError(f"{model} requires WAYFINDER_API_KEY or system.api_key.")
    env["WAYFINDER_API_KEY"] = str(key).strip()


def resolve_judge_model(
    requested_model: str,
    *,
    fallback_model: str,
    allow_fallback: bool,
    env: dict[str, str],
) -> str:
    if not requested_model.startswith("openai/"):
        return requested_model
    if env.get("OPENAI_API_KEY"):
        return requested_model
    try:
        from wayfinder_paths.core.config import get_openai_credentials, load_config

        load_config()
        creds = get_openai_credentials()
    except Exception:
        creds = {"api_key": None, "organization": None}
    if creds.get("api_key"):
        env["OPENAI_API_KEY"] = str(creds["api_key"])
    if creds.get("organization"):
        env["OPENAI_ORGANIZATION"] = str(creds["organization"])
    if env.get("OPENAI_API_KEY"):
        return requested_model
    if allow_fallback:
        return fallback_model
    raise RuntimeError(
        f"{requested_model} requires OPENAI_API_KEY or system.openai.api_key. "
        "Use --allow-judge-fallback only for local/debug fallback."
    )


def run_judge(
    *,
    case_id: str,
    prompt: str,
    output_dir: Path,
    opencode_bin: str,
    judge_model: str,
    timeout_seconds: int,
    env: Mapping[str, str],
    db_path: Path,
) -> dict[str, Any]:
    title = f"eval/jobs/judge/{case_id}/{uuid.uuid4().hex[:8]}"
    prompt_path = output_dir / f"{case_id}.judge.prompt.md"
    log_path = output_dir / f"{case_id}.judge.log"
    verdict_path = output_dir / f"{case_id}.judge.json"
    prompt_path.write_text(prompt, encoding="utf-8")
    command = build_judge_command(
        opencode_bin,
        judge_model,
        prompt,
        directory=repo_root(),
        title=title,
    )
    returncode, duration, error = run_process(
        command,
        cwd=repo_root(),
        env=env,
        log_path=log_path,
        timeout_seconds=timeout_seconds,
    )
    output = harvest_answer(log_path, db_path, title=title)
    verdict = find_json(output) or find_json(log_path.read_text(errors="replace"))
    if verdict is not None:
        write_json(verdict_path, verdict)
    return {
        "case_id": case_id,
        "status": "passed"
        if returncode == 0 and verdict and verdict.get("verdict") == "pass"
        else "failed",
        "returncode": returncode,
        "duration_seconds": round(duration, 3),
        "error": error,
        "prompt_path": str(prompt_path),
        "log_path": str(log_path),
        "verdict_path": str(verdict_path) if verdict else None,
        "verdict": verdict,
    }


def run_creation_case(
    case: CreationCase,
    *,
    live: bool,
    judge: bool,
    output_dir: Path,
    opencode_bin: str,
    model: str,
    judge_model: str,
    timeout_seconds: int,
    env: Mapping[str, str],
    db_path: Path,
) -> dict[str, Any]:
    case_dir = output_dir / case.id
    case_dir.mkdir(parents=True, exist_ok=True)
    agent_output = ""
    with tempfile.TemporaryDirectory(prefix=f"wf-job-eval-{case.id}-") as tmp:
        workspace = Path(tmp) / "repo"
        copy_workspace(repo_root(), workspace)
        write_strategy_fixture(workspace)
        prompt = build_creation_prompt(case)
        (case_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        if live:
            title = f"eval/jobs/{case.id}/{uuid.uuid4().hex[:8]}"
            log_path = case_dir / "agent.log"
            command = build_candidate_command(
                opencode_bin,
                model,
                prompt,
                directory=workspace,
                title=title,
            )
            returncode, duration, error = run_process(
                command,
                cwd=workspace,
                env=env,
                log_path=log_path,
                timeout_seconds=timeout_seconds,
            )
            agent_output = harvest_answer(log_path, db_path, title=title)
        else:
            create_expected_job_bundle(workspace, case)
            returncode, duration, error = 0, 0.0, None
        validator = validate_creation_case(workspace, case)
        write_json(case_dir / "validator.json", validator)
        kept = case_dir / "workspace"
        if kept.exists():
            shutil.rmtree(kept)
        copy_workspace(workspace, kept)
        judge_result = None
        if judge:
            rubric = (repo_root() / JUDGE_RUBRIC).read_text(encoding="utf-8")
            prompt_for_judge = build_jobs_judge_prompt(
                rubric_text=rubric,
                case_id=case.id,
                task=prompt,
                workspace=kept,
                job_id=case.job_id,
                validator_report=validator,
                agent_output=agent_output,
            )
            judge_result = run_judge(
                case_id=case.id,
                prompt=prompt_for_judge,
                output_dir=case_dir,
                opencode_bin=opencode_bin,
                judge_model=judge_model,
                timeout_seconds=timeout_seconds,
                env=env,
                db_path=db_path,
            )
    status = (
        "passed"
        if validator["status"] == "passed"
        and (not judge_result or judge_result["status"] == "passed")
        else "failed"
    )
    return {
        "case_id": case.id,
        "status": status,
        "kind": case.kind,
        "live_returncode": returncode,
        "duration_seconds": round(duration, 3),
        "error": error,
        "validator": validator,
        "judge": judge_result,
    }


def run_execution_backtest_case(
    case: ExecutionBacktestCase,
    *,
    live: bool,
    judge: bool,
    output_dir: Path,
    opencode_bin: str,
    model: str,
    judge_model: str,
    timeout_seconds: int,
    env: Mapping[str, str],
    db_path: Path,
) -> dict[str, Any]:
    case_dir = output_dir / case.id
    case_dir.mkdir(parents=True, exist_ok=True)
    agent_output = ""
    with tempfile.TemporaryDirectory(prefix=f"wf-job-eval-{case.id}-") as tmp:
        workspace = Path(tmp) / "repo"
        copy_workspace(repo_root(), workspace)
        prompt = build_execution_backtest_prompt(case)
        (case_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        log_text = ""
        if live:
            title = f"eval/jobs/{case.id}/{uuid.uuid4().hex[:8]}"
            log_path = case_dir / "agent.log"
            command = build_candidate_command(
                opencode_bin,
                model,
                prompt,
                directory=workspace,
                title=title,
            )
            returncode, duration, error = run_process(
                command,
                cwd=workspace,
                env=env,
                log_path=log_path,
                timeout_seconds=timeout_seconds,
            )
            agent_output = harvest_answer(log_path, db_path, title=title)
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        else:
            create_expected_execution_backtest_bundle(workspace, case)
            returncode, duration, error = 0, 0.0, None
        validator = validate_execution_backtest_case(
            workspace,
            case,
            log_text=log_text,
        )
        write_json(case_dir / "validator.json", validator)
        kept = case_dir / "workspace"
        if kept.exists():
            shutil.rmtree(kept)
        copy_workspace(workspace, kept)
        judge_result = None
        if judge:
            rubric = (repo_root() / JUDGE_RUBRIC).read_text(encoding="utf-8")
            prompt_for_judge = build_jobs_judge_prompt(
                rubric_text=rubric,
                case_id=case.id,
                task=prompt,
                workspace=kept,
                job_id=case.job_id,
                validator_report=validator,
                agent_output=agent_output,
            )
            judge_result = run_judge(
                case_id=case.id,
                prompt=prompt_for_judge,
                output_dir=case_dir,
                opencode_bin=opencode_bin,
                judge_model=judge_model,
                timeout_seconds=timeout_seconds,
                env=env,
                db_path=db_path,
            )
    status = (
        "passed"
        if validator["status"] == "passed"
        and (not judge_result or judge_result["status"] == "passed")
        else "failed"
    )
    return {
        "case_id": case.id,
        "status": status,
        "kind": "execution_backtest",
        "live_returncode": returncode,
        "duration_seconds": round(duration, 3),
        "error": error,
        "validator": validator,
        "judge": judge_result,
    }


def run_worker_case(
    case: WorkerCase,
    *,
    live: bool,
    judge: bool,
    iterations: int,
    output_dir: Path,
    opencode_bin: str,
    model: str,
    judge_model: str,
    timeout_seconds: int,
    env: Mapping[str, str],
    db_path: Path,
) -> dict[str, Any]:
    case_dir = output_dir / case.id
    case_dir.mkdir(parents=True, exist_ok=True)
    iteration_reports: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix=f"wf-job-eval-{case.id}-") as tmp:
        workspace = Path(tmp) / "repo"
        copy_workspace(repo_root(), workspace)
        for iteration in range(1, iterations + 1):
            if case.kind == "script_agent_worker":
                setup_script_agent_worker_fixture(
                    workspace, iteration=iteration, case=case
                )
            else:
                setup_auto_worker_fixture(workspace, iteration=iteration)
            prompt = build_worker_prompt(case, iteration=iteration)
            iter_dir = case_dir / f"iteration_{iteration}"
            iter_dir.mkdir(parents=True, exist_ok=True)
            (iter_dir / "prompt.md").write_text(prompt, encoding="utf-8")
            log_text = ""
            if live:
                title = f"eval/jobs/{case.id}/iter-{iteration}/{uuid.uuid4().hex[:8]}"
                log_path = iter_dir / "worker.log"
                command = build_worker_command(
                    opencode_bin,
                    model,
                    case.agent_name,
                    prompt,
                    directory=workspace,
                    title=title,
                )
                returncode, duration, error = run_process(
                    command,
                    cwd=workspace,
                    env=env,
                    log_path=log_path,
                    timeout_seconds=timeout_seconds,
                )
                agent_output = harvest_answer(log_path, db_path, title=title)
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
            else:
                write_valid_worker_artifacts(workspace, case, iteration=iteration)
                returncode, duration, error, agent_output = 0, 0.0, None, ""
            validator = validate_worker_case(
                workspace, case, iteration=iteration, log_text=log_text
            )
            write_json(iter_dir / "validator.json", validator)
            judge_result = None
            if judge:
                rubric = (repo_root() / JUDGE_RUBRIC).read_text(encoding="utf-8")
                judge_prompt = build_jobs_judge_prompt(
                    rubric_text=rubric,
                    case_id=f"{case.id}:iteration_{iteration}",
                    task=prompt,
                    workspace=workspace,
                    job_id=case.job_id,
                    validator_report=validator,
                    agent_output=agent_output,
                )
                judge_result = run_judge(
                    case_id=f"{case.id}.iteration_{iteration}",
                    prompt=judge_prompt,
                    output_dir=iter_dir,
                    opencode_bin=opencode_bin,
                    judge_model=judge_model,
                    timeout_seconds=timeout_seconds,
                    env=env,
                    db_path=db_path,
                )
            iteration_reports.append(
                {
                    "iteration": iteration,
                    "status": "passed"
                    if validator["status"] == "passed"
                    and (not judge_result or judge_result["status"] == "passed")
                    else "failed",
                    "returncode": returncode,
                    "duration_seconds": round(duration, 3),
                    "error": error,
                    "validator": validator,
                    "judge": judge_result,
                }
            )
        application_report = None
        if case.kind == "script_agent_worker" and iterations >= 2:
            apply_dir = case_dir / "application"
            apply_dir.mkdir(parents=True, exist_ok=True)
            approve_worker_proposal_for_application(workspace, case)
            apply_prompt = build_application_prompt(workspace, case)
            (apply_dir / "prompt.md").write_text(apply_prompt, encoding="utf-8")
            apply_log_text = ""
            if live:
                title = f"eval/jobs/{case.id}/apply/{uuid.uuid4().hex[:8]}"
                log_path = apply_dir / "worker.log"
                command = build_worker_command(
                    opencode_bin,
                    model,
                    case.agent_name,
                    apply_prompt,
                    directory=workspace,
                    title=title,
                )
                apply_returncode, apply_duration, apply_error = run_process(
                    command,
                    cwd=workspace,
                    env=env,
                    log_path=log_path,
                    timeout_seconds=timeout_seconds,
                )
                apply_agent_output = harvest_answer(log_path, db_path, title=title)
                apply_log_text = log_path.read_text(encoding="utf-8", errors="replace")
            else:
                write_valid_application_artifacts(workspace, case)
                apply_returncode, apply_duration, apply_error = 0, 0.0, None
                apply_agent_output = ""
            application_validator = validate_application_case(
                workspace,
                case,
                log_text=apply_log_text,
            )
            write_json(apply_dir / "validator.json", application_validator)
            application_judge = None
            if judge:
                rubric = (repo_root() / JUDGE_RUBRIC).read_text(encoding="utf-8")
                judge_prompt = build_jobs_judge_prompt(
                    rubric_text=rubric,
                    case_id=f"{case.id}:application",
                    task=apply_prompt,
                    workspace=workspace,
                    job_id=case.job_id,
                    validator_report=application_validator,
                    agent_output=apply_agent_output,
                )
                application_judge = run_judge(
                    case_id=f"{case.id}.application",
                    prompt=judge_prompt,
                    output_dir=apply_dir,
                    opencode_bin=opencode_bin,
                    judge_model=judge_model,
                    timeout_seconds=timeout_seconds,
                    env=env,
                    db_path=db_path,
                )
            application_report = {
                "status": "passed"
                if application_validator["status"] == "passed"
                and (not application_judge or application_judge["status"] == "passed")
                else "failed",
                "returncode": apply_returncode,
                "duration_seconds": round(apply_duration, 3),
                "error": apply_error,
                "validator": application_validator,
                "judge": application_judge,
            }
        kept = case_dir / "workspace"
        if kept.exists():
            shutil.rmtree(kept)
        copy_workspace(workspace, kept)
    reports_ok = all(item["status"] == "passed" for item in iteration_reports)
    if application_report:
        reports_ok = reports_ok and application_report["status"] == "passed"
    return {
        "case_id": case.id,
        "status": "passed" if reports_ok else "failed",
        "kind": case.kind,
        "agent_name": case.agent_name,
        "iterations": iteration_reports,
        "application": application_report,
    }


def selected_creation_cases(selection: str) -> list[CreationCase]:
    if selection in {"all", "creation"}:
        return CREATION_CASES
    return [case for case in CREATION_CASES if case.id == selection]


def selected_worker_cases(selection: str) -> list[WorkerCase]:
    if selection in {"all", "workers"}:
        return WORKER_CASES
    return [case for case in WORKER_CASES if case.id == selection]


def selected_execution_backtest_cases(selection: str) -> list[ExecutionBacktestCase]:
    if selection in {"all", "execution_backtest"}:
        return EXECUTION_BACKTEST_CASES
    return [case for case in EXECUTION_BACKTEST_CASES if case.id == selection]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        default="all",
        choices=[
            "all",
            "creation",
            "workers",
            "execution_backtest",
            *[case.id for case in CREATION_CASES],
            *[case.id for case in WORKER_CASES],
            *[case.id for case in EXECUTION_BACKTEST_CASES],
        ],
    )
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--live", action="store_true", help="Run real OpenCode agents.")
    parser.add_argument(
        "--judge", action="store_true", help="Run the stronger pass/fail judge."
    )
    parser.add_argument(
        "--hard-live",
        action="store_true",
        help=(
            "Run the hard same-script execution backtest creation eval live and "
            "require the judge."
        ),
    )
    parser.add_argument("--iterations", type=int, default=2)
    parser.add_argument("--model", default=DEFAULT_CANDIDATE_MODEL)
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--judge-fallback-model", default=DEFAULT_FALLBACK_JUDGE_MODEL)
    parser.add_argument("--allow-judge-fallback", action="store_true")
    parser.add_argument("--opencode-bin", default=DEFAULT_OPENCODE)
    parser.add_argument("--opencode-db", default=DEFAULT_DB)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)

    if args.hard_live:
        args.case = "hard_execution_backtest_creation"
        args.live = True
        args.judge = True

    if args.iterations < 1:
        raise SystemExit("--iterations must be >= 1")

    output_dir = (
        repo_root() / args.output_dir / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    ).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    resolve_wayfinder_model_env(args.model, env)
    judge_model = args.judge_model
    if args.judge:
        judge_model = resolve_judge_model(
            args.judge_model,
            fallback_model=args.judge_fallback_model,
            allow_fallback=args.allow_judge_fallback,
            env=env,
        )
        resolve_wayfinder_model_env(judge_model, env)

    report: dict[str, Any] = {
        "started_at": utc_now(),
        "live": args.live,
        "judge": args.judge,
        "model": args.model,
        "judge_model": judge_model if args.judge else None,
        "cases": [],
    }
    db_path = Path(args.opencode_db)
    for case in selected_creation_cases(args.case):
        result = run_creation_case(
            case,
            live=args.live,
            judge=args.judge,
            output_dir=output_dir,
            opencode_bin=args.opencode_bin,
            model=args.model,
            judge_model=judge_model,
            timeout_seconds=args.timeout,
            env=env,
            db_path=db_path,
        )
        report["cases"].append(result)
    for case in selected_worker_cases(args.case):
        result = run_worker_case(
            case,
            live=args.live,
            judge=args.judge,
            iterations=args.iterations,
            output_dir=output_dir,
            opencode_bin=args.opencode_bin,
            model=args.model,
            judge_model=judge_model,
            timeout_seconds=args.timeout,
            env=env,
            db_path=db_path,
        )
        report["cases"].append(result)
    for case in selected_execution_backtest_cases(args.case):
        result = run_execution_backtest_case(
            case,
            live=args.live,
            judge=args.judge,
            output_dir=output_dir,
            opencode_bin=args.opencode_bin,
            model=args.model,
            judge_model=judge_model,
            timeout_seconds=args.timeout,
            env=env,
            db_path=db_path,
        )
        report["cases"].append(result)

    report["status"] = (
        "passed"
        if report["cases"]
        and all(case["status"] == "passed" for case in report["cases"])
        else "failed"
    )
    write_json(output_dir / "latest.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
