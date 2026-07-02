"""Optuna parameter search that speaks the grid contract.

`run_optuna_search` is a drop-in sibling of `run_execution_grid`: it returns
the same `ExecutionGridResult` shape, so experiments.jsonl recording, grid
artifacts (`grids/<id>/summary.json`), `promote-params`, and walk-forward all
consume it unchanged.

Search-space file format (the `--grid` file doubles as the space — the two
formats are self-distinguishing):

    grid:   {"threshold": [1, 2, 3]}                       # dict of lists
    space:  {"threshold": {"type": "float", "low": 0.5, "high": 5.0},
             "symbol": "SNX"}                              # constants pass through

Typed dimensions: {"type": "float"|"int"|"categorical", "low", "high",
"step"?, "log"?, "choices"?}. Every non-dict value (or dict without "type")
is merged into every trial as a constant.

Trials run SERIALLY by design (n_jobs=1): the purity sandbox monkey-patches
process-global time/random hooks per decide() call, so concurrent trials
would race on those patches — and a seeded TPESampler is only reproducible
when trials complete in order. `direction="maximize"` is uniformly correct:
grid ranking sorts descending on every GRID_RANK_KEYS metric.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.execution.simulator import (
    GRID_RANK_KEYS,
    ExecutionGridResult,
    PreparedExecutionDataset,
    _grid_row,
    simulate_execution,
)

_SUGGEST_TYPES = {"float", "int", "categorical"}


def is_search_space(payload: Any) -> bool:
    """True when the payload contains at least one typed search dimension."""
    if not isinstance(payload, Mapping):
        return False
    return any(
        isinstance(value, Mapping) and value.get("type") in _SUGGEST_TYPES
        for value in payload.values()
    )


def run_optuna_search(
    script_entrypoint: str | Path,
    dataset: PreparedExecutionDataset,
    execution_spec: ExecutionSpec | Mapping[str, Any] | None,
    search_space: Mapping[str, Any],
    *,
    rank_by: str = "net_return",
    n_trials: int = 50,
    seed: int = 42,
    timeout: float | None = None,
    sampler: str = "tpe",
    top_n_artifacts: int = 10,
) -> ExecutionGridResult:
    try:
        import optuna
    except ImportError as exc:
        raise RuntimeError(
            'optuna is required for optimizer="optuna"; '
            "run `poetry install --with ml`"
        ) from exc

    if rank_by not in GRID_RANK_KEYS:
        raise ValueError(
            f"rank_by must be one of {sorted(GRID_RANK_KEYS)}, got {rank_by!r}"
        )
    dimensions = {
        name: dict(value)
        for name, value in search_space.items()
        if isinstance(value, Mapping) and value.get("type") in _SUGGEST_TYPES
    }
    if not dimensions:
        raise ValueError(
            "optuna search space has no typed dimensions; each searched param "
            'needs {"type": "float"|"int"|"categorical", ...} (constants pass '
            "through unchanged)"
        )
    constants = {
        name: value for name, value in search_space.items() if name not in dimensions
    }

    run_rows: list[dict[str, Any]] = []

    def objective(trial: Any) -> float:
        params = dict(constants)
        for name, dim in dimensions.items():
            params[name] = _suggest(trial, name, dim)
        result = simulate_execution(
            script_entrypoint, dataset, execution_spec, params
        )
        row = _grid_row(result, rank_by=rank_by)
        row["trial"] = trial.number
        run_rows.append(row)
        if row["validation"]["execution_valid"] is not True:
            raise optuna.TrialPruned("execution trace invalid for these params")
        return float(row[rank_by] or 0)  # same coercion as grid ranking

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    match sampler:
        case "tpe":
            sampler_impl = optuna.samplers.TPESampler(seed=seed)
        case "random":
            sampler_impl = optuna.samplers.RandomSampler(seed=seed)
        case _:
            raise ValueError(f"sampler must be tpe or random, got {sampler!r}")
    study = optuna.create_study(direction="maximize", sampler=sampler_impl)
    # n_jobs=1 is REQUIRED (see module docstring), not a performance choice.
    study.optimize(objective, n_trials=n_trials, timeout=timeout, n_jobs=1)

    valid = [row for row in run_rows if row["validation"]["execution_valid"] is True]
    invalid = [
        row for row in run_rows if row["validation"]["execution_valid"] is not True
    ]
    ranked = sorted(valid, key=lambda row: float(row[rank_by] or 0), reverse=True)
    best_trial = None
    try:
        best_trial = {
            "number": study.best_trial.number,
            "value": study.best_trial.value,
            "params": dict(study.best_trial.params),
        }
    except ValueError:
        pass  # every trial pruned/invalid
    return ExecutionGridResult(
        grid_id=uuid.uuid4().hex[:12],
        rank_by=rank_by,
        runs=run_rows,
        ranked=ranked[:top_n_artifacts],
        invalid=invalid,
        optimizer="optuna",
        search={
            "n_trials": len(run_rows),
            "seed": seed,
            "sampler": sampler,
            "best_trial": best_trial,
        },
    )


def _suggest(trial: Any, name: str, dim: Mapping[str, Any]) -> Any:
    kind = dim.get("type")
    if kind == "float":
        return trial.suggest_float(
            name,
            float(dim["low"]),
            float(dim["high"]),
            step=dim.get("step"),
            log=bool(dim.get("log") or False),
        )
    if kind == "int":
        return trial.suggest_int(
            name,
            int(dim["low"]),
            int(dim["high"]),
            step=int(dim.get("step") or 1),
            log=bool(dim.get("log") or False),
        )
    if kind == "categorical":
        return trial.suggest_categorical(name, list(dim["choices"]))
    raise ValueError(f"unsupported search dimension type {kind!r} for {name!r}")
