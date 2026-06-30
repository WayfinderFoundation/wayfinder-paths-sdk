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
    script = _script_path(root, job_data, store.repo_root)
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
        params = dict(
            (job_data.get("execution_params") or {})
            if isinstance(job_data, dict)
            else {}
        )
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
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Invalid job.yaml: {path}")
    return loaded


def _load_spec(root: Path, job_data: dict[str, Any]) -> dict[str, Any]:
    embedded = job_data.get("execution_spec")
    if isinstance(embedded, dict) and embedded:
        return embedded
    path = root / "execution_spec.json"
    if path.exists():
        loaded = _load_json(path)
        if isinstance(loaded, dict):
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
            if isinstance(rows, dict):
                rows = rows.get("bars")
            if isinstance(rows, list):
                return PreparedExecutionDataset.from_rows(rows, {"source": str(path)})
    scenario_plan = job_data.get("execution_scenario_plan") or spec.validation.get(
        "execution_scenario_plan"
    )
    scenarios = (
        scenario_plan.get("scenarios") if isinstance(scenario_plan, dict) else None
    )
    if isinstance(scenarios, list) and scenarios:
        rows = scenarios[0].get("bars")
        if isinstance(rows, list):
            return PreparedExecutionDataset.from_rows(
                rows, {"source": "execution_scenario_plan[0]"}
            )
    fixture_bars = spec.validation.get("fixture_bars")
    if isinstance(fixture_bars, list):
        return PreparedExecutionDataset.from_rows(
            fixture_bars, {"source": "execution_spec.validation.fixture_bars"}
        )
    raise FileNotFoundError(
        "No backtest bars found. Provide results/backtest/input_bars.json, "
        "workspace/config/backtest_bars.json, execution_scenario_plan bars, or "
        "execution_spec.validation.fixture_bars."
    )


def _script_path(root: Path, job_data: dict[str, Any], repo_root: Path) -> Path | None:
    script_loop = job_data.get("script_loop") or {}
    if not isinstance(script_loop, dict) or not script_loop.get("enabled"):
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


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
