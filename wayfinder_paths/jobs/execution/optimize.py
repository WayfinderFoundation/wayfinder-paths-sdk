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
    ExecutionGridResult,
    PreparedExecutionDataset,
    _grid_row,
    check_rank_key,
    rank_and_partition,
    simulate_execution,
)


def _is_typed_dimension(value: Any) -> bool:
    match value:
        case {"type": "float" | "int" | "categorical"}:
            return True
        case _:
            return False


def is_search_space(payload: Any) -> bool:
    """True when the payload contains at least one typed search dimension."""
    match payload:
        case Mapping():
            return any(_is_typed_dimension(value) for value in payload.values())
        case _:
            return False


# Grid-row axes where smaller is better; every other rank key maximizes.
_MINIMIZE_AXES = frozenset({"max_drawdown_pct"})


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
    objectives: list[str] | None = None,
) -> ExecutionGridResult:
    """Single-objective TPE by default. Pass ``objectives=[axis, ...]`` (2+
    grid rank keys) for multi-objective search: NSGA-II over the requested
    axes, ``ranked`` leads with the Pareto front (rank_by breaks ties inside
    it), every run row carries ``pareto: bool``. A search ranked on one
    scalar collapses the risk/return trade-off the constitution actually
    scores — MOO returns the frontier and lets the economic gate choose."""
    try:
        import optuna  # lazy: optional --with ml dep; grid path never pays it
    except ImportError as exc:
        raise RuntimeError(
            'optuna is required for optimizer="optuna"; run `poetry install --with ml`'
        ) from exc

    check_rank_key(rank_by)
    objectives = list(objectives or [])
    if len(objectives) == 1:
        raise ValueError(
            "objectives needs 2+ axes for multi-objective search; for a "
            "single axis pass rank_by instead"
        )
    for axis in objectives:
        check_rank_key(axis)
    dimensions = {
        name: dict(value)
        for name, value in search_space.items()
        if _is_typed_dimension(value)
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

    def objective(trial: Any) -> float | tuple[float, ...]:
        params = dict(constants)
        for name, dim in dimensions.items():
            params[name] = _suggest(trial, name, dim)
        result = simulate_execution(script_entrypoint, dataset, execution_spec, params)
        row = _grid_row(result, rank_by=rank_by)
        row["trial"] = trial.number
        run_rows.append(row)
        if not row["validation"]["execution_valid"]:
            raise optuna.TrialPruned("execution trace invalid for these params")
        if objectives:
            return tuple(float(row.get(axis) or 0) for axis in objectives)
        return float(row[rank_by] or 0)  # same coercion as grid ranking

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    if objectives:
        # NSGA-II handles mixed int/float/categorical spaces; an explicit
        # sampler="motpe" opts into optuna's multi-objective TPE instead.
        match sampler:
            case "tpe" | "nsga2":
                sampler_name = "nsga2"
                sampler_impl = optuna.samplers.NSGAIISampler(seed=seed)
            case "motpe":
                sampler_name = "motpe"
                sampler_impl = optuna.samplers.TPESampler(seed=seed)
            case "random":
                sampler_name = "random"
                sampler_impl = optuna.samplers.RandomSampler(seed=seed)
            case _:
                raise ValueError(
                    f"sampler must be nsga2, motpe, or random for multi-"
                    f"objective search, got {sampler!r}"
                )
        study = optuna.create_study(
            directions=[
                "minimize" if axis in _MINIMIZE_AXES else "maximize"
                for axis in objectives
            ],
            sampler=sampler_impl,
        )
    else:
        match sampler:
            case "tpe":
                sampler_name = "tpe"
                sampler_impl = optuna.samplers.TPESampler(seed=seed)
            case "random":
                sampler_name = "random"
                sampler_impl = optuna.samplers.RandomSampler(seed=seed)
            case _:
                raise ValueError(f"sampler must be tpe or random, got {sampler!r}")
        study = optuna.create_study(direction="maximize", sampler=sampler_impl)
    # n_jobs=1 is REQUIRED (see module docstring), not a performance choice.
    study.optimize(objective, n_trials=n_trials, timeout=timeout, n_jobs=1)

    ranked, invalid = rank_and_partition(
        run_rows, rank_by=rank_by, top_n=top_n_artifacts
    )
    best_trial = None
    pareto = None
    if objectives:
        front_numbers = {trial.number for trial in study.best_trials}
        for row in run_rows:
            row["pareto"] = row["trial"] in front_numbers
        # Pareto members lead the ranking; rank_by orders inside each part.
        ranked = sorted(ranked, key=lambda row: bool(row.get("pareto")), reverse=True)
        pareto = [
            {
                "number": trial.number,
                "values": list(trial.values),
                "params": dict(trial.params),
            }
            for trial in study.best_trials[:top_n_artifacts]
        ]
    elif ranked:  # study.best_trial raises when every trial was pruned/invalid
        best_trial = {
            "number": study.best_trial.number,
            "value": study.best_trial.value,
            "params": dict(study.best_trial.params),
        }
    return ExecutionGridResult(
        grid_id=uuid.uuid4().hex[:12],
        rank_by=rank_by,
        runs=run_rows,
        ranked=ranked,
        invalid=invalid,
        optimizer="nsga2" if sampler_name == "nsga2" else "optuna",
        search={
            "n_trials": len(run_rows),
            "seed": seed,
            "sampler": sampler_name,
            "best_trial": best_trial,
            **(
                {"objectives": objectives, "pareto_front": pareto} if objectives else {}
            ),
        },
    )


def _suggest(trial: Any, name: str, dim: Mapping[str, Any]) -> Any:
    match dim["type"]:
        case "float":
            return trial.suggest_float(
                name,
                float(dim["low"]),
                float(dim["high"]),
                step=dim.get("step"),
                log=bool(dim.get("log")),
            )
        case "int":
            return trial.suggest_int(
                name,
                int(dim["low"]),
                int(dim["high"]),
                step=int(dim.get("step") or 1),
                log=bool(dim.get("log")),
            )
        case "categorical":
            return trial.suggest_categorical(name, list(dim["choices"]))
        case kind:
            raise ValueError(f"unsupported search dimension type {kind!r} for {name!r}")
