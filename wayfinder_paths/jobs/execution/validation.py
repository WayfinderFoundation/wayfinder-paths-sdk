from __future__ import annotations

import json
import py_compile
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from croniter import croniter

from wayfinder_paths.jobs.execution.primitives import (
    ExecutionSpec,
    _load_module_from_path,
    bar_interval_seconds,
)
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.schedule import ScheduleSpec, normalize_schedule

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
    spec = ExecutionSpec.coerce(execution_spec or trace["execution_spec"])
    issues: list[str] = []
    warnings: list[str] = []
    critical_failures: list[str] = []

    visible_counts = [item["visible_bar_count"] for item in trace["runs"]]
    no_lookahead = visible_counts == sorted(visible_counts)
    if not no_lookahead:
        critical_failures.append(
            "visible bar count moved backward or leaked future bars"
        )

    bracket_events = trace["bracket_events"]
    ohlc_correct = all(item["used_ohlc"] for item in bracket_events if item["hit"])
    if not ohlc_correct:
        critical_failures.append("bracket event missing OHLC high/low evaluation")
    if spec.ohlc_rules["use_high_low_for_stops"] and not bracket_events:
        warnings.append(
            "no bracket events recorded; stop/TP behavior was not exercised"
        )

    hidden_success = [
        fill
        for fill in trace["fills"]
        if fill["status"] not in {"filled", "partial"} and not fill["error"]
    ]
    if hidden_success:
        issues.append("non-filled order statuses must not be reported as success")

    guard_events = trace.get("guard_events") or []
    stale_timestamps = {
        event.get("timestamp")
        for event in guard_events
        if event.get("kind") == "stale_data"
    }
    stale_entries = [
        fill
        for fill in trace["fills"]
        if fill.get("timestamp") in stale_timestamps
        and fill["status"] in {"filled", "partial"}
        and not fill.get("reduce_only")
    ]
    state_valid = not stale_entries
    if stale_entries:
        issues.append("position-opening fills executed against stale market data")

    rejected = [
        event for event in guard_events if event.get("kind") == "intent_rejected"
    ]
    capacity_valid = not rejected
    if rejected:
        warnings.append(
            f"{len(rejected)} intent(s) rejected by capability/limit guards"
        )

    execution_valid = not critical_failures and not issues
    return {
        "execution_valid": execution_valid,
        "data_valid": no_lookahead,
        "state_valid": state_valid,
        "capacity_valid": capacity_valid,
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
            match loaded:
                case dict():
                    job_data = loaded
                    yaml_ok = True
                case _:
                    yaml_ok = False
            checks.append({"name": "job_yaml_parse", "passed": yaml_ok})
        except Exception as exc:
            checks.append(
                {"name": "job_yaml_parse", "passed": False, "error": str(exc)}
            )

    spec_data, spec_path = resolve_execution_spec(root, job_data)
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
    checks.extend(_timing_checks(job_data, spec))
    checks.extend(_feature_checks(root, spec))
    script_path = store.resolve_script_entrypoint(
        job_id,
        job_data,
        candidate_dir=root if candidate_dir else None,
    )
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
                "passed": bool(trace_report["execution_valid"]),
                "details": trace_report,
            }
        )

    checks.extend(_preflight_checks(root, job_data, spec))

    report = _report(checks, strict=strict or spec.strict)
    report["revision"] = compute_workspace_revision(root)
    if not candidate_dir:
        store.write_json(job_id, "reports/validation/latest.json", report)
    return report


def _feature_checks(root: Path, spec: ExecutionSpec) -> list[dict[str, Any]]:
    from wayfinder_paths.jobs.execution.features import (
        load_feature_rows,
        parse_feature_specs,
    )

    try:
        specs = parse_feature_specs(spec)
    except ValueError as exc:
        # A malformed feature schema is a spec error: blocking.
        return [
            {"name": "declared_features_valid", "passed": False, "error": str(exc)}
        ]
    if not specs:
        return []
    frames = load_feature_rows([root], specs)
    missing = [
        item.name
        for item in specs
        if frames.get(item.name) is None or frames[item.name].empty
    ]
    return [
        {"name": "declared_features_valid", "passed": True, "count": len(specs)},
        {
            # Non-blocking: pre-live jobs may declare features before the
            # agent has published any rows.
            "name": "declared_features_available",
            "passed": not missing,
            "missing": missing,
            "blocking": False,
        },
    ]


def _preflight_checks(
    root: Path, job_data: Mapping[str, Any], spec: ExecutionSpec
) -> list[dict[str, Any]]:
    """A passing preflight (the behavioral gate that drives the real driver
    over replayed data + fault scenarios) is mandatory before live mode."""
    is_jobs_v1 = str(job_data.get("execution_contract") or "legacy") == "jobs_v1"
    if not is_jobs_v1:
        return []
    script_loop = job_data.get("script_loop") or {}
    live = str(script_loop.get("mode") or "paper") == "live"
    blocking = live or spec.strict
    path = root / "reports" / "preflight" / "latest.json"
    if not path.exists():
        return [
            {
                "name": "preflight_report_present",
                "passed": False,
                "blocking": blocking,
                "hint": "run `wayfinder job preflight`",
            }
        ]
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        report = {}
    return [
        {"name": "preflight_report_present", "passed": True, "blocking": blocking},
        {
            "name": "preflight_passed",
            "passed": report.get("status") == "passed",
            "blocking": blocking,
            "details": {
                "status": report.get("status"),
                "revision": report.get("revision"),
            },
        },
    ]


def resolve_execution_spec(
    root: Path, job_data: Mapping[str, Any]
) -> tuple[dict[str, Any], Path | None]:
    match job_data.get("execution_spec"):
        case Mapping() as embedded if embedded:
            return dict(embedded), None
    path = root / "execution_spec.json"
    if not path.exists():
        return {}, None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}, path
    match loaded:
        case dict():
            return loaded, path
        case _:
            return {}, path


STALE_POLICIES = frozenset({"skip", "flat", "decide_anyway"})


def _timing_checks(
    job_data: Mapping[str, Any], spec: ExecutionSpec
) -> list[dict[str, Any]]:
    """Structural timing checks: schedule vs bar interval, timeout, staleness.

    A strategy that consumes 1h bars but wakes every 4h silently skips bars; a
    timeout longer than the schedule period means the runner (which skips
    in-flight ticks and SIGKILLs on timeout) can starve the schedule. Both are
    unobservable in a fixture backtest, so they are validated structurally here.
    """
    is_jobs_v1 = str(job_data.get("execution_contract") or "legacy") == "jobs_v1"
    bar_seconds = bar_interval_seconds(spec.data_contract.get("bar_interval"))
    checks: list[dict[str, Any]] = [
        {
            "name": "bar_interval_declared",
            "passed": bar_seconds is not None or not is_jobs_v1,
            "value": spec.data_contract.get("bar_interval"),
            "blocking": is_jobs_v1,
        },
        {
            "name": "staleness_policy_valid",
            "passed": spec.data_contract.get("stale_policy") in STALE_POLICIES,
            "value": spec.data_contract.get("stale_policy"),
            "blocking": False,
        },
        {
            # Without an explicit base, equity/return stats and compound
            # sizing silently use the engine default — declare it.
            "name": "initial_capital_declared",
            "passed": bool(
                (job_data.get("execution_params") or {}).get("initial_capital")
            )
            or not is_jobs_v1,
            "value": (job_data.get("execution_params") or {}).get("initial_capital"),
            "blocking": False,
        },
        {
            # The live driver always fetches a bounded window (default 200
            # bars); an undeclared lookback means backtests see full history
            # while live sees 200 — path-dependent indicators (Wilder ATR,
            # SuperTrend) will diverge. Declaring it aligns both AND bounds
            # per-tick backtest cost.
            "name": "lookback_bars_declared",
            "passed": bool(
                (job_data.get("execution_params") or {}).get("lookback_bars")
            )
            or not is_jobs_v1,
            "value": (job_data.get("execution_params") or {}).get("lookback_bars"),
            "blocking": False,
        },
    ]

    script_loop = job_data.get("script_loop") or {}
    if not script_loop.get("enabled"):
        return checks

    try:
        schedule = normalize_schedule(
            interval_seconds=script_loop.get("interval_seconds"),
            cron_expr=script_loop.get("cron_expr"),
            timezone=script_loop.get("timezone"),
        )
        checks.append(
            {"name": "schedule_declared_valid", "passed": True, "kind": schedule.kind}
        )
    except (ValueError, TypeError) as exc:
        checks.append(
            {"name": "schedule_declared_valid", "passed": False, "error": str(exc)}
        )
        return checks

    period = _schedule_period_seconds(schedule)
    if bar_seconds is not None and period is not None:
        checks.append(
            {
                "name": "schedule_matches_bar_interval",
                "passed": period <= bar_seconds,
                "schedule_period_seconds": period,
                "bar_interval_seconds": bar_seconds,
            }
        )
    if period is not None:
        timeout = int(script_loop.get("timeout_seconds") or 120)
        checks.append(
            {
                "name": "timeout_vs_interval",
                "passed": timeout < period,
                "timeout_seconds": timeout,
                "schedule_period_seconds": period,
            }
        )
    return checks


def _schedule_period_seconds(schedule: ScheduleSpec) -> int | None:
    if schedule.kind == "interval":
        return schedule.interval_seconds
    if not schedule.cron_expr:
        return None
    iterator = croniter(schedule.cron_expr, 0)
    fires = [iterator.get_next(float) for _ in range(4)]
    gaps = sorted(b - a for a, b in zip(fires, fires[1:], strict=False))
    return int(gaps[len(gaps) // 2]) if gaps else None


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
            "passed": bool(spec.ohlc_rules["use_high_low_for_stops"]),
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
            "passed": not (spec.data_contract["no_external_ccxt"] and raw_hits),
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
        module = _load_module_from_path(script_path)
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
                "passed": not spec.validation["require_scenarios"],
                "blocking": bool(spec.validation["require_scenarios"]),
            }
        ]
    match scenario_plan:
        case Mapping():
            scenarios = scenario_plan.get("scenarios")
        case _:
            scenarios = None
    match scenarios:
        case list() if scenarios:
            pass
        case _:
            return [{"name": "execution_scenario_plan_present", "passed": False}]
    from wayfinder_paths.jobs.execution.simulator import (  # circular import
        PreparedExecutionDataset,
        simulate_execution,
    )

    checks: list[dict[str, Any]] = [
        {"name": "execution_scenario_plan_present", "passed": True}
    ]
    for index, scenario in enumerate(scenarios):
        name = str(scenario.get("name") or f"scenario_{index + 1}")
        try:
            dataset = PreparedExecutionDataset.from_rows(scenario.get("bars") or [])
            result = simulate_execution(
                script_path,
                dataset,
                spec,
                params=scenario.get("params") or {},
            )
            expected = scenario.get("expect") or {}
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
    trades = result["trades"]
    if "min_trades" in expected and len(trades) < int(expected["min_trades"]):
        return False
    if "max_trades" in expected and len(trades) > int(expected["max_trades"]):
        return False
    if "execution_valid" in expected:
        actual = result["validation"]["execution_valid"]
        if bool(actual) is not bool(expected["execution_valid"]):
            return False
    return True


def _latest_trace_validation(root: Path, spec: ExecutionSpec) -> dict[str, Any] | None:
    latest = root / "results" / "backtest" / "latest.json"
    if not latest.exists():
        return None
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "execution_valid": False,
            "critical_failures": ["latest backtest JSON is invalid"],
        }
    match data:
        case {"trace": Mapping() as trace}:
            return validate_execution_trace(trace, spec)
        case _:
            return {
                "execution_valid": False,
                "critical_failures": ["latest backtest trace missing"],
            }


def _report(checks: list[dict[str, Any]], *, strict: bool) -> dict[str, Any]:
    failed = [check for check in checks if not check["passed"]]
    blocking = [
        check for check in failed if strict or check.get("blocking") is not False
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
