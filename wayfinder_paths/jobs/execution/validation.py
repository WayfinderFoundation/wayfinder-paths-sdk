from __future__ import annotations

import importlib.util
import json
import py_compile
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.store import JobStore

FORBIDDEN_ORDER_PATTERNS = (
    "hyperliquid_place_",
    "polymarket_place_",
    ".place_market_order(",
    ".place_limit_order(",
    ".place_trigger_order(",
    ".place_stop_loss(",
)
RAW_CANDLE_PATTERNS = ("ccxt", "fetch_ohlcv", "get_candles(")
MANUAL_STATE_CLEAR_PATTERNS = (
    "in_position = False",
    '"in_position": False',
    "'in_position': False",
    "position = None",
)


def validate_execution_trace(
    trace: Mapping[str, Any],
    execution_spec: ExecutionSpec | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    spec = (
        execution_spec
        if isinstance(execution_spec, ExecutionSpec)
        else ExecutionSpec.from_dict(execution_spec or trace.get("execution_spec"))
    )
    issues: list[str] = []
    warnings: list[str] = []
    critical_failures: list[str] = []

    runs = trace.get("runs") if isinstance(trace, Mapping) else []
    visible_counts = [
        int(item.get("visible_bar_count"))
        for item in runs or []
        if isinstance(item, Mapping) and item.get("visible_bar_count") is not None
    ]
    no_lookahead = visible_counts == sorted(visible_counts)
    if not no_lookahead:
        critical_failures.append(
            "visible bar count moved backward or leaked future bars"
        )

    bracket_events = trace.get("bracket_events") if isinstance(trace, Mapping) else []
    ohlc_correct = all(
        bool(item.get("used_ohlc"))
        for item in bracket_events or []
        if isinstance(item, Mapping) and item.get("hit")
    )
    if not ohlc_correct:
        critical_failures.append("bracket event missing OHLC high/low evaluation")
    if spec.ohlc_rules.get("use_high_low_for_stops") and not bracket_events:
        warnings.append(
            "no bracket events recorded; stop/TP behavior was not exercised"
        )

    fills = trace.get("fills") if isinstance(trace, Mapping) else []
    hidden_success = [
        fill
        for fill in fills or []
        if isinstance(fill, Mapping)
        and str(fill.get("status")).lower() not in {"filled", "partial"}
        and not fill.get("error")
    ]
    if hidden_success:
        issues.append("non-filled order statuses must not be reported as success")

    ledger_snapshots = (
        trace.get("ledger_snapshots") if isinstance(trace, Mapping) else []
    )
    ledger_valid = all(isinstance(item, Mapping) for item in ledger_snapshots or [])
    if not ledger_valid:
        critical_failures.append("ledger snapshots are malformed")

    execution_valid = not critical_failures and not issues
    return {
        "execution_valid": execution_valid,
        "data_valid": no_lookahead,
        "state_valid": ledger_valid,
        "capacity_valid": True,
        "issues": issues,
        "warnings": warnings,
        "critical_failures": critical_failures,
        "auto_fix_suggestions": _suggestions(issues + critical_failures + warnings),
    }


def validate_execution_job(
    job_id: str,
    *,
    strict: bool = False,
    candidate_dir: str | Path | None = None,
    store: JobStore | None = None,
) -> dict[str, Any]:
    store = store or JobStore()
    root = Path(candidate_dir) if candidate_dir else store.job_dir(job_id)
    job_yaml_path = root / "job.yaml"
    checks: list[dict[str, Any]] = [
        {
            "name": "job_yaml_exists",
            "passed": job_yaml_path.exists(),
            "path": str(job_yaml_path),
        }
    ]
    job_data: dict[str, Any] = {}
    if job_yaml_path.exists():
        try:
            loaded = yaml.safe_load(job_yaml_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                job_data = loaded
            checks.append(
                {"name": "job_yaml_parse", "passed": isinstance(loaded, dict)}
            )
        except Exception as exc:
            checks.append(
                {"name": "job_yaml_parse", "passed": False, "error": str(exc)}
            )

    spec_data, spec_path = _load_execution_spec(root, job_data)
    has_spec = bool(spec_data)
    checks.append(
        {
            "name": "execution_spec_present",
            "passed": has_spec,
            "path": str(spec_path) if spec_path else None,
            "blocking": bool(strict),
        }
    )
    if not has_spec:
        report = _report(checks, strict=strict)
        if not candidate_dir:
            store.write_json(job_id, "reports/validation/latest.json", report)
        return report

    spec = ExecutionSpec.from_dict(spec_data)
    checks.extend(_execution_spec_checks(spec))
    script_path = _script_path(root, job_data, store.repo_root)
    checks.append(
        {
            "name": "execution_script_exists",
            "passed": bool(script_path and script_path.exists()),
            "path": str(script_path) if script_path else None,
        }
    )
    if script_path and script_path.exists():
        checks.extend(_script_static_checks(script_path, spec))
        checks.extend(_strategy_entrypoint_checks(script_path))
        checks.extend(_execution_scenario_checks(script_path, job_data, spec))

    trace_report = _latest_trace_validation(root, spec)
    if trace_report is not None:
        checks.append(
            {
                "name": "latest_trace_valid",
                "passed": bool(trace_report.get("execution_valid")),
                "details": trace_report,
            }
        )

    report = _report(checks, strict=strict or spec.strict)
    if not candidate_dir:
        store.write_json(job_id, "reports/validation/latest.json", report)
    return report


def _load_execution_spec(
    root: Path, job_data: Mapping[str, Any]
) -> tuple[dict[str, Any], Path | None]:
    embedded = job_data.get("execution_spec")
    if isinstance(embedded, Mapping):
        return dict(embedded), None
    path = root / "execution_spec.json"
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded, path
        except Exception:
            return {}, path
    return {}, None


def _execution_spec_checks(spec: ExecutionSpec) -> list[dict[str, Any]]:
    return [
        {
            "name": "bar_model_completed_only",
            "passed": spec.bar_model == "completed_only",
        },
        {
            "name": "fill_model_supported",
            "passed": spec.fill_model in {"next_bar_open", "replay"},
        },
        {
            "name": "ohlc_stops_use_high_low",
            "passed": bool(spec.ohlc_rules.get("use_high_low_for_stops")),
        },
        {
            "name": "token_state_not_execution_venue",
            "passed": spec.view_type != "token_state",
        },
    ]


def _script_static_checks(
    script_path: Path, spec: ExecutionSpec
) -> list[dict[str, Any]]:
    text = script_path.read_text(encoding="utf-8", errors="replace")
    checks: list[dict[str, Any]] = []
    try:
        py_compile.compile(str(script_path), doraise=True)
        checks.append({"name": "execution_script_py_compile", "passed": True})
    except Exception as exc:
        checks.append(
            {"name": "execution_script_py_compile", "passed": False, "error": str(exc)}
        )
    checks.append(
        {
            "name": "no_direct_order_placement",
            "passed": not any(pattern in text for pattern in FORBIDDEN_ORDER_PATTERNS),
        }
    )
    raw_hits = [pattern for pattern in RAW_CANDLE_PATTERNS if pattern in text]
    checks.append(
        {
            "name": "no_forbidden_external_candles",
            "passed": not (spec.data_contract.get("no_external_ccxt") and raw_hits),
            "details": raw_hits,
        }
    )
    checks.append(
        {
            "name": "no_manual_position_clear",
            "passed": not any(
                pattern in text for pattern in MANUAL_STATE_CLEAR_PATTERNS
            ),
        }
    )
    close_stop_pattern = re.search(r"(stop|take_profit|tp).*close", text, re.IGNORECASE)
    checks.append(
        {
            "name": "no_close_only_stop_tp",
            "passed": close_stop_pattern is None
            or "BracketEngine" in text
            or "ohlc_" in text,
        }
    )
    return checks


def _strategy_entrypoint_checks(script_path: Path) -> list[dict[str, Any]]:
    try:
        module = _load_module(script_path)
    except Exception as exc:
        return [{"name": "strategy_module_loads", "passed": False, "error": str(exc)}]
    return [
        {"name": "strategy_module_loads", "passed": True},
        {
            "name": "strategy_entrypoint_present",
            "passed": callable(getattr(module, "build_strategy", None))
            or callable(getattr(module, "decide", None)),
        },
    ]


def _execution_scenario_checks(
    script_path: Path, job_data: Mapping[str, Any], spec: ExecutionSpec
) -> list[dict[str, Any]]:
    scenario_plan = job_data.get("execution_scenario_plan") or spec.validation.get(
        "execution_scenario_plan"
    )
    if not scenario_plan:
        return [
            {
                "name": "execution_scenario_plan_present",
                "passed": not bool(spec.validation.get("require_scenarios")),
                "blocking": bool(spec.validation.get("require_scenarios")),
            }
        ]
    scenarios = (
        scenario_plan.get("scenarios") if isinstance(scenario_plan, Mapping) else None
    )
    if not isinstance(scenarios, list) or not scenarios:
        return [{"name": "execution_scenario_plan_present", "passed": False}]
    from wayfinder_paths.jobs.execution.simulator import (
        PreparedExecutionDataset,
        simulate_execution,
    )

    checks: list[dict[str, Any]] = [
        {"name": "execution_scenario_plan_present", "passed": True}
    ]
    for index, scenario in enumerate(scenarios):
        name = str(scenario.get("name") or f"scenario_{index + 1}")
        try:
            dataset = PreparedExecutionDataset.from_rows(
                list(scenario.get("bars") or [])
            )
            result = simulate_execution(
                script_path,
                dataset,
                spec,
                params=dict(scenario.get("params") or {}),
            )
            expected = dict(scenario.get("expect") or {})
            passed = _scenario_matches(result.to_dict(), expected)
            checks.append(
                {
                    "name": f"execution_scenario_{name}",
                    "passed": passed,
                    "expected": expected,
                    "stats": result.stats,
                    "validation": result.validation,
                }
            )
        except Exception as exc:
            checks.append(
                {
                    "name": f"execution_scenario_{name}",
                    "passed": False,
                    "error": str(exc),
                }
            )
    return checks


def _scenario_matches(result: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    if not expected:
        return True
    if "min_trades" in expected and len(result.get("trades") or []) < int(
        expected["min_trades"]
    ):
        return False
    if "max_trades" in expected and len(result.get("trades") or []) > int(
        expected["max_trades"]
    ):
        return False
    if "execution_valid" in expected:
        actual = (result.get("validation") or {}).get("execution_valid")
        if bool(actual) is not bool(expected["execution_valid"]):
            return False
    return True


def _latest_trace_validation(root: Path, spec: ExecutionSpec) -> dict[str, Any] | None:
    latest = root / "results" / "backtest" / "latest.json"
    if not latest.exists():
        return None
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except Exception:
        return {
            "execution_valid": False,
            "critical_failures": ["latest backtest JSON is invalid"],
        }
    trace = data.get("trace") if isinstance(data, Mapping) else None
    if not isinstance(trace, Mapping):
        return {
            "execution_valid": False,
            "critical_failures": ["latest backtest trace missing"],
        }
    return validate_execution_trace(trace, spec)


def _script_path(
    root: Path, job_data: Mapping[str, Any], repo_root: Path
) -> Path | None:
    script_loop = job_data.get("script_loop") if isinstance(job_data, Mapping) else {}
    if not isinstance(script_loop, Mapping) or not script_loop.get("enabled"):
        return None
    raw = str(script_loop.get("entrypoint") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        return path
    parts = path.parts
    if ".wayfinder" in parts and "workspace" in parts:
        workspace_index = parts.index("workspace")
        return root / "workspace" / Path(*parts[workspace_index + 1 :])
    if parts and parts[0] == "workspace":
        return root / path
    return repo_root / path


def _load_module(script_path: Path) -> Any:
    module_name = (
        f"_wayfinder_execution_validation_{abs(hash(str(script_path.resolve())))}"
    )
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load strategy script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    old_path = list(sys.path)
    try:
        sys.path.insert(0, str(script_path.parent))
        spec.loader.exec_module(module)
    finally:
        sys.path = old_path
    return module


def _report(checks: list[dict[str, Any]], *, strict: bool) -> dict[str, Any]:
    failed = [check for check in checks if not check.get("passed")]
    blocking = [
        check for check in failed if strict or check.get("blocking", True) is not False
    ]
    return {
        "status": "passed" if not blocking else "failed",
        "checks": checks,
        "strict": strict,
        "warnings": [check for check in failed if check not in blocking],
    }


def _suggestions(messages: list[str]) -> list[str]:
    suggestions: list[str] = []
    joined = " ".join(messages).lower()
    if "ohlc" in joined or "bracket" in joined:
        suggestions.append(
            "use BracketEngine / OHLC high-low helpers for stops and take profits"
        )
    if "lookahead" in joined or "future" in joined:
        suggestions.append(
            "feed strategies CompletedBarsView truncated to the current tick"
        )
    if "success" in joined or "status" in joined:
        suggestions.append(
            "treat resting/rejected/ambiguous order responses as non-success"
        )
    return suggestions
