from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.execution.job import backtest_execution_job
from wayfinder_paths.jobs.execution.preflight import run_preflight
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.improver.spec import revision_stamp
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

EXPERIMENTS_FILE = "results/backtest/experiments.jsonl"
TRIALS_FILE = "results/backtest/trials.jsonl"


def experiment_semantic_hash(row: Mapping[str, Any]) -> str:
    """Stable identity for the question an experiment asks, not its run id.

    New experiment rows carry this hash directly. The fallback keeps legacy
    ledgers classifiable using the definition fields they retained; timestamps,
    generated ids and observed statistics deliberately do not participate.
    """
    recorded = str(row.get("semantic_hash") or "").strip()
    if recorded:
        return recorded
    raw_best = row.get("best")
    best: Mapping[str, Any] = raw_best if isinstance(raw_best, Mapping) else {}
    definition = {
        "revision": row.get("revision"),
        "dataset": row.get("dataset"),
        "rank_by": row.get("rank_by"),
        "optimizer": row.get("optimizer") or "grid",
        "search": row.get("search"),
        "parameters": (
            row.get("parameters")
            if row.get("parameters") is not None
            else ([best.get("params")] if best.get("params") is not None else [])
        ),
        "walk_forward": row.get("walk_forward_definition"),
    }
    encoded = json.dumps(definition, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _experiment_definition(grid_payload: Mapping[str, Any]) -> dict[str, Any]:
    result = grid_payload["result"]
    parameters = [run.get("params") for run in result.get("runs") or []]
    parameters.sort(
        key=lambda value: json.dumps(value, sort_keys=True, default=str)
    )
    return {
        "revision": grid_payload.get("revision"),
        "dataset": grid_payload.get("dataset"),
        "rank_by": result.get("rank_by"),
        "optimizer": result.get("optimizer") or "grid",
        "search": result.get("search"),
        # Grid ids and run ids are regenerated on every invocation. The tested
        # coordinates are the semantic family and preserve duplicate identity.
        "parameters": parameters,
        "walk_forward_definition": grid_payload.get("walk_forward_definition"),
    }


def _behavior_descriptor(stats: Mapping[str, Any]) -> dict[str, Any]:
    """Compact behavioral fingerprint per trial — the population-search axes
    (how a candidate TRADES, not just how much it made). Defensive: stats
    keys vary by engine version; absent fields stay None."""

    def _num(key: str) -> float | None:
        value = stats.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    return {
        "trade_count": _num("trade_count"),
        "win_rate": _num("win_rate"),
        "avg_hold_bars": _num("avg_hold_bars") or _num("avg_holding_bars"),
        "max_drawdown_pct": _num("max_drawdown_pct"),
        "net_return": _num("net_return"),
    }


def record_trial_lineage(
    job_id: str,
    grid_payload: Mapping[str, Any],
    *,
    store: JobStore | None = None,
    cap: int = 500,
) -> int:
    """EVERY evaluated trial becomes a lineage row — the archive-of-record a
    global-search benchmark needs (the ranked top-N alone hides where search
    actually went). One compact row per run, appended per grid/optuna
    campaign; `cap` bounds pathological sweeps."""
    store = store or JobStore()
    result = grid_payload["result"]
    stamp = revision_stamp(store.job_dir(job_id))
    rows = []
    for run in list(result.get("runs") or [])[:cap]:
        stats = run.get("stats") or {}
        rows.append(
            {
                "ts": utc_now_iso(),
                **stamp,
                "grid_id": result.get("grid_id"),
                "revision": grid_payload.get("revision"),
                "optimizer": result.get("optimizer") or "grid",
                "run_id": run.get("run_id"),
                "params": run.get("params"),
                "rank_metric": stats.get(result.get("rank_by") or "net_return"),
                "pareto": run.get("pareto"),
                "behavior": _behavior_descriptor(stats),
            }
        )
    if not rows:
        return 0
    path = store.job_dir(job_id) / TRIALS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return len(rows)


def record_experiment(
    job_id: str,
    grid_payload: Mapping[str, Any],
    *,
    store: JobStore | None = None,
) -> dict[str, Any]:
    """Append one experiment row per grid run so parameter searches leave a
    durable, comparable trail instead of evaporating into a grids/ folder."""
    store = store or JobStore()
    result = grid_payload["result"]
    ranked = result["ranked"]
    best = ranked[0] if ranked else None
    definition = _experiment_definition(grid_payload)
    row = {
        "ts": utc_now_iso(),
        **revision_stamp(store.job_dir(job_id)),
        "grid_id": result["grid_id"],
        "revision": grid_payload["revision"],
        "dataset": grid_payload["dataset"],
        "rank_by": result["rank_by"],
        "run_count": len(result["runs"]),
        "invalid_count": len(result["invalid"]),
        "semantic_hash": experiment_semantic_hash(definition),
        "best": (
            {
                "run_id": best["run_id"],
                "params": best["params"],
                "stats": best["stats"],
            }
            if best
            else None
        ),
    }
    if result["optimizer"] != "grid":
        row["optimizer"] = result["optimizer"]
        row["search"] = result["search"]
    if "walk_forward" in grid_payload:
        row["walk_forward"] = grid_payload["walk_forward"]
    path = store.job_dir(job_id) / EXPERIMENTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    row["trials_recorded"] = record_trial_lineage(job_id, grid_payload, store=store)
    return row


def list_experiments(
    job_id: str, *, store: JobStore | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    store = store or JobStore()
    path = store.job_dir(job_id) / EXPERIMENTS_FILE
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]
    ]


def promote_params(
    job_id: str,
    *,
    grid_id: str | None = None,
    run_id: str | None = None,
    params: Mapping[str, Any] | None = None,
    via_proposal: bool = False,
    store: JobStore | None = None,
) -> dict[str, Any]:
    """Promote winning experiment parameters into the job.

    Direct path (pre-go-live modelling): write the params into
    job.execution_params, re-run the backtest so latest.json is stamped with
    the NEW revision, and record the revision. Proposal path (live jobs): the
    same parameter change rides the normal approve -> validate -> promote flow
    so it cannot skip the gate.
    """
    store = store or JobStore()
    resolved = (
        dict(params)
        if params
        else _params_from_grid(store, job_id, grid_id=grid_id, run_id=run_id)
    )
    if not resolved:
        raise ValueError("no params resolved; pass params or grid_id/run_id")

    if via_proposal:
        return _promote_via_proposal(store, job_id, resolved, grid_id=grid_id)

    job = store.load(job_id)
    job.execution_params.update(resolved)
    job.touch()
    store.save(job)
    backtest = backtest_execution_job(job_id, store=store)
    # The params change produced a new revision: the backtest above re-stamps
    # results/ and validation, but preflight would stay at the old revision
    # and leave the live gate red until someone re-ran it manually.
    preflight = run_preflight(job_id, store=store)
    revision = compute_workspace_revision(store.job_dir(job_id))
    _record_params_revision(store, job_id, revision, resolved, grid_id=grid_id)
    outcome = {
        "mode": "direct",
        "params": resolved,
        "revision": revision,
        "backtest_stats": backtest["result"]["stats"],
        "validation": backtest["validation"]["status"],
        "preflight": preflight.get("status"),
    }
    if grid_id:
        for row in reversed(list_experiments(job_id, store=store)):
            if row["grid_id"] == grid_id and row.get("walk_forward"):
                # Report-only: shown so IS/OOS decay is visible at the moment
                # of promotion; never blocks.
                outcome["walk_forward_summary"] = row["walk_forward"]["summary"]
                break
    return outcome


def _params_from_grid(
    store: JobStore,
    job_id: str,
    *,
    grid_id: str | None,
    run_id: str | None,
) -> dict[str, Any]:
    if not grid_id:
        raise ValueError("grid_id is required when params are not passed explicitly")
    summary_path = (
        store.job_dir(job_id)
        / "results"
        / "backtest"
        / "grids"
        / grid_id
        / "summary.json"
    )
    if not summary_path.exists():
        raise FileNotFoundError(f"grid summary not found: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rows = summary["ranked"]
    if run_id:
        rows = [row for row in summary["runs"] if row["run_id"] == run_id]
    if not rows:
        raise ValueError(
            f"no {'run ' + run_id if run_id else 'ranked runs'} in grid {grid_id}"
        )
    return dict(rows[0]["params"])


def _record_params_revision(
    store: JobStore,
    job_id: str,
    revision: str,
    params: Mapping[str, Any],
    *,
    grid_id: str | None,
) -> None:
    # versioning is excluded from the revision hash, so this save is pure
    # bookkeeping and cannot invalidate the revision it records.
    job = store.load(job_id)
    job.versioning["active_revision"] = revision
    job.versioning["active_label"] = f"params/{grid_id or 'manual'}"
    store.save(job)
    store.write_json(
        job_id,
        "versions/active.json",
        {
            "job_id": job_id,
            "active_revision": revision,
            "active_label": f"params/{grid_id or 'manual'}",
        },
    )
    root = store.job_dir(job_id)
    revisions_path = root / "versions" / "revisions.jsonl"
    revisions_path.parent.mkdir(parents=True, exist_ok=True)
    with revisions_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "ts": utc_now_iso(),
                    "revision": revision,
                    "source": "promote_params",
                    "grid_id": grid_id,
                    "params": dict(params),
                },
                sort_keys=True,
                default=str,
            )
            + "\n"
        )
    store.append_journal(
        job_id,
        {"type": "params_promoted", "revision": revision, "grid_id": grid_id},
    )


def _promote_via_proposal(
    store: JobStore,
    job_id: str,
    params: dict[str, Any],
    *,
    grid_id: str | None,
) -> dict[str, Any]:
    # Route through the structured propose flow: the proposal gets a
    # pre-approval candidate, full validation, a baseline-vs-candidate
    # comparison, and the candidate_report the approve gates require.
    # circular import: proposals imports experiments helpers
    from wayfinder_paths.jobs.proposals import propose_change

    changed = sorted(params)
    proposal_id = f"params-{(grid_id or 'manual')[:12]}-{utc_now_iso()[:10]}"
    proposal = propose_change(
        store,
        job_id,
        kind="params_update",
        summary=f"Update execution_params from experiment {grid_id or 'manual'}",
        intent_contract={
            "intent": (
                "Promote experiment-selected parameters "
                f"({', '.join(changed)}) into execution_params."
            ),
            "rules_changed": [f"execution_params.{name}" for name in changed],
            "rules_unchanged": ["strategy logic", "execution spec", "schedule"],
            "risk_constraints": "unchanged; auto_limits still enforced per intent",
            "entry_conditions": "unchanged",
            "exit_conditions": "unchanged",
            "known_non_goals": ["No strategy-logic or schedule changes."],
        },
        params=dict(params),
        proposal_id=proposal_id,
    )
    return {
        "mode": "proposal",
        "proposal_id": proposal_id,
        "params": params,
        "candidate_report": proposal.get("candidate_report"),
    }


def run_experiment(
    job_id: str,
    grid: Mapping[str, Any] | list[Mapping[str, Any]] | str | Path,
    *,
    rank_by: str = "net_return",
    workers: int = 0,
    parallel: str = "process",
    walk_forward: Mapping[str, Any] | None = None,
    optimizer: str = "grid",
    optuna_options: Mapping[str, Any] | None = None,
    quick_bars: int | None = None,
    store: JobStore | None = None,
) -> dict[str, Any]:
    """Grid/optuna backtest + experiment record in one step (CLI convenience).

    `grid` doubles as the optuna search space when optimizer="optuna" — the
    two file formats are self-distinguishing (dict-of-lists vs typed dims).

    Defaults to a process pool across all (cgroup-clamped) cores — grid cells
    are independent and a serial sweep of a heavy strategy over full history
    is what blows the interactive time budget (observed live: 8 combos x 4319
    bars at ~30 bars/s ~= 20 minutes serial). `quick_bars` bounds the dataset
    to the last N bars for the whole experiment (grid AND walk-forward) for
    iteration-speed sweeps; leave it unset for the final validation.
    """
    store = store or JobStore()
    match grid:
        case str() | Path():
            grid_path = Path(grid)
        case _:
            handle = tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8"
            )
            json.dump(grid, handle)
            handle.close()
            grid_path = Path(handle.name)
    payload = backtest_execution_job(
        job_id,
        grid_path=grid_path,
        workers=workers,
        parallel=parallel,
        rank_by=rank_by,
        walk_forward=walk_forward,
        optimizer=optimizer,
        optuna_options=optuna_options,
        quick_bars=quick_bars,
        store=store,
    )
    row = record_experiment(job_id, payload, store=store)
    return {"experiment": row, "backtest": payload}
