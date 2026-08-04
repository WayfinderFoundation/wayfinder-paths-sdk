from __future__ import annotations

import io
import json
import py_compile
import re
import tokenize
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from croniter import croniter

from wayfinder_paths.jobs.execution.features import (
    load_feature_rows,
    parse_feature_specs,
)
from wayfinder_paths.jobs.execution.primitives import (
    ExecutionSpec,
    _load_module_from_path,
    bar_interval_seconds,
)
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.schedule import normalize_schedule

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
        event["timestamp"] for event in guard_events if event["kind"] == "stale_data"
    }
    stale_entries = [
        fill
        for fill in trace["fills"]
        if fill["timestamp"] in stale_timestamps
        and fill["status"] in {"filled", "partial"}
        and not fill["reduce_only"]
    ]
    state_valid = not stale_entries
    if stale_entries:
        issues.append("position-opening fills executed against stale market data")

    rejected = [event for event in guard_events if event["kind"] == "intent_rejected"]
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
    checks.append(entrypoint_inside_workspace_check(root, script_path))
    if script_path and script_path.exists():
        checks.extend(_script_static_checks(script_path, spec))
        try:
            module = _load_module_from_path(script_path)
            checks.append({"name": "strategy_module_loads", "passed": True})
            checks.append(
                {
                    "name": "strategy_entrypoint_present",
                    "passed": callable(getattr(module, "build_strategy", None))
                    or callable(getattr(module, "decide", None)),
                }
            )
        except Exception as exc:
            checks.append(
                {"name": "strategy_module_loads", "passed": False, "error": str(exc)}
            )
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
    checks.extend(_evidence_window_check(root))
    checks.extend(_live_wallet_checks(job_data))

    report = _report(checks, strict=strict or spec.strict)
    report["revision"] = compute_workspace_revision(root)
    if not candidate_dir:
        store.write_json(job_id, "reports/validation/latest.json", report)
    return report


# Evidence-window policy (owner-set 2026-07-27): a 5m strategy validated on
# a 14-day window backtested +21% at deploy and ran -41bps/trade forward —
# window-local noise survived every multiplicity gate because none of them
# question the window itself. Force long history when it exists; the 30d
# floor applies ONLY with proof of unavailability (the full target was
# requested and the source could not supply it — e.g. a new listing).
EVIDENCE_TARGET_DAYS = 120.0
EVIDENCE_FLOOR_DAYS = 30.0


def _evidence_window_check(root: Path) -> list[dict[str, Any]]:
    bars_path = root / "results" / "backtest" / "input_bars.json"
    if not bars_path.exists():
        return []  # fixture/scenario-driven validation contexts
    try:
        metadata = json.loads(bars_path.read_text(encoding="utf-8")).get("metadata")
    except ValueError:
        metadata = None
    if not isinstance(metadata, dict) or metadata.get("days") is None:
        return [
            {
                "name": "evidence_window",
                "passed": True,
                "blocking": False,
                "note": (
                    "dataset provenance unknown (hand-written bars) — window "
                    "policy not evaluable; prefer fetch-dataset so the window "
                    "is auditable"
                ),
            }
        ]
    requested = float(metadata.get("days") or 0.0)
    received = float(metadata.get("days_received") or requested)
    if received >= 0.9 * EVIDENCE_TARGET_DAYS:
        return [
            {
                "name": "evidence_window",
                "passed": True,
                "tier": "long_history",
                "days_received": received,
            }
        ]
    if requested < EVIDENCE_TARGET_DAYS:
        # A short window is only excusable with PROOF the data does not
        # exist — and proof requires having asked for the full target.
        return [
            {
                "name": "evidence_window",
                "passed": False,
                "days_received": received,
                "error": (
                    f"dataset spans {received:g}d but only {requested:g}d was "
                    f"requested — request the full target first: fetch-dataset "
                    f"--days {EVIDENCE_TARGET_DAYS:g} --source ccxt. The "
                    f"{EVIDENCE_FLOOR_DAYS:g}d floor applies only when the "
                    "full target was requested and the source could not "
                    "supply it (new listing)."
                ),
            }
        ]
    source = str(metadata.get("source") or "")
    if source != "ccxt":
        # A VENUE shortfall proves nothing — venue feeds cap at days of
        # history while the ccxt path has years. This exact hole let an
        # Aug 2 default-source refetch replace the 120d ccxt dataset with
        # 40d of venue data and still pass the gate as "proven".
        return [
            {
                "name": "evidence_window",
                "passed": False,
                "days_received": received,
                "error": (
                    f"dataset spans {received:g}d from source {source!r} — a "
                    "venue-capped shortfall is NOT proof of unavailability. "
                    f"Refetch via the long-history path: fetch-dataset --days "
                    f"{EVIDENCE_TARGET_DAYS:g} --source ccxt. Only a ccxt "
                    "shortfall counts as proven (new listing)."
                ),
            }
        ]
    if received >= EVIDENCE_FLOOR_DAYS:
        return [
            {
                "name": "evidence_window",
                "passed": True,
                "tier": "short_history_proven",
                "days_received": received,
                "note": (
                    f"{received:g}d received of {requested:g}d requested — the "
                    "long-history source could not supply the target (new "
                    "symbol); the 30d floor applies. Evidence from this window "
                    "is short-history: prefer probation sizing and re-validate "
                    "as history grows."
                ),
            }
        ]
    return [
        {
            "name": "evidence_window",
            "passed": False,
            "days_received": received,
            "error": (
                f"only {received:g}d of history exists (full target "
                "requested) — below the "
                f"{EVIDENCE_FLOOR_DAYS:g}d floor; too new to validate any "
                "deployment evidence"
            ),
        }
    ]


def _feature_checks(root: Path, spec: ExecutionSpec) -> list[dict[str, Any]]:
    try:
        specs = parse_feature_specs(spec)
    except ValueError as exc:
        # A malformed feature schema is a spec error: blocking.
        return [{"name": "declared_features_valid", "passed": False, "error": str(exc)}]
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


def _live_wallet_checks(job_data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Live execution signs with `execution_params.wallet_label`; the engine
    default is 'main', which rarely exists on hosted instances — a live job
    without an explicit label starts cleanly and then fails EVERY tick with
    'Wallet not found: main' (observed live: three config guesses across two
    sessions before the nested key was found). Blocking when mode is live."""
    if str(job_data.get("execution_contract") or "legacy") != "jobs_v1":
        return []
    script_loop = job_data.get("script_loop") or {}
    if str(script_loop.get("mode") or "paper") != "live":
        return []
    params = job_data.get("execution_params") or {}
    label = str(params.get("wallet_label") or "").strip()
    return [
        {
            "name": "wallet_label_declared",
            "passed": bool(label),
            "blocking": True,
            "hint": (
                "live mode signs with execution_params.wallet_label (the "
                "engine default 'main' rarely exists on this instance) — set "
                "it to a label from core_get_wallets() before going live; it "
                "is NOT a job-root key, an env var, or an adapter config file"
            ),
            "details": {"wallet_label": label or None},
        }
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
    params = job_data.get("execution_params") or {}
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
            "passed": bool(params.get("initial_capital")) or not is_jobs_v1,
            "value": params.get("initial_capital"),
            "blocking": False,
        },
        {
            # The live driver always fetches a bounded window (default 200
            # bars); an undeclared lookback means backtests see full history
            # while live sees 200 — path-dependent indicators (Wilder ATR,
            # SuperTrend) will diverge. Declaring it aligns both AND bounds
            # per-tick backtest cost.
            "name": "lookback_bars_declared",
            "passed": bool(params.get("lookback_bars")) or not is_jobs_v1,
            "value": params.get("lookback_bars"),
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

    if schedule.kind == "interval":
        period = schedule.interval_seconds
    elif schedule.cron_expr:
        iterator = croniter(schedule.cron_expr, 0)
        fires = [iterator.get_next(float) for _ in range(4)]
        gaps = sorted(b - a for a, b in zip(fires, fires[1:], strict=False))
        period = int(gaps[len(gaps) // 2])  # median gap
    else:
        period = None
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


def entrypoint_inside_workspace_check(
    root: Path, script_path: Path | None
) -> dict[str, Any]:
    """Blocking: strategy code has exactly one durable, versionable home.

    Revisions hash only workspace/* + job.yaml and proposals stage only
    workspace/, so an entrypoint anywhere else can never be versioned,
    staged, or promoted (and may not even survive an image update).
    """
    workspace = (root / "workspace").resolve()
    passed = bool(
        script_path is not None and script_path.resolve().is_relative_to(workspace)
    )
    check: dict[str, Any] = {
        "name": "entrypoint_inside_workspace",
        "passed": passed,
        "blocking": True,
        "path": str(script_path) if script_path else None,
        "expected_dir": str(root / "workspace" / "src"),
    }
    if not passed:
        check["hint"] = (
            "move the strategy into <job>/workspace/src/ and set "
            "script_loop.entrypoint to 'workspace/src/<file>.py' — revisions "
            "hash only workspace/* + job.yaml and proposals stage only "
            "workspace/, so code elsewhere cannot be versioned or promoted"
        )
    return check


def _code_only_text(text: str) -> str:
    """Strip comments and docstrings so static greps see only real code.

    Falls back to the raw text on tokenize failures — those scripts already
    fail execution_script_py_compile with the real error.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except Exception:
        return text
    keep: list[str] = []
    prev_significant = tokenize.INDENT
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and prev_significant in (
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.NEWLINE,
        ):
            # Expression-statement string == docstring; drop it.
            prev_significant = tok.type
            continue
        if tok.type not in (tokenize.NL, tokenize.NEWLINE):
            prev_significant = tok.type
        elif tok.type == tokenize.NEWLINE:
            prev_significant = tokenize.NEWLINE
        keep.append(tok.string)
    return " ".join(keep)


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
    # Comments and docstrings must neither trip this check ("# time stop:
    # close if held > N days") nor rescue it (a comment saying BracketEngine).
    code_text = _code_only_text(text)
    # Boot-relative warmup/cadence counters go dark for a full warmup period
    # after every state reset and never fire correctly in live's sliding
    # window — the live funding-carry job sat 27 days from one of these.
    counter_gate = re.search(
        r"strategy_state\s*(?:\.\s*get\s*\(|\[)\s*[\"'](?:bar|tick)_?count",
        code_text,
    )
    checks.append(
        {
            "name": "no_boot_relative_warmup",
            "passed": counter_gate is None,
            "blocking": False,
            "hint": (
                "gate warmup on ctx.bar_index (data available in the view) and "
                "cadence on ctx.every_n_bars(n) — tick counters in "
                "strategy_state re-warm from zero on every state reset"
            )
            if counter_gate is not None
            else None,
        }
    )
    close_stop_pattern = re.search(
        r"(stop|take_profit|tp).*close", code_text, re.IGNORECASE
    )
    # Intent bracket dicts ({"bracket": {"stop_loss": ...}}) delegate stop
    # evaluation to the engine, which honors ohlc_rules (intrabar highs/lows)
    # — pricing the level off a close is then correct, not a close-only stop.
    # Without this escape hatch `"stop_loss": current_close * 0.98` inside a
    # bracket trips the regex and agents contort strategy code to appease it.
    bracket_delegation = re.search(r"[\"']bracket[\"']\s*:", code_text)
    checks.append(
        {
            "name": "no_close_only_stop_tp",
            "passed": close_stop_pattern is None
            or "BracketEngine" in code_text
            or "ohlc_" in code_text
            or bracket_delegation is not None,
        }
    )
    return checks


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
            trades = result.trades
            passed = (
                (
                    "min_trades" not in expected
                    or len(trades) >= int(expected["min_trades"])
                )
                and (
                    "max_trades" not in expected
                    or len(trades) <= int(expected["max_trades"])
                )
                and (
                    "execution_valid" not in expected
                    or bool(result.validation["execution_valid"])
                    is bool(expected["execution_valid"])
                )
            )
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
