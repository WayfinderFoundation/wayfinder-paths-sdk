from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.execution.features import (
    load_feature_rows,
    merge_features,
    parse_feature_specs,
)
from wayfinder_paths.jobs.execution.optimize import is_search_space, run_optuna_search
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
from wayfinder_paths.jobs.execution.walk_forward import run_walk_forward
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

_HEAVY_RESULT_KEYS = ("equity_curve", "trades", "positions", "trace", "visualization")


def summarize_backtest_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Compact view of a backtest payload for stdout / agent context.

    Keeps the decision-grade fields — `stats`, `profile` (timing), `validation`,
    artifact paths, revision stamp — and drops the multi-MB per-bar arrays
    (`equity_curve`, `trades`, `positions`, `trace`, `visualization`). Those are
    all persisted under `results/backtest/` and browsable via `job backtest-view`,
    so nothing is lost — the full payload can also be requested with `--full`.
    A single-run backtest payload is ~8 MB; this trims it to ~2 KB.
    """
    summary: dict[str, Any] = {
        k: payload[k]
        for k in ("type", "artifacts", "stamp", "walk_forward", "validation")
        if k in payload
    }
    coverage = ((payload.get("dataset") or {}).get("feature_coverage")) or {}
    thin = {
        name: info
        for name, info in coverage.items()
        if float(info.get("coverage_fraction") or 0.0) < 0.8
    }
    if coverage:
        # summarize drops the raw dataset metadata; coverage is decision-grade.
        summary["feature_coverage"] = coverage
    if thin:
        worst = min(thin, key=lambda k: float(thin[k].get("coverage_fraction") or 0.0))
        frac = float(thin[worst].get("coverage_fraction") or 0.0)
        summary["feature_coverage_note"] = (
            f"feature '{worst}' covers only {frac:.0%} of the bars span "
            f"(thin: {sorted(thin)}) — bars outside a feature's span carry "
            "NaN, so any comparison against full-history signals is biased. "
            "Re-fetch the feature to match the dataset window "
            "(e.g. fetch_funding with the same days as the candles)."
        )
    result = payload.get("result")
    if isinstance(result, Mapping):
        if "ranked" in result:  # grid / optuna result
            summary["result"] = {
                k: result[k]
                for k in ("grid_id", "rank_by", "optimizer", "search", "plateau")
                if k in result
            }
            summary["result"]["run_count"] = len(result.get("runs") or [])
            summary["result"]["invalid_count"] = len(result.get("invalid") or [])
            summary["result"]["ranked"] = [
                {k: v for k, v in row.items() if k not in _HEAVY_RESULT_KEYS}
                for row in (result.get("ranked") or [])[:10]
            ]
            # In-sample grids overfit: the top params were picked AND scored on
            # the same data, so their metrics are not evidence of an edge.
            if "walk_forward" not in payload:
                summary["note"] = (
                    "In-sample ranking only — these params were tuned and scored "
                    "on the same data. Validate out-of-sample before trusting "
                    "them: `job experiments <id> --grid <file> --wf-test-bars N "
                    "--wf-folds K`, then judge on decay_ratio / oos_positive_folds."
                )
        else:  # single run — the ~8 MB case
            summary["result"] = {
                k: result[k]
                for k in ("run_id", "params", "stats", "validation", "profile")
                if k in result
            }
    return summary


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
    quick_bars: int | None = None,
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
    # `--quick N`: backtest only the last N bars — cheap enough to sweep many
    # parameters fast while iterating, before the full-history confirmation run.
    if quick_bars and quick_bars > 0:
        timestamps = dataset.bars.timestamps
        if len(timestamps) > quick_bars:
            dataset = PreparedExecutionDataset(
                dataset.bars.window(len(timestamps) - 1, quick_bars),
                {**dataset.metadata, "quick_bars": quick_bars},
            )
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
        param_grid = json.loads(Path(grid_path).read_text(encoding="utf-8"))
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
        artifacts["trade_forensics"] = _write_trade_forensics(
            result, dataset, output_dir, stamp=stamp
        )
        payload = {
            "type": "single",
            "result": result.to_dict(),
            "artifacts": artifacts,
            **stamp,
        }
    validation = validate_execution_job(job_id, store=store)
    payload["validation"] = validation
    return payload


_FORENSICS_MAX_TRADES = 120


def _write_trade_forensics(
    result: Any,
    dataset: PreparedExecutionDataset,
    output_dir: Path,
    *,
    stamp: Mapping[str, Any],
) -> str | None:
    """Per-trade path forensics + population aggregate for the baseline run.

    The aggregate (by exit reason: MAE/MFE, post-exit excursion, stop-survival
    rates) is the statistically meaningful exit-quality view the intervention
    agent reasons from; forward per-trade rows are only hypothesis fuel.
    Best-effort: a forensics failure must not fail the backtest.
    """
    try:
        from wayfinder_paths.jobs.trade_forensics import (
            aggregate_trade_forensics,
            forensics_for_closed_trades,
        )

        trades = list(result.trades or [])
        # A backtest close row keys prices under avg_price/timestamp; align
        # copies with the forward trade-close shape the shared helper expects
        # (copies: result.trades feeds payload["result"] afterwards).
        closes = [
            {
                **row,
                "price": row.get("avg_price"),
                "closed_at": row.get("timestamp"),
                "net_pnl": row.get("realized_pnl_delta"),
            }
            for row in trades
            if row.get("reduce_only")
        ][-_FORENSICS_MAX_TRADES:]
        view = dataset.bars
        bars_by_symbol = {symbol: view.symbol_frame(symbol) for symbol in view.symbols}
        rows = forensics_for_closed_trades(bars_by_symbol, closes, trades)
        path = Path(output_dir) / "trade_forensics.json"
        path.write_text(
            json.dumps(
                {
                    "aggregate": aggregate_trade_forensics(rows),
                    "trades": rows,
                    **dict(stamp),
                },
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        return str(path)
    except Exception:  # noqa: BLE001 — telemetry must never fail a backtest
        return None


def _load_job_yaml(root: Path) -> dict[str, Any]:
    path = root / "job.yaml"
    if not path.exists():
        raise FileNotFoundError(f"job.yaml not found: {path}")
    match yaml.safe_load(path.read_text(encoding="utf-8")) or {}:
        case dict() as loaded:
            return loaded
        case _:
            raise ValueError(f"Invalid job.yaml: {path}")


def _store_feature_specs(roots: tuple[Path, ...], declared: set[str]) -> list[Any]:
    """FeatureSpecs for every feature-store name NOT in the data contract.

    Research loaders merge these so undeclared research-side columns
    (derive-features output) are visible to rank-check/--column/workspace
    defs; execution paths never call this — the contract still governs what
    a LIVE strategy may consume."""
    from wayfinder_paths.jobs.execution.features import (
        DEFAULT_FEATURES_PATH,
        FeatureSpec,
    )

    names: set[str] = set()
    for root in roots:
        path = root / DEFAULT_FEATURES_PATH
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("name"):
                names.add(str(row["name"]))
    return [FeatureSpec(name=name) for name in sorted(names - declared)]


def _load_dataset(
    root: Path,
    spec: ExecutionSpec,
    job_data: dict[str, Any],
    *,
    feature_roots: tuple[Path, ...] | None = None,
    include_store_features: bool = False,
) -> PreparedExecutionDataset:
    dataset = _resolve_dataset(root, spec, job_data)
    # Same feature merge the live driver applies per tick (as-of, backward):
    # backtest/live parity for exogenous data holds by construction.
    specs = parse_feature_specs(spec)
    if include_store_features:
        specs = specs + _store_feature_specs(
            tuple(feature_roots or (root,)), {item.name for item in specs}
        )
    if specs:
        frames = load_feature_rows(list(feature_roots or (root,)), specs)
        dataset = PreparedExecutionDataset(
            merge_features(dataset.bars, frames, specs),
            {
                **dataset.metadata,
                "features": [item.name for item in specs],
                "feature_coverage": _feature_coverage(dataset.bars, frames),
            },
        )
    return dataset


def _feature_coverage(bars: Any, frames: Mapping[str, Any]) -> dict[str, Any]:
    """Per-feature span vs the bars span. A feature that covers only the tail
    of the dataset silently handicaps that signal in any comparison (a 1-year
    funding file against 6 years of candles condemned a signal in a live
    session); coverage_fraction < 1 means every earlier bar carries NaN."""
    timestamps = bars.timestamps
    if not timestamps:
        return {}
    bars_first, bars_last = timestamps[0], timestamps[-1]
    bars_span = max((bars_last - bars_first).total_seconds(), 1.0)
    coverage: dict[str, Any] = {}
    for name, frame in frames.items():
        if frame.empty:
            coverage[name] = {"rows": 0, "coverage_fraction": 0.0}
            continue
        first = frame["timestamp"].iloc[0]
        last = frame["timestamp"].iloc[-1]
        overlap = (min(last, bars_last) - max(first, bars_first)).total_seconds()
        coverage[name] = {
            "first_ts": str(first),
            "last_ts": str(last),
            "rows": int(len(frame)),
            "coverage_fraction": round(max(0.0, overlap) / bars_span, 3),
        }
    return coverage


def _resolve_dataset(
    root: Path, spec: ExecutionSpec, job_data: dict[str, Any]
) -> PreparedExecutionDataset:
    candidate_paths = [
        root / "results" / "backtest" / "input_bars.json",
        root / "workspace" / "config" / "backtest_bars.json",
    ]
    for path in candidate_paths:
        if path.exists():
            # Agent-written files: accept a bare row list or {"bars": [...]}.
            # The file's own metadata block (days/interval/fetched_at from
            # fetch-dataset) rides along — dropping it here left the proposal
            # comparison's dataset window empty, so the UI could not label
            # "window return (Nd)".
            match json.loads(path.read_text(encoding="utf-8")):
                case {"bars": list() as rows, **rest}:
                    file_meta = rest.get("metadata")
                    return PreparedExecutionDataset.from_rows(
                        rows,
                        {
                            **(file_meta if isinstance(file_meta, dict) else {}),
                            "source": str(path),
                        },
                    )
                case [*rows]:
                    return PreparedExecutionDataset.from_rows(
                        rows, {"source": str(path)}
                    )
    scenario_plan = job_data.get("execution_scenario_plan") or spec.validation.get(
        "execution_scenario_plan"
    )
    match scenario_plan:
        case {"scenarios": [{"bars": list() as rows}, *_]}:
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


def synthesize_scenario_plan(
    root: Path,
    spec: ExecutionSpec,
    job_data: dict[str, Any],
    *,
    max_bars: int = 120,
) -> dict[str, Any] | None:
    """Minimal replay scenario plan from the job's backtest dataset: the last
    `max_bars` bars with the expectation that the trace validates. Used when a
    proposal (e.g. promote-params) must be applicable but the job declares no
    execution_scenario_plan — every applicable proposal carries executable
    scenarios, so candidate validation always exercises the real engine.
    Returns None when no dataset exists (raise-free by design)."""
    try:
        dataset = _load_dataset(root, spec, job_data)
    except FileNotFoundError:
        return None
    rows = dataset.bars.to_rows()
    if not rows:
        return None
    # Proposals are plain JSON files: coerce pd.Timestamp/NaN into JSON-safe
    # values so store.write_proposal can serialize the plan verbatim.
    bars = json.loads(json.dumps(rows[-max_bars:], default=str))
    return {
        "scenarios": [
            {
                "name": "baseline_replay",
                "bars": bars,
                "expect": {"execution_valid": True},
            }
        ]
    }
