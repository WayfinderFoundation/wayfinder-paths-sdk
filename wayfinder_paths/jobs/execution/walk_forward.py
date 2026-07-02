"""Walk-forward grid evaluation: select params on train slices, evaluate the
selected params on held-out test slices, and report in-sample vs out-of-sample
decay. Report-only by design — promotion is never blocked; the numbers exist
so an in-sample-only spike (the SNX SuperTrend(7,2.5) failure mode: a lone
+22.3% grid cell whose neighbors sat at +1.9–8.5%) is visible before anyone
acts on it."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from statistics import fmean
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.optimize import run_optuna_search
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionSpec,
    bar_interval_seconds,
)
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    _stats,
    run_execution_grid,
    simulate_execution,
)


def run_walk_forward(
    script_entrypoint: str | Path | Callable[..., Any],
    dataset: PreparedExecutionDataset,
    execution_spec: ExecutionSpec | Mapping[str, Any] | None,
    param_grid: Mapping[str, list[Any]] | list[Mapping[str, Any]],
    *,
    rank_by: str = "net_return",
    folds: int = 3,
    train_bars: int | None = None,
    test_bars: int | None = None,
    warmup_bars: int = 60,
    anchored: bool = False,
    workers: int = 1,
    parallel: str = "serial",
    optimizer: str = "grid",
    optuna_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    spec = ExecutionSpec.coerce(execution_spec)
    if not test_bars or test_bars <= 0:
        raise ValueError("walk-forward requires test_bars > 0")
    if folds <= 0:
        raise ValueError("walk-forward requires folds > 0")
    if train_bars is None and not anchored:
        raise ValueError("pass train_bars or anchored=True")

    timestamps = dataset.bars.timestamps  # unique + sorted (multi-symbol safe)
    total = len(timestamps)
    min_train = max(train_bars or warmup_bars, warmup_bars)
    required = folds * test_bars + min_train
    if total < required:
        raise ValueError(
            f"dataset has {total} bars; walk-forward with folds={folds}, "
            f"test_bars={test_bars}, train>= {min_train} needs at least {required}"
        )

    fold_rows: list[dict[str, Any]] = []
    for index in range(folds):
        test_end = total - (folds - 1 - index) * test_bars
        test_start = test_end - test_bars
        train_start = (
            0 if anchored or train_bars is None else max(0, test_start - train_bars)
        )
        train_slice = _slice(dataset, timestamps, train_start, test_start)
        if optimizer == "optuna":
            # Per-fold seed offset: reproducible, but each fold explores its
            # own sampling path instead of replaying fold 0's suggestions.
            options = dict(optuna_options or {})
            options["seed"] = int(options.get("seed") or 42) + index
            grid = run_optuna_search(
                script_entrypoint,
                train_slice,
                spec,
                param_grid,
                rank_by=rank_by,
                **options,
            )
        else:
            grid = run_execution_grid(
                script_entrypoint,
                train_slice,
                spec,
                param_grid,
                rank_by=rank_by,
                workers=workers,
                parallel=parallel,
            )
        base_row = {
            "fold": index,
            "train": {
                "start": str(timestamps[train_start]),
                "end": str(timestamps[test_start - 1]),
                "bars": test_start - train_start,
            },
            "test": {
                "start": str(timestamps[test_start]),
                "end": str(timestamps[test_end - 1]),
                "bars": test_bars,
                "warmup_bars": warmup_bars,
            },
        }
        if not grid.ranked:
            fold_rows.append({**base_row, "status": "no_valid_runs"})
            continue
        best = grid.ranked[0]
        params = dict(best["params"])
        eval_start = max(0, test_start - warmup_bars)
        eval_slice = _slice(dataset, timestamps, eval_start, test_end)
        result = simulate_execution(script_entrypoint, eval_slice, spec, params)
        test_stats = _test_window_stats(result, timestamps[test_start], spec, params)
        fold_rows.append(
            {
                **base_row,
                "status": "ok",
                "params": params,
                "train_run_id": best["run_id"],
                "train_stats": best["stats"],
                "test_stats": test_stats,
            }
        )

    return {
        "spec": {
            "folds": folds,
            "train_bars": train_bars,
            "test_bars": test_bars,
            "warmup_bars": warmup_bars,
            "anchored": anchored,
            "rank_by": rank_by,
            "optimizer": optimizer,
        },
        "folds": fold_rows,
        "summary": _summary(fold_rows, rank_by),
    }


def _slice(
    dataset: PreparedExecutionDataset,
    timestamps: list[pd.Timestamp],
    start: int,
    end: int,
) -> PreparedExecutionDataset:
    frame = dataset.bars.to_frame()
    window = frame[
        (frame["timestamp"] >= timestamps[start])
        & (frame["timestamp"] <= timestamps[end - 1])
    ]
    return PreparedExecutionDataset(
        CompletedBarsView(window),
        {
            **dataset.metadata,
            "wf_window": [str(timestamps[start]), str(timestamps[end - 1])],
        },
    )


def _test_window_stats(
    result: Any,
    test_start: pd.Timestamp,
    spec: ExecutionSpec,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Recompute stats over the test window only. Warmup bars seed indicators
    (the same information a live job would have); returns rebase at the first
    test bar because _stats uses the window's first equity point as the base."""

    def in_window(row: Mapping[str, Any]) -> bool:
        return pd.Timestamp(row["timestamp"]) >= test_start

    equity = [row for row in result.equity_curve if in_window(row)]
    trades = [row for row in result.trades if in_window(row)]
    positions = [row for row in result.positions if in_window(row)]
    guard_events = [row for row in result.trace["guard_events"] if in_window(row)]
    price_series = {
        series["symbol"]: [point for point in series["points"] if in_window(point)]
        for series in result.visualization["series"]
        if series["kind"] == "market_price"
    }
    return _stats(
        equity,
        trades,
        positions,
        bar_interval_seconds(spec.data_contract.get("bar_interval")),
        params=params,
        guard_events=guard_events,
        price_series=price_series,
    )


def _summary(fold_rows: list[dict[str, Any]], rank_by: str) -> dict[str, Any]:
    ok = [row for row in fold_rows if row["status"] == "ok"]
    if not ok:
        return {"fold_count": 0}
    is_returns = [float(row["train_stats"]["net_return"]) for row in ok]
    oos_returns = [float(row["test_stats"]["net_return"]) for row in ok]
    is_rank = [
        v for v in (_metric(row["train_stats"], rank_by) for row in ok) if v is not None
    ]
    oos_rank = [
        v for v in (_metric(row["test_stats"], rank_by) for row in ok) if v is not None
    ]
    oos_sharpes = [
        float(row["test_stats"]["sharpe"])
        for row in ok
        if row["test_stats"]["sharpe"] is not None
    ]
    oos_sortinos = [
        float(row["test_stats"]["sortino"])
        for row in ok
        if row["test_stats"]["sortino"] is not None
    ]
    is_mean = fmean(is_returns)
    oos_mean = fmean(oos_returns)
    return {
        "fold_count": len(ok),
        "is_return_mean": is_mean,
        "oos_return_mean": oos_mean,
        "is_rank_metric_mean": fmean(is_rank) if is_rank else None,
        "oos_rank_metric_mean": fmean(oos_rank) if oos_rank else None,
        # Sign-guarded: a ratio against a negative in-sample base is noise.
        "decay_ratio": (oos_mean / is_mean) if is_mean > 0 else None,
        "oos_positive_folds": sum(1 for value in oos_returns if value > 0),
        "oos_sharpe_mean": fmean(oos_sharpes) if oos_sharpes else None,
        "oos_sortino_mean": fmean(oos_sortinos) if oos_sortinos else None,
        "oos_max_drawdown_worst": min(
            (float(row["test_stats"]["max_drawdown_pct"]) for row in ok),
            default=None,
        ),
    }


def _metric(stats: Mapping[str, Any], key: str) -> float | None:
    value = stats[key]
    return float(value) if value is not None else None


def format_fold_table(report: Mapping[str, Any]) -> str:
    """Compact human-readable fold table for CLI stderr output."""
    lines = ["walk-forward: fold | train window | IS ret | OOS ret | params"]
    for row in report["folds"]:
        if row["status"] != "ok":
            lines.append(f"  {row['fold']} | {row['status']}")
            continue
        is_ret = float(row["train_stats"]["net_return"]) * 100
        oos_ret = float(row["test_stats"]["net_return"]) * 100
        lines.append(
            f"  {row['fold']} | {row['train']['start'][:10]}→"
            f"{row['test']['end'][:10]} | {is_ret:+.2f}% | {oos_ret:+.2f}% | "
            f"{row['params']}"
        )
    summary = report["summary"]
    if summary["fold_count"]:
        decay = summary["decay_ratio"]
        lines.append(
            "  summary: IS mean "
            f"{summary['is_return_mean'] * 100:+.2f}% | OOS mean "
            f"{summary['oos_return_mean'] * 100:+.2f}% | "
            f"OOS-positive folds {summary['oos_positive_folds']}/"
            f"{summary['fold_count']} | decay "
            f"{f'{decay:.2f}' if decay is not None else 'n/a'}"
        )
    return "\n".join(lines)
