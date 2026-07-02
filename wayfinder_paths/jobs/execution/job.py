from __future__ import annotations

import json
from collections.abc import Mapping
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
from wayfinder_paths.jobs.execution.validation import (
    resolve_execution_spec,
    validate_execution_job,
)
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore


def backtest_execution_job(
    job_id: str,
    *,
    grid_path: str | Path | None = None,
    workers: int = 1,
    parallel: str = "serial",
    rank_by: str = "net_return",
    walk_forward: Mapping[str, Any] | None = None,
    optimizer: str = "grid",
    optuna_options: Mapping[str, Any] | None = None,
    store: JobStore | None = None,
) -> dict[str, Any]:
    store = store or JobStore()
    root = store.job_dir(job_id)
    job_data = _load_job_yaml(root)
    spec_data, _ = resolve_execution_spec(root, job_data)
    if not spec_data:
        raise FileNotFoundError(
            f"execution_spec missing for job {job_data.get('id') or root.name}"
        )
    spec = ExecutionSpec.from_dict(spec_data)
    script = store.resolve_script_entrypoint(job_id, job_data)
    if script is None or not script.exists():
        raise FileNotFoundError(
            f"Execution script not found for job {job_id}: {script}"
        )
    dataset = _load_dataset(root, spec, job_data)
    output_dir = root / "results" / "backtest"
    stamp = {
        "revision": compute_workspace_revision(root),
        "generated_at": utc_now_iso(),
        "dataset": dict(dataset.metadata),
    }
    if walk_forward is not None and not grid_path:
        raise ValueError("walk_forward requires a grid (pass grid_path)")
    if optimizer not in {"grid", "optuna"}:
        raise ValueError(f"optimizer must be grid or optuna, got {optimizer!r}")
    if optimizer == "optuna" and not grid_path:
        raise ValueError("optimizer=optuna requires a search space (pass grid_path)")
    if grid_path:
        from wayfinder_paths.jobs.execution.optimize import (
            is_search_space,
            run_optuna_search,
        )

        param_grid = _load_json(Path(grid_path))
        if optimizer == "grid" and is_search_space(param_grid):
            raise ValueError(
                "the grid file looks like an optuna search space (typed "
                'dimensions with {"type": ...}); pass --optimizer optuna or '
                "provide a dict-of-lists grid"
            )
        if optimizer == "optuna":
            result = run_optuna_search(
                script,
                dataset,
                spec,
                param_grid,
                rank_by=rank_by,
                **dict(optuna_options or {}),
            )
        else:
            result = run_execution_grid(
                script,
                dataset,
                spec,
                param_grid,
                workers=workers,
                parallel=parallel,
                rank_by=rank_by,
            )
        grid_dir = output_dir / "grids" / result.grid_id
        artifacts = write_backtest_artifacts(result, grid_dir, extra=stamp)
        payload = {
            "type": "grid",
            "result": result.to_dict(),
            "artifacts": artifacts,
            **stamp,
        }
        if walk_forward is not None:
            from wayfinder_paths.jobs.execution.walk_forward import run_walk_forward

            payload["walk_forward"] = run_walk_forward(
                script,
                dataset,
                spec,
                param_grid,
                rank_by=rank_by,
                workers=workers,
                parallel=parallel,
                optimizer=optimizer,
                optuna_options=optuna_options,
                **dict(walk_forward),
            )
    else:
        params = job_data.get("execution_params") or {}
        result = simulate_execution(script, dataset, spec, params)
        artifacts = write_backtest_artifacts(result, output_dir, extra=stamp)
        payload = {
            "type": "single",
            "result": result.to_dict(),
            "artifacts": artifacts,
            **stamp,
        }
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
