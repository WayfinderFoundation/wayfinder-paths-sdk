from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    run_execution_grid,
    simulate_execution,
    write_backtest_artifacts,
)
from wayfinder_paths.jobs.execution.validation import validate_execution_job
from wayfinder_paths.jobs.store import JobStore


def backtest_execution_job(
    job_id: str,
    *,
    grid_path: str | Path | None = None,
    workers: int = 1,
    parallel: str = "serial",
    store: JobStore | None = None,
) -> dict[str, Any]:
    store = store or JobStore()
    root = store.job_dir(job_id)
    job_data = _load_job_yaml(root)
    spec = ExecutionSpec.from_dict(_load_spec(root, job_data))
    script = store.resolve_script_entrypoint(job_id, job_data)
    if script is None or not script.exists():
        raise FileNotFoundError(
            f"Execution script not found for job {job_id}: {script}"
        )
    dataset = _load_dataset(root, spec, job_data)
    output_dir = root / "results" / "backtest"
    if grid_path:
        param_grid = _load_json(Path(grid_path))
        result = run_execution_grid(
            script,
            dataset,
            spec,
            param_grid,
            workers=workers,
            parallel=parallel,
        )
        grid_dir = output_dir / "grids" / result.grid_id
        artifacts = write_backtest_artifacts(result, grid_dir)
        payload = {"type": "grid", "result": result.to_dict(), "artifacts": artifacts}
    else:
        params = dict(job_data.get("execution_params") or {})
        result = simulate_execution(script, dataset, spec, params)
        artifacts = write_backtest_artifacts(result, output_dir)
        payload = {"type": "single", "result": result.to_dict(), "artifacts": artifacts}
    validation = validate_execution_job(job_id, store=store)
    payload["validation"] = validation
    return payload


def validate_job(
    job_id: str, *, strict: bool = False, store: JobStore | None = None
) -> dict[str, Any]:
    return validate_execution_job(job_id, strict=strict, store=store)


def _load_job_yaml(root: Path) -> dict[str, Any]:
    path = root / "job.yaml"
    if not path.exists():
        raise FileNotFoundError(f"job.yaml not found: {path}")
    match yaml.safe_load(path.read_text(encoding="utf-8")) or {}:
        case dict() as loaded:
            return loaded
        case _:
            raise ValueError(f"Invalid job.yaml: {path}")


def _load_spec(root: Path, job_data: dict[str, Any]) -> dict[str, Any]:
    match job_data.get("execution_spec"):
        case dict() as embedded if embedded:
            return embedded
    path = root / "execution_spec.json"
    if path.exists():
        match _load_json(path):
            case dict() as loaded:
                return loaded
    raise FileNotFoundError(
        f"execution_spec missing for job {job_data.get('id') or root.name}"
    )


def _load_dataset(
    root: Path, spec: ExecutionSpec, job_data: dict[str, Any]
) -> PreparedExecutionDataset:
    candidate_paths = [
        root / "results" / "backtest" / "input_bars.json",
        root / "workspace" / "config" / "backtest_bars.json",
    ]
    for path in candidate_paths:
        if path.exists():
            rows = _load_json(path)
            match rows:
                case dict():
                    rows = rows.get("bars")
            match rows:
                case list():
                    return PreparedExecutionDataset.from_rows(
                        rows, {"source": str(path)}
                    )
    scenario_plan = job_data.get("execution_scenario_plan") or spec.validation.get(
        "execution_scenario_plan"
    )
    scenarios = None
    match scenario_plan:
        case dict():
            scenarios = scenario_plan.get("scenarios")
    match scenarios:
        case [first, *_]:
            match first.get("bars"):
                case list() as rows:
                    return PreparedExecutionDataset.from_rows(
                        rows, {"source": "execution_scenario_plan[0]"}
                    )
    match spec.validation.get("fixture_bars"):
        case list() as fixture_bars:
            return PreparedExecutionDataset.from_rows(
                fixture_bars, {"source": "execution_spec.validation.fixture_bars"}
            )
    raise FileNotFoundError(
        "No backtest bars found. Provide results/backtest/input_bars.json, "
        "workspace/config/backtest_bars.json, execution_scenario_plan bars, or "
        "execution_spec.validation.fixture_bars."
    )


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
