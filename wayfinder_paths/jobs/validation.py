from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import py_compile
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.store import JobStore

REQUIRED_INTENT_FIELDS = (
    "intent",
    "rules_changed",
    "rules_unchanged",
    "risk_constraints",
    "entry_conditions",
    "exit_conditions",
    "known_non_goals",
)


def validate_candidate_application(
    *,
    repo_root: Path,
    job_dir: Path,
    proposal: Mapping[str, Any],
    candidate_dir: Path,
    require_judge: bool = False,
    allow_legacy: bool = False,
) -> dict[str, Any]:
    """Validate an approved job application candidate before promotion.

    The validator intentionally checks the candidate artifacts, not the active
    job workspace. It is deterministic: no market reads, no order tools, and no
    model calls. The optional judge result can be supplied by a separate worker.
    """

    checks: list[dict[str, Any]] = []
    candidate_job_yaml = candidate_dir / "job.yaml"
    candidate_workspace = candidate_dir / "workspace"

    checks.append(
        {
            "name": "candidate_workspace_exists",
            "passed": candidate_workspace.exists(),
            "path": str(candidate_workspace),
        }
    )
    checks.append(
        {
            "name": "candidate_job_yaml_exists",
            "passed": candidate_job_yaml.exists(),
            "path": str(candidate_job_yaml),
        }
    )

    job_data: dict[str, Any] = {}
    if candidate_job_yaml.exists():
        try:
            loaded = (
                yaml.safe_load(candidate_job_yaml.read_text(encoding="utf-8")) or {}
            )
            match loaded:
                case dict():
                    job_data = loaded
        except Exception as exc:
            checks.append(
                {
                    "name": "candidate_job_yaml_parse",
                    "passed": False,
                    "error": str(exc),
                }
            )

    checks.extend(_intent_contract_checks(proposal))
    checks.extend(_judge_checks(proposal, require_judge=require_judge))

    contract = str(job_data.get("execution_contract") or "legacy")
    checks.append(
        {
            "name": "execution_contract_jobs_v1",
            "passed": contract == "jobs_v1" or allow_legacy,
            "legacy_allowed": allow_legacy if contract != "jobs_v1" else None,
            "hint": (
                "the versioned-change flow requires the jobs_v1 driver "
                "contract; run `wayfinder job migrate-contract` first"
            ),
        }
    )

    script_path = _candidate_script_path(
        repo_root=repo_root,
        job_dir=job_dir,
        candidate_dir=candidate_dir,
        job_data=job_data,
        proposal=proposal,
    )
    checks.append(
        {
            "name": "candidate_script_exists",
            "passed": bool(script_path and script_path.exists()),
            "path": str(script_path) if script_path else None,
        }
    )
    if script_path and script_path.exists():
        if contract == "jobs_v1":
            checks.extend(_jobs_v1_script_checks(script_path))
            checks.extend(
                _engine_scenario_checks(script_path, proposal, job_data)
            )
            checks.extend(
                _candidate_behavior_checks(
                    repo_root=repo_root,
                    job_dir=job_dir,
                    candidate_dir=candidate_dir,
                    job_data=job_data,
                    script_path=script_path,
                )
            )
        else:
            checks.extend(_script_static_checks(script_path))
            checks.extend(_scenario_checks(script_path, proposal))

    passed = all(check.get("passed") for check in checks)
    return {
        "status": "passed" if passed else "failed",
        "checks": checks,
        "candidate_workspace": str(candidate_workspace),
        "candidate_job_yaml": str(candidate_job_yaml),
        "candidate_script": str(script_path) if script_path else None,
    }


def _jobs_v1_script_checks(script_path: Path) -> list[dict[str, Any]]:
    """jobs_v1 strategies are decide()-only modules: the driver owns telemetry
    and order routing, so forward-recorder greps and free-form main() checks
    are replaced by entrypoint + no-direct-order checks."""
    from wayfinder_paths.jobs.execution.validation import FORBIDDEN_ORDER_PATTERNS

    checks: list[dict[str, Any]] = []
    try:
        py_compile.compile(str(script_path), doraise=True)
        checks.append({"name": "py_compile", "passed": True})
    except Exception as exc:
        checks.append({"name": "py_compile", "passed": False, "error": str(exc)})
    text = script_path.read_text(encoding="utf-8", errors="replace")
    checks.append(
        {
            "name": "no_direct_order_placement",
            "passed": not any(pattern in text for pattern in FORBIDDEN_ORDER_PATTERNS),
        }
    )
    module = _load_module(script_path)
    checks.append(
        {
            "name": "strategy_entrypoint_present",
            "passed": module is not None
            and (
                callable(getattr(module, "build_strategy", None))
                or callable(getattr(module, "decide", None))
            ),
        }
    )
    return checks


def _engine_scenario_checks(
    script_path: Path, proposal: Mapping[str, Any], job_data: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Run proposal scenarios through the real engine (simulate_execution)
    instead of the legacy decide_from_snapshot fixture convention."""
    from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
    from wayfinder_paths.jobs.execution.simulator import (
        PreparedExecutionDataset,
        simulate_execution,
    )

    scenario_plan = proposal.get("scenario_plan")
    match scenario_plan:
        case list():
            scenarios = scenario_plan
        case Mapping():
            scenarios = scenario_plan.get("scenarios") or []
        case _:
            scenarios = []
    checks: list[dict[str, Any]] = [
        {
            "name": "scenario_plan_present",
            "passed": bool(scenarios),
            "scenario_count": len(scenarios),
        }
    ]
    if not scenarios:
        return checks
    spec = ExecutionSpec.from_dict(dict(job_data.get("execution_spec") or {}))
    for index, scenario in enumerate(scenarios):
        match scenario:
            case Mapping():
                scenario_data = dict(scenario)
            case _:
                scenario_data = {}
        name = str(scenario_data.get("name") or f"scenario_{index + 1}")
        bars = scenario_data.get("bars")
        if not bars:
            checks.append(
                {
                    "name": f"scenario_{name}",
                    "passed": False,
                    "error": (
                        "jobs_v1 scenarios must supply bars; snapshot-style "
                        "decide_from_snapshot fixtures are legacy-only"
                    ),
                }
            )
            continue
        expected = scenario_data.get("expect") or scenario_data.get("expected") or {}
        try:
            result = simulate_execution(
                script_path,
                PreparedExecutionDataset.from_rows(bars),
                spec,
                params=scenario_data.get("params") or {},
            ).to_dict()
            failures = _scenario_expectation_failures(result, expected)
            checks.append(
                {
                    "name": f"scenario_{name}",
                    "passed": not failures,
                    "expected": expected,
                    "failures": failures,
                    "stats": result["stats"],
                }
            )
        except Exception as exc:
            checks.append(
                {"name": f"scenario_{name}", "passed": False, "error": str(exc)}
            )
    return checks


def _scenario_expectation_failures(
    result: Mapping[str, Any], expected: Mapping[str, Any]
) -> list[str]:
    failures: list[str] = []
    trades = result.get("trades") or []
    if "min_trades" in expected and len(trades) < int(expected["min_trades"]):
        failures.append(f"expected >= {expected['min_trades']} trades, got {len(trades)}")
    if "max_trades" in expected and len(trades) > int(expected["max_trades"]):
        failures.append(f"expected <= {expected['max_trades']} trades, got {len(trades)}")
    if "execution_valid" in expected:
        actual = bool((result.get("validation") or {}).get("execution_valid"))
        if actual is not bool(expected["execution_valid"]):
            failures.append(f"execution_valid expected {expected['execution_valid']}")
    return failures


def _candidate_behavior_checks(
    *,
    repo_root: Path,
    job_dir: Path,
    candidate_dir: Path,
    job_data: Mapping[str, Any],
    script_path: Path,
) -> list[dict[str, Any]]:
    """Candidate backtest + preflight: the behavioral half of the promotion
    gate. Both run against the CANDIDATE workspace so the report is tied to
    the candidate revision (which equals the post-promotion revision)."""
    from wayfinder_paths.jobs.execution.job import _load_dataset
    from wayfinder_paths.jobs.execution.preflight import run_preflight
    from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
    from wayfinder_paths.jobs.execution.simulator import simulate_execution
    from wayfinder_paths.jobs.gating import compute_workspace_revision

    checks: list[dict[str, Any]] = []
    job_id = str(job_data.get("id") or job_dir.name)
    spec = ExecutionSpec.from_dict(dict(job_data.get("execution_spec") or {}))
    candidate_revision = compute_workspace_revision(candidate_dir)
    try:
        dataset = _load_dataset(candidate_dir, spec, dict(job_data))
    except FileNotFoundError:
        try:
            dataset = _load_dataset(job_dir, spec, dict(job_data))
        except FileNotFoundError as exc:
            checks.append(
                {
                    "name": "candidate_backtest_valid",
                    "passed": False,
                    "error": f"no dataset for candidate backtest: {exc}",
                }
            )
            return checks
    try:
        result = simulate_execution(
            script_path,
            dataset,
            spec,
            params=dict(job_data.get("execution_params") or {}),
        )
        checks.append(
            {
                "name": "candidate_backtest_valid",
                "passed": bool(result.validation.get("execution_valid")),
                "revision": candidate_revision,
                "stats": result.stats,
                "validation": result.validation,
            }
        )
    except Exception as exc:
        checks.append(
            {"name": "candidate_backtest_valid", "passed": False, "error": str(exc)}
        )
        return checks

    try:
        preflight = run_preflight(
            job_id,
            store=JobStore(repo_root=repo_root),
            candidate_dir=candidate_dir,
        )
        checks.append(
            {
                "name": "candidate_preflight_passed",
                "passed": preflight.get("status") == "passed",
                "revision": preflight.get("revision"),
                "failed_checks": [
                    check.get("name")
                    for check in preflight.get("checks") or []
                    if not check.get("passed") and check.get("blocking") is not False
                ],
            }
        )
    except Exception as exc:
        checks.append(
            {"name": "candidate_preflight_passed", "passed": False, "error": str(exc)}
        )
    return checks


def _intent_contract_checks(proposal: Mapping[str, Any]) -> list[dict[str, Any]]:
    contract = proposal.get("intent_contract")
    match contract:
        case Mapping():
            contract_present = True
            contract_data = dict(contract)
        case _:
            contract_present = False
            contract_data = {}
    checks = [
        {
            "name": "intent_contract_present",
            "passed": contract_present,
        }
    ]
    for field in REQUIRED_INTENT_FIELDS:
        value = contract_data.get(field)
        present = field in contract_data
        non_empty = _is_non_empty_contract_value(value) or field == "known_non_goals"
        checks.append(
            {
                "name": f"intent_contract_{field}",
                "passed": present and non_empty,
            }
        )
    return checks


def _judge_checks(
    proposal: Mapping[str, Any], *, require_judge: bool
) -> list[dict[str, Any]]:
    validation = proposal.get("application", {}).get("validation", {})
    judge = (
        proposal.get("judge_validation")
        or proposal.get("application", {}).get("judge_validation")
        or (validation or {}).get("judge_validation")
    )
    if not judge:
        return [
            {
                "name": "judge_validation",
                "passed": not require_judge,
                "status": "skipped",
                "blocking": require_judge,
            }
        ]
    verdict = str((judge or {}).get("verdict") or "").lower()
    return [
        {
            "name": "judge_validation",
            "passed": verdict == "pass",
            "verdict": verdict,
            "blocking": True,
        }
    ]


def _script_static_checks(script_path: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    try:
        py_compile.compile(str(script_path), doraise=True)
        checks.append({"name": "py_compile", "passed": True})
    except Exception as exc:
        checks.append({"name": "py_compile", "passed": False, "error": str(exc)})

    text = script_path.read_text(encoding="utf-8", errors="replace")
    checks.extend(
        [
            {
                "name": "forward_recorder_imported",
                "passed": "get_forward_recorder" in text,
            },
            {
                "name": "forward_run_recorded",
                "passed": "record_run(" in text,
            },
        ]
    )
    return checks


def _scenario_checks(
    script_path: Path, proposal: Mapping[str, Any]
) -> list[dict[str, Any]]:
    scenario_plan = proposal.get("scenario_plan")
    match scenario_plan:
        case list():
            scenarios = scenario_plan
            decision_function = "decide_from_snapshot"
        case Mapping():
            scenarios = scenario_plan.get("scenarios") or []
            decision_function = str(
                scenario_plan.get("decision_function") or "decide_from_snapshot"
            )
        case _:
            return [{"name": "scenario_plan_present", "passed": False}]

    match scenarios:
        case list():
            scenario_count = len(scenarios)
        case _:
            scenario_count = 0
    checks: list[dict[str, Any]] = [
        {
            "name": "scenario_plan_present",
            "passed": bool(scenarios),
            "scenario_count": scenario_count,
        }
    ]
    match scenarios:
        case list() if scenarios:
            pass
        case _:
            return checks

    module = _load_module(script_path)
    fn = getattr(module, decision_function, None) if module is not None else None
    checks.append(
        {
            "name": "decision_function_present",
            "passed": callable(fn),
            "decision_function": decision_function,
        }
    )
    if not callable(fn):
        return checks

    for index, scenario in enumerate(scenarios):
        match scenario:
            case Mapping():
                scenario_data = dict(scenario)
            case _:
                scenario_data = {}
        name = str(scenario_data.get("name") or f"scenario_{index + 1}")
        expected = scenario_data.get("expect") or scenario_data.get("expected") or {}
        try:
            actual = _call_decision_function(
                fn,
                snapshot=scenario_data.get("snapshot") or {},
                state=scenario_data.get("state") or {},
            )
            passed, failures = _compare_expected(actual, expected)
            checks.append(
                {
                    "name": f"scenario_{name}",
                    "passed": passed,
                    "actual": actual,
                    "expected": expected,
                    "failures": failures,
                }
            )
        except Exception as exc:
            checks.append(
                {
                    "name": f"scenario_{name}",
                    "passed": False,
                    "error": str(exc),
                    "expected": expected,
                }
            )
    return checks


def _candidate_script_path(
    *,
    repo_root: Path,
    job_dir: Path,
    candidate_dir: Path,
    job_data: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> Path | None:
    job_id = str(proposal.get("job_id") or job_dir.name)
    return JobStore(repo_root=repo_root).resolve_script_entrypoint(
        job_id,
        job_data,
        candidate_dir=candidate_dir,
    )


def _load_module(script_path: Path) -> Any | None:
    module_name = f"_wayfinder_job_candidate_{abs(hash(script_path))}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    inserted_paths = [str(script_path.parent), str(script_path.parent.parent)]
    old_path = list(sys.path)
    try:
        for item in reversed(inserted_paths):
            if item not in sys.path:
                sys.path.insert(0, item)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path = old_path


def _call_decision_function(fn: Any, *, snapshot: Any, state: Any) -> Any:
    signature = inspect.signature(fn)
    params = list(signature.parameters)
    if any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
    ):
        result = fn(snapshot=snapshot, state=state)
    elif len(params) >= 2:
        result = fn(snapshot, state)
    else:
        result = fn(snapshot)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    return result


def _compare_expected(actual: Any, expected: Any) -> tuple[bool, list[str]]:
    match expected:
        case Mapping():
            expected_data = dict(expected)
        case _:
            return True, []
    failures: list[str] = []
    if "action" in expected_data:
        action = _extract_action(actual)
        if action != expected_data["action"]:
            failures.append(
                f"action expected {expected_data['action']!r}, got {action!r}"
            )
    if "decision" in expected_data:
        action = _extract_action(actual)
        if action != expected_data["decision"]:
            failures.append(
                f"decision expected {expected_data['decision']!r}, got {action!r}"
            )
    if "reason_contains" in expected_data:
        reason = str(_extract_reason(actual)).lower()
        needle = str(expected_data["reason_contains"]).lower()
        if needle not in reason:
            failures.append(f"reason missing {needle!r}: {reason!r}")
    for path, expected_value in dict(expected_data.get("equals") or {}).items():
        actual_value = _dotted_get(actual, str(path))
        if actual_value != expected_value:
            failures.append(f"{path} expected {expected_value!r}, got {actual_value!r}")
    return not failures, failures


def _extract_action(actual: Any) -> Any:
    match actual:
        case str():
            return actual
        case Mapping():
            if "action" in actual:
                return actual.get("action")
            decision = actual.get("decision")
            match decision:
                case Mapping():
                    return decision.get("action")
                case _:
                    return decision
        case _:
            return None


def _extract_reason(actual: Any) -> Any:
    match actual:
        case Mapping():
            if "reason" in actual:
                return actual.get("reason")
            decision = actual.get("decision")
            match decision:
                case Mapping():
                    return decision.get("reason")
                case _:
                    return ""
        case _:
            return ""


def _dotted_get(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        match current:
            case Mapping():
                current = current.get(part)
            case list() if part.isdigit():
                current = current[int(part)]
            case _:
                return None
    return current


def _is_non_empty_contract_value(value: Any) -> bool:
    match value:
        case None:
            return False
        case str():
            return bool(value.strip())
        case list() | tuple() | set() | dict():
            return bool(value)
        case _:
            return True


def validation_summary(validation: Mapping[str, Any]) -> dict[str, Any]:
    checks = validation["checks"]
    return {
        "status": validation["status"],
        "failed_checks": [check["name"] for check in checks if not check["passed"]],
    }


def compact_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, default=str)
