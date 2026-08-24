"""Advisory, reproducible robustness evidence for jobs-v1 strategies."""

from __future__ import annotations

import hashlib
import itertools
import json
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    _load_strategy,
    run_execution_grid,
    simulate_execution,
)
from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
from wayfinder_paths.jobs.execution.walk_forward import (
    _slice,
    _test_window_stats,
    run_walk_forward,
)
from wayfinder_paths.jobs.gating import (
    compute_workspace_revision,
    evaluate_economic_gate,
)
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.research_contract import RESEARCH_CONTRACT_VERSION
from wayfinder_paths.jobs.store import JobStore

ROBUSTNESS_DIR = "results/research/robustness"
ROBUSTNESS_LATEST = f"{ROBUSTNESS_DIR}/latest.json"
_PLAN_KEYS = frozenset({"neighbors", "phase", "leverage", "walk_forward", "scenarios"})
_STAT_KEYS = (
    "net_return",
    "sharpe",
    "max_drawdown_pct",
    "trade_count",
    "total_fees",
    "total_funding",
    "liquidation_count",
    "exposure_pct",
    "gate_diagnostics",
)


def robustness_check_job(
    job_id: str,
    *,
    candidate_dir: str | Path | None = None,
    robustness_plan: Mapping[str, Any] | None = None,
    store: JobStore | None = None,
) -> dict[str, Any]:
    """Run the requested evidence lanes and persist a compact advisory report."""
    store = store or JobStore()
    job_root = store.job_dir(job_id)
    subject_root = Path(candidate_dir) if candidate_dir else job_root
    job_data = _load_job_yaml(subject_root)
    spec_data, _ = resolve_execution_spec(subject_root, job_data)
    if not spec_data:
        raise FileNotFoundError(f"execution_spec missing for job {job_id}")
    spec = ExecutionSpec.from_dict(spec_data)
    configured_plan = (spec.validation or {}).get("robustness_plan") or {}
    plan = validate_robustness_plan(
        robustness_plan if robustness_plan is not None else configured_plan
    )
    script = store.resolve_script_entrypoint(
        job_id,
        job_data,
        candidate_dir=subject_root if candidate_dir else None,
    )
    if script is None or not script.exists():
        raise FileNotFoundError(f"execution script not found for job {job_id}")
    try:
        dataset = _load_dataset(subject_root, spec, job_data)
    except FileNotFoundError:
        if not candidate_dir:
            raise
        dataset = _load_dataset(job_root, spec, job_data)

    revision = compute_workspace_revision(subject_root)
    dataset_hash = _dataset_hash(dataset)
    plan_hash = _stable_hash(plan)
    artifact_path = f"{ROBUSTNESS_DIR}/{revision}.json"
    existing = store.read_json(job_id, artifact_path)
    if _matches(existing, revision, dataset_hash, plan_hash):
        return {**existing, "reused": True, "artifact": artifact_path}

    params = dict(job_data.get("execution_params") or {})
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "research_contract_version": RESEARCH_CONTRACT_VERSION,
        "job_id": job_id,
        "candidate_revision": revision,
        "dataset_hash": dataset_hash,
        "plan_hash": plan_hash,
        "plan": plan,
        "advisory": True,
        "generated_at": utc_now_iso(),
        "lanes": {},
        "warnings": [],
    }
    lane_errors: list[str] = []
    try:
        subject = simulate_execution(script, dataset, spec, params)
    except Exception as exc:  # noqa: BLE001 - persist a diagnosable failed artifact
        report.update(
            {
                "status": "failed",
                "warnings": [{"code": "baseline_failed", "detail": str(exc)[:300]}],
            }
        )
        return _persist(store, job_id, artifact_path, report)

    warmup_bars = _strategy_warmup_bars(script, params)
    report["lanes"]["subject"] = _compact_stats(subject.stats)
    if candidate_dir:
        _run_incumbent_lane(report, store, job_id, dataset, lane_errors)
    _run_grid_lane(
        report,
        "neighbors",
        script,
        dataset,
        spec,
        _variant_params(params, plan.get("neighbors") or {}, include_base=False),
        lane_errors,
    )
    phase = plan.get("phase") or {}
    phase_axes = {str(phase["param"]): list(phase["values"])} if phase else {}
    _run_grid_lane(
        report,
        "phase",
        script,
        dataset,
        spec,
        _variant_params(params, phase_axes, include_base=False),
        lane_errors,
    )
    leverage = plan.get("leverage") or []
    _run_grid_lane(
        report,
        "leverage",
        script,
        dataset,
        spec,
        (
            _variant_params(params, {"leverage": leverage}, include_base=False)
            if leverage
            else []
        ),
        lane_errors,
    )
    if plan.get("walk_forward"):
        try:
            wf_grid: list[Mapping[str, Any]] = _variant_params(
                params, plan.get("neighbors") or {}, include_base=True
            ) or [params]
            wf_options = dict(plan["walk_forward"])
            wf_options.setdefault("warmup_bars", warmup_bars)
            report["lanes"]["walk_forward"] = run_walk_forward(
                script,
                dataset,
                spec,
                wf_grid,
                rank_by="net_return",
                workers=1,
                parallel="serial",
                **wf_options,
            )
        except Exception as exc:  # noqa: BLE001 - one advisory lane may be partial
            lane_errors.append(f"walk_forward: {exc}")
            report["lanes"]["walk_forward"] = {"status": "error", "error": str(exc)}
    if plan.get("scenarios"):
        report["lanes"]["scenarios"] = _run_scenarios(
            script,
            dataset,
            spec,
            params,
            plan["scenarios"],
            lane_errors,
            warmup_bars=warmup_bars,
        )
    if candidate_dir:
        try:
            report["lanes"]["economic"] = evaluate_economic_gate(
                job_id, candidate_dir=subject_root, store=store
            )
        except Exception as exc:  # noqa: BLE001 - report advisory incompleteness
            lane_errors.append(f"economic: {exc}")
            report["lanes"]["economic"] = {"status": "error", "error": str(exc)}

    report["data_completeness"] = _data_completeness(dataset, spec, params)
    warnings = _warning_codes(report, job_data)
    warnings.extend(
        {"code": "lane_failed", "detail": detail[:300]} for detail in lane_errors
    )
    report["warnings"] = warnings
    report["status"] = (
        "partial"
        if lane_errors or report["data_completeness"].get("status") == "incomplete"
        else "complete"
    )
    return _persist(store, job_id, artifact_path, report)


def validate_robustness_plan(plan: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(plan or {})
    unknown = set(raw) - _PLAN_KEYS
    if unknown:
        raise ValueError(f"unknown robustness plan keys: {sorted(unknown)}")
    neighbors = raw.get("neighbors") or {}
    if not isinstance(neighbors, Mapping) or any(
        not isinstance(values, list) or not values for values in neighbors.values()
    ):
        raise ValueError(
            "robustness neighbors must map parameter names to non-empty lists"
        )
    phase = raw.get("phase") or {}
    if phase and (
        not isinstance(phase, Mapping)
        or not str(phase.get("param") or "").strip()
        or not isinstance(phase.get("values"), list)
        or not phase["values"]
    ):
        raise ValueError("robustness phase requires param and non-empty values")
    leverage = raw.get("leverage") or []
    if not isinstance(leverage, list) or any(float(value) <= 0 for value in leverage):
        raise ValueError("robustness leverage must be a list of positive values")
    walk_forward = raw.get("walk_forward") or {}
    if walk_forward:
        required = {"test_bars", "folds"}
        if not isinstance(walk_forward, Mapping) or not required.issubset(walk_forward):
            raise ValueError("walk_forward requires test_bars and folds")
        if any(int(walk_forward[key]) <= 0 for key in required):
            raise ValueError("walk_forward test_bars and folds must be positive")
        if (
            walk_forward.get("train_bars") is not None
            and int(walk_forward["train_bars"]) <= 0
        ):
            raise ValueError("walk_forward train_bars must be positive when set")
    scenarios = raw.get("scenarios") or []
    if not isinstance(scenarios, list):
        raise ValueError("robustness scenarios must be a list")
    for scenario in scenarios:
        if not isinstance(scenario, Mapping) or not str(scenario.get("name") or ""):
            raise ValueError("each robustness scenario requires a name")
        relative = scenario.get("lookback_days") is not None
        explicit = scenario.get("start") is not None or scenario.get("end") is not None
        if relative == explicit or (
            explicit and not {"start", "end"}.issubset(scenario)
        ):
            raise ValueError(
                f"scenario {scenario['name']!r} requires either lookback_days or start+end"
            )
        if scenario.get("role", "development") not in {"development", "audit"}:
            raise ValueError("scenario role must be development or audit")
        if relative and float(scenario["lookback_days"]) <= 0:
            raise ValueError("scenario lookback_days must be positive")
    return raw


def latest_robustness_summary(
    store: JobStore,
    job_id: str,
    *,
    candidate_revision: str,
    candidate_dir: Path,
) -> dict[str, Any]:
    artifact_path = f"{ROBUSTNESS_DIR}/{candidate_revision}.json"
    artifact = store.read_json(job_id, artifact_path)
    if (
        not artifact
        or artifact.get("candidate_revision") != candidate_revision
        or artifact.get("research_contract_version") != RESEARCH_CONTRACT_VERSION
    ):
        return {"status": "not_run", "advisory": True}
    try:
        job_data = _load_job_yaml(candidate_dir)
        spec_data, _ = resolve_execution_spec(candidate_dir, job_data)
        if not spec_data:
            raise FileNotFoundError("execution spec missing")
        spec = ExecutionSpec.from_dict(spec_data)
        try:
            dataset = _load_dataset(candidate_dir, spec, job_data)
        except FileNotFoundError:
            dataset = _load_dataset(store.job_dir(job_id), spec, job_data)
    except (FileNotFoundError, ValueError):
        return {"status": "not_run", "advisory": True}
    if artifact.get("dataset_hash") != _dataset_hash(dataset):
        return {"status": "not_run", "advisory": True, "reason": "stale_dataset"}
    return {
        "status": artifact.get("status"),
        "advisory": True,
        "warnings": artifact.get("warnings") or [],
        "artifact": artifact.get("artifact") or artifact_path,
        "research_contract_version": artifact.get("research_contract_version"),
    }


def _run_incumbent_lane(
    report: dict[str, Any],
    store: JobStore,
    job_id: str,
    dataset: PreparedExecutionDataset,
    errors: list[str],
) -> None:
    try:
        data = _load_job_yaml(store.job_dir(job_id))
        spec_data, _ = resolve_execution_spec(store.job_dir(job_id), data)
        if not spec_data:
            raise FileNotFoundError("incumbent execution spec missing")
        incumbent_spec = ExecutionSpec.from_dict(spec_data)
        script = store.resolve_script_entrypoint(job_id, data)
        if script is None:
            raise FileNotFoundError("incumbent script missing")
        result = simulate_execution(
            script, dataset, incumbent_spec, data.get("execution_params") or {}
        )
        report["lanes"]["incumbent"] = _compact_stats(result.stats)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"incumbent: {exc}")


def _run_grid_lane(
    report: dict[str, Any],
    name: str,
    script: Path,
    dataset: PreparedExecutionDataset,
    spec: ExecutionSpec,
    variants: list[Mapping[str, Any]],
    errors: list[str],
) -> None:
    if not variants:
        return
    try:
        grid = run_execution_grid(
            script,
            dataset,
            spec,
            variants,
            rank_by="net_return",
            workers=1,
            parallel="serial",
        )
        report["lanes"][name] = {
            "status": "ok",
            "runs": [
                {
                    "params": row["params"],
                    "stats": _compact_stats(row["stats"]),
                    "execution_valid": bool(row["validation"]["execution_valid"]),
                }
                for row in grid.runs
            ],
        }
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{name}: {exc}")
        report["lanes"][name] = {"status": "error", "error": str(exc)}


def _run_scenarios(
    script: Path,
    dataset: PreparedExecutionDataset,
    spec: ExecutionSpec,
    params: dict[str, Any],
    scenarios: Sequence[Mapping[str, Any]],
    errors: list[str],
    *,
    warmup_bars: int,
) -> list[dict[str, Any]]:
    timestamps = dataset.bars.timestamps
    output: list[dict[str, Any]] = []
    for scenario in scenarios:
        try:
            if scenario.get("lookback_days") is not None:
                start_at = timestamps[-1] - pd.Timedelta(
                    days=float(scenario["lookback_days"])
                )
                end_at = timestamps[-1]
            else:
                start_at = pd.Timestamp(str(scenario["start"]))
                end_at = pd.Timestamp(str(scenario["end"]))
                start_at = (
                    start_at.tz_localize("UTC")
                    if start_at.tzinfo is None
                    else start_at.tz_convert("UTC")
                )
                end_at = (
                    end_at.tz_localize("UTC")
                    if end_at.tzinfo is None
                    else end_at.tz_convert("UTC")
                )
            selected = [
                i for i, stamp in enumerate(timestamps) if start_at <= stamp <= end_at
            ]
            if not selected:
                raise ValueError("scenario window has no bars")
            start, end = selected[0], selected[-1] + 1
            evaluation = _slice(dataset, timestamps, max(0, start - warmup_bars), end)
            result = simulate_execution(script, evaluation, spec, params)
            stats = _test_window_stats(result, timestamps[start], spec, params)
            output.append(
                {
                    "name": scenario["name"],
                    "role": scenario.get("role", "development"),
                    "start": str(timestamps[start]),
                    "end": str(timestamps[end - 1]),
                    "stats": _compact_stats(stats),
                }
            )
        except Exception as exc:  # noqa: BLE001
            detail = f"scenario {scenario.get('name')}: {exc}"
            errors.append(detail)
            output.append(
                {"name": scenario.get("name"), "status": "error", "error": str(exc)}
            )
    return output


def _strategy_warmup_bars(script: Path, params: dict[str, Any]) -> int:
    configured = int(params.get("lookback_bars") or 0)
    strategy = _load_strategy(script, params)
    declared = int(getattr(strategy, "warmup_bars", 60) or 60)
    return max(1, configured, declared)


def _variant_params(
    base: Mapping[str, Any],
    axes: Mapping[str, Sequence[Any]],
    *,
    include_base: bool,
) -> list[Mapping[str, Any]]:
    if not axes:
        return []
    keys = list(axes)
    variants: list[Mapping[str, Any]] = [
        {**dict(base), **dict(zip(keys, values, strict=True))}
        for values in itertools.product(*(axes[key] for key in keys))
    ]
    if include_base:
        return variants
    return [
        variant
        for variant in variants
        if any(variant.get(key) != base.get(key) for key in keys)
    ]


def _compact_stats(stats: Mapping[str, Any]) -> dict[str, Any]:
    return {key: stats.get(key) for key in _STAT_KEYS if key in stats}


def _data_completeness(
    dataset: PreparedExecutionDataset, spec: ExecutionSpec, params: Mapping[str, Any]
) -> dict[str, Any]:
    if spec.market_kind != "perp":
        return {"status": "not_applicable"}
    symbols = [str(value) for value in params.get("symbols") or dataset.bars.symbols]
    funded = {
        event.symbol for event in dataset.market_events if event.kind == "funding"
    }
    symbol_fraction = (
        len(funded.intersection(symbols)) / len(symbols) if symbols else 0.0
    )
    coverage = (dataset.metadata.get("feature_coverage") or {}).get("funding") or {}
    span_fraction = float(coverage.get("coverage_fraction") or 0.0)
    complete = min(symbol_fraction, span_fraction) >= 0.9
    return {
        "status": "complete" if complete else "incomplete",
        "funding_symbol_fraction": round(symbol_fraction, 3),
        "funding_span_fraction": round(span_fraction, 3),
        "remediation": (
            None
            if complete
            else "fetch funding for the same venue, symbols, and days as the candle dataset"
        ),
    }


def _warning_codes(
    report: Mapping[str, Any], job_data: Mapping[str, Any]
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    if (report.get("data_completeness") or {}).get("status") == "incomplete":
        warnings.append(
            {"code": "funding_incomplete", "detail": "funding coverage is below 90%"}
        )
    gates = ((report.get("lanes") or {}).get("subject") or {}).get(
        "gate_diagnostics"
    ) or {}
    for name, gate in gates.items():
        scope = gate.get("scope")
        portfolio_unobserved = (
            scope == "portfolio" and int(gate.get("active_bars") or 0) == 0
        )
        symbol_unobserved = scope == "symbol" and not any(
            int(row.get("active_bars") or 0)
            for row in (gate.get("symbols") or {}).values()
        )
        if portfolio_unobserved or symbol_unobserved:
            warnings.append(
                {"code": "gate_unobserved", "detail": f"{name} never activated"}
            )
    subject_return = float(
        ((report.get("lanes") or {}).get("subject") or {}).get("net_return") or 0.0
    )
    phase_returns = [subject_return, *_lane_returns(report, "phase")]
    if phase_returns and min(phase_returns) <= 0 < max(phase_returns):
        warnings.append(
            {
                "code": "phase_sensitivity",
                "detail": "after-cost return changes sign across rebalance phases",
            }
        )
    neighbor_returns = _immediate_neighbor_returns(report, job_data)
    if subject_return > 0 and any(value <= 0 for value in neighbor_returns):
        warnings.append(
            {
                "code": "isolated_neighbor",
                "detail": "an immediate parameter neighbor has nonpositive return",
            }
        )
    wf = (report.get("lanes") or {}).get("walk_forward") or {}
    oos = [
        float(row["test_stats"]["net_return"])
        for row in wf.get("folds") or []
        if row.get("status") == "ok"
    ]
    if subject_return > 0 and oos and statistics.median(oos) <= 0:
        warnings.append(
            {"code": "oos_decay", "detail": "median walk-forward return is nonpositive"}
        )
    risk_limit = (
        ((job_data.get("controller") or {}).get("starter") or {}).get("risk_limits")
        or {}
    ).get("max_drawdown")
    leverage_rows = [
        {"stats": (report.get("lanes") or {}).get("subject") or {}},
        *(((report.get("lanes") or {}).get("leverage") or {}).get("runs") or []),
    ]
    for row in leverage_rows:
        stats = row.get("stats") or {}
        if int(stats.get("liquidation_count") or 0) or (
            risk_limit is not None
            and float(stats.get("max_drawdown_pct") or 0.0) < float(risk_limit)
        ):
            warnings.append(
                {
                    "code": "leverage_risk",
                    "detail": "a leverage lane breaches liquidation or owner drawdown limits",
                }
            )
            break
    for scenario in (report.get("lanes") or {}).get("scenarios") or []:
        if float((scenario.get("stats") or {}).get("net_return") or 0.0) < 0:
            warnings.append(
                {
                    "code": "scenario_loss",
                    "detail": f"scenario {scenario.get('name')} has negative return",
                }
            )
    delta = (
        ((report.get("lanes") or {}).get("economic") or {}).get(
            "paired_incumbent_delta"
        )
        or {}
    ).get("lcb")
    if delta is not None and float(delta) <= 0:
        warnings.append(
            {
                "code": "negative_paired_delta",
                "detail": "paired economic utility LCB is nonpositive",
            }
        )
    return warnings


def _lane_returns(report: Mapping[str, Any], name: str) -> list[float]:
    return [
        float((row.get("stats") or {}).get("net_return") or 0.0)
        for row in (((report.get("lanes") or {}).get(name) or {}).get("runs") or [])
    ]


def _immediate_neighbor_returns(
    report: Mapping[str, Any], job_data: Mapping[str, Any]
) -> list[float]:
    plan = report.get("plan") or {}
    base = job_data.get("execution_params") or {}
    rows = ((report.get("lanes") or {}).get("neighbors") or {}).get("runs") or []
    values: list[float] = []
    for parameter, axis in (plan.get("neighbors") or {}).items():
        if base.get(parameter) not in axis:
            continue
        index = axis.index(base[parameter])
        adjacent = {axis[i] for i in (index - 1, index + 1) if 0 <= i < len(axis)}
        for row in rows:
            if (row.get("params") or {}).get(parameter) in adjacent:
                values.append(float((row.get("stats") or {}).get("net_return") or 0.0))
    return values


def _dataset_hash(dataset: PreparedExecutionDataset) -> str:
    digest = hashlib.sha256()
    digest.update(
        pd.util.hash_pandas_object(
            dataset.bars.to_frame(), index=False
        ).values.tobytes()
    )
    digest.update(
        json.dumps(
            [event.to_dict() for event in dataset.market_events], sort_keys=True
        ).encode()
    )
    return digest.hexdigest()


def _stable_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _matches(
    artifact: Mapping[str, Any] | None,
    revision: str,
    dataset_hash: str,
    plan_hash: str,
) -> bool:
    return bool(
        artifact
        and artifact.get("status") in {"complete", "partial"}
        and artifact.get("research_contract_version") == RESEARCH_CONTRACT_VERSION
        and artifact.get("candidate_revision") == revision
        and artifact.get("dataset_hash") == dataset_hash
        and artifact.get("plan_hash") == plan_hash
    )


def _persist(
    store: JobStore, job_id: str, artifact_path: str, report: dict[str, Any]
) -> dict[str, Any]:
    report["artifact"] = artifact_path
    store.write_json(job_id, artifact_path, report)
    store.write_json(
        job_id,
        ROBUSTNESS_LATEST,
        {
            key: report.get(key)
            for key in (
                "schema_version",
                "research_contract_version",
                "job_id",
                "candidate_revision",
                "dataset_hash",
                "plan_hash",
                "status",
                "advisory",
                "generated_at",
                "warnings",
                "artifact",
            )
        },
    )
    return report
