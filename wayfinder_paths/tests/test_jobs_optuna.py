"""Optuna search: grid-contract compatibility, determinism, plumbing.

All heavy tests skip without the optional `ml` group; the lazy-import error
message test runs everywhere (sys.modules poisoning) so the no-deps UX is
always exercised.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution import optimize as optimize_module
from wayfinder_paths.jobs.execution.experiments import promote_params, run_experiment
from wayfinder_paths.jobs.execution.optimize import is_search_space, run_optuna_search
from wayfinder_paths.jobs.execution.simulator import (
    ExecutionBacktestResult,
    PreparedExecutionDataset,
)
from wayfinder_paths.jobs.execution.walk_forward import run_walk_forward
from wayfinder_paths.tests.test_jobs_preflight import _make_job
from wayfinder_paths.tests.test_jobs_strategies_scenarios import bars_from_closes
from wayfinder_paths.tests.test_jobs_walk_forward import (
    DIRECTION_STRATEGY,
)
from wayfinder_paths.tests.test_jobs_walk_forward import (
    _bars as rising_bars,
)

# Peaked price path: close 10 -> 20 at index 10 -> back down. A buy-and-hold-k
# strategy's net_return has a unique argmax at hold=9 (exit fills at the peak).
PEAKED = [10.0 + i for i in range(11)] + [20.0 - i for i in range(1, 10)]

INT_SPACE = {
    "hold": {"type": "int", "low": 4, "high": 14},
    "initial_capital": 1000.0,  # constant passthrough
}


class HoldK:
    def __init__(self, params: dict[str, Any]) -> None:
        self.hold = int(params["hold"])

    def decide(self, ctx: Any) -> list[dict[str, Any]]:
        bar_index = len(ctx.view.timestamps) - 1
        if "SNX" not in ctx.ledger.positions:
            if bar_index == 0:
                return [
                    {
                        "action": "OPEN",
                        "venue": "hyperliquid",
                        "symbol": "SNX",
                        "side": "buy",
                        "size": 10,
                    }
                ]
            return []
        if bar_index >= self.hold:
            return [
                {
                    "action": "CLOSE",
                    "venue": "hyperliquid",
                    "symbol": "SNX",
                    "side": "sell",
                    "size": 10,
                    "reduce_only": True,
                }
            ]
        return []


def _spec() -> ExecutionSpec:
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    return spec


def _dataset() -> PreparedExecutionDataset:
    return PreparedExecutionDataset.from_rows(bars_from_closes(PEAKED, symbol="SNX"))


def _search(**kwargs: Any):
    return run_optuna_search(
        lambda params: HoldK(params),  # type: ignore[arg-type]
        _dataset(),
        _spec(),
        INT_SPACE,
        rank_by="net_return",
        **kwargs,
    )


def test_is_search_space_distinguishes_formats() -> None:
    assert is_search_space({"x": {"type": "float", "low": 0, "high": 1}})
    assert not is_search_space({"x": [1, 2, 3]})
    assert not is_search_space({"x": 5, "y": "SNX"})
    assert not is_search_space([{"x": 1}])


def test_missing_optuna_error_mentions_ml_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "optuna", None)
    with pytest.raises(RuntimeError, match="poetry install --with ml"):
        _search(n_trials=2)


def test_planted_optimum_recovered() -> None:
    pytest.importorskip("optuna")
    result = _search(n_trials=20, seed=42)
    assert result.optimizer == "optuna"
    assert result.rank_by == "net_return"
    assert result.search["sampler"] == "tpe"
    best = result.ranked[0]
    assert best["params"]["hold"] == 9  # exit fill lands exactly on the peak
    assert best["params"]["initial_capital"] == 1000.0  # constant passed through
    assert best["stats"]["net_return"] == pytest.approx(0.09)


def test_same_seed_is_deterministic() -> None:
    pytest.importorskip("optuna")
    first = _search(n_trials=12, seed=7)
    second = _search(n_trials=12, seed=7)
    assert [row["params"] for row in first.runs] == [
        row["params"] for row in second.runs
    ]
    assert first.ranked[0]["params"] == second.ranked[0]["params"]
    different = _search(n_trials=12, seed=8)
    assert [row["params"] for row in different.runs] != [
        row["params"] for row in first.runs
    ]


def test_invalid_runs_are_pruned_not_ranked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("optuna")

    def fake_simulate(script: Any, dataset: Any, spec: Any, params: Any):
        hold = int(params["hold"])
        return ExecutionBacktestResult(
            run_id=f"fake-{hold}",
            params=dict(params),
            equity_curve=[],
            trades=[],
            positions=[],
            stats={"net_return": hold / 100.0},
            trace={},
            validation={"execution_valid": hold % 2 == 1},  # even holds invalid
            visualization={},
        )

    monkeypatch.setattr(optimize_module, "simulate_execution", fake_simulate)
    result = _search(n_trials=15, seed=1)
    assert result.invalid, "even-hold trials must land in invalid"
    assert all(row["params"]["hold"] % 2 == 0 for row in result.invalid)
    assert all(row["params"]["hold"] % 2 == 1 for row in result.ranked)


def test_rejects_space_without_typed_dimensions() -> None:
    pytest.importorskip("optuna")
    with pytest.raises(ValueError, match="no typed dimensions"):
        run_optuna_search(
            lambda params: HoldK(params),  # type: ignore[arg-type]
            _dataset(),
            _spec(),
            {"hold": [4, 9, 14]},  # a grid, not a space
        )


def test_grid_optimizer_rejects_search_space_payload(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path)
    with pytest.raises(ValueError, match="optimizer optuna"):
        run_experiment(
            job_id,
            {"threshold": {"type": "float", "low": 5.0, "high": 50.0}},
            store=store,
        )


def test_run_experiment_and_promote_from_optuna_summary(tmp_path: Path) -> None:
    pytest.importorskip("optuna")
    store, job_id, root = _make_job(tmp_path)

    result = run_experiment(
        job_id,
        {"threshold": {"type": "float", "low": 5.0, "high": 50.0}},
        rank_by="net_return",
        optimizer="optuna",
        optuna_options={"n_trials": 5, "seed": 3},
        store=store,
    )

    experiment = result["experiment"]
    assert experiment["optimizer"] == "optuna"
    assert experiment["search"]["n_trials"] == 5
    assert experiment["best"]["params"]["threshold"] is not None
    grid_id = experiment["grid_id"]
    summary_path = root / "results" / "backtest" / "grids" / grid_id / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["optimizer"] == "optuna"

    promoted = promote_params(job_id, grid_id=grid_id, store=store)
    assert promoted["mode"] == "direct"
    job = store.load(job_id)
    assert (
        job.execution_params["threshold"] == experiment["best"]["params"]["threshold"]
    )


def test_walk_forward_with_optuna_selects_per_fold(tmp_path: Path) -> None:
    pytest.importorskip("optuna")
    script = tmp_path / "strategy.py"
    script.write_text(DIRECTION_STRATEGY.lstrip(), encoding="utf-8")
    dataset = PreparedExecutionDataset.from_rows(rising_bars(320, rise_until=320))

    report = run_walk_forward(
        script,
        dataset,
        _spec(),
        {"direction": {"type": "categorical", "choices": ["long", "short"]}},
        rank_by="net_return",
        folds=2,
        test_bars=40,
        warmup_bars=10,
        anchored=True,
        optimizer="optuna",
        optuna_options={"n_trials": 4, "seed": 5},
    )

    assert report["spec"]["optimizer"] == "optuna"
    ok = [row for row in report["folds"] if row["status"] == "ok"]
    assert ok, report["folds"]
    # Monotone rising market: every fold's in-sample search must pick long.
    assert all(row["params"]["direction"] == "long" for row in ok)
    assert all(row["test_stats"]["net_return"] > 0 for row in ok)


def test_cli_passes_optimizer_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from click.testing import CliRunner

    from wayfinder_paths.jobs import cli as cli_module
    from wayfinder_paths.jobs.execution import experiments as experiments_module

    captured: dict[str, Any] = {}

    def fake_run_experiment(job_id: str, grid: Any, **kwargs: Any) -> dict[str, Any]:
        captured["job_id"] = job_id
        captured.update(kwargs)
        return {"experiment": {"grid_id": "x"}, "backtest": {}}

    # Patch both the defining module and the CLI's module-top binding.
    monkeypatch.setattr(experiments_module, "run_experiment", fake_run_experiment)
    monkeypatch.setattr(cli_module, "run_experiment", fake_run_experiment)
    monkeypatch.setattr(cli_module, "JobStore", lambda: None)
    space = tmp_path / "space.json"
    space.write_text(json.dumps({"threshold": {"type": "float", "low": 1, "high": 2}}))

    result = CliRunner().invoke(
        cli_module.job_cli,
        [
            "experiments",
            "demo-job",
            "--grid",
            str(space),
            "--optimizer",
            "optuna",
            "--n-trials",
            "7",
            "--seed",
            "3",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["optimizer"] == "optuna"
    assert captured["optuna_options"] == {"n_trials": 7, "seed": 3}
