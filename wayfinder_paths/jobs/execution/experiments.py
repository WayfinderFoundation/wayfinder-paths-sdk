from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.execution.job import backtest_execution_job
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

EXPERIMENTS_FILE = "results/backtest/experiments.jsonl"


def record_experiment(
    job_id: str,
    grid_payload: Mapping[str, Any],
    *,
    store: JobStore | None = None,
) -> dict[str, Any]:
    """Append one experiment row per grid run so parameter searches leave a
    durable, comparable trail instead of evaporating into a grids/ folder."""
    store = store or JobStore()
    result = grid_payload.get("result") or grid_payload
    ranked = result.get("ranked") or []
    best = ranked[0] if ranked else None
    row = {
        "ts": utc_now_iso(),
        "grid_id": result.get("grid_id"),
        "revision": grid_payload.get("revision"),
        "dataset": grid_payload.get("dataset"),
        "rank_by": result.get("rank_by"),
        "run_count": len(result.get("runs") or []),
        "invalid_count": len(result.get("invalid") or []),
        "best": (
            {
                "run_id": best.get("run_id"),
                "params": best.get("params"),
                "stats": best.get("stats"),
            }
            if best
            else None
        ),
    }
    if result.get("optimizer") and result.get("optimizer") != "grid":
        row["optimizer"] = result["optimizer"]
        row["search"] = result.get("search")
    if grid_payload.get("walk_forward") is not None:
        row["walk_forward"] = grid_payload["walk_forward"]
    path = store.job_dir(job_id) / EXPERIMENTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return row


def list_experiments(
    job_id: str, *, store: JobStore | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    store = store or JobStore()
    path = store.job_dir(job_id) / EXPERIMENTS_FILE
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[
        -limit:
    ]:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


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
    resolved = dict(params) if params else _params_from_grid(
        store, job_id, grid_id=grid_id, run_id=run_id
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
    from wayfinder_paths.jobs.execution.preflight import run_preflight

    preflight = run_preflight(job_id, store=store)
    revision = compute_workspace_revision(store.job_dir(job_id))
    _record_params_revision(store, job_id, revision, resolved, grid_id=grid_id)
    outcome = {
        "mode": "direct",
        "params": resolved,
        "revision": revision,
        "backtest_stats": ((backtest.get("result") or {}).get("stats")),
        "validation": (backtest.get("validation") or {}).get("status"),
        "preflight": preflight.get("status"),
    }
    wf_summary = _walk_forward_summary_for_grid(store, job_id, grid_id)
    if wf_summary is not None:
        # Report-only: shown so IS/OOS decay is visible at the moment of
        # promotion; never blocks.
        outcome["walk_forward_summary"] = wf_summary
    return outcome


def _walk_forward_summary_for_grid(
    store: JobStore, job_id: str, grid_id: str | None
) -> dict[str, Any] | None:
    if not grid_id:
        return None
    for row in reversed(list_experiments(job_id, store=store)):
        if row.get("grid_id") == grid_id and row.get("walk_forward"):
            return row["walk_forward"].get("summary")
    return None


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
        store.job_dir(job_id) / "results" / "backtest" / "grids" / grid_id
        / "summary.json"
    )
    if not summary_path.exists():
        raise FileNotFoundError(f"grid summary not found: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rows = summary.get("ranked") or []
    if run_id:
        rows = [row for row in summary.get("runs") or [] if row.get("run_id") == run_id]
    if not rows:
        raise ValueError(
            f"no {'run ' + run_id if run_id else 'ranked runs'} in grid {grid_id}"
        )
    return dict(rows[0].get("params") or {})


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
    workers: int = 1,
    parallel: str = "serial",
    walk_forward: Mapping[str, Any] | None = None,
    optimizer: str = "grid",
    optuna_options: Mapping[str, Any] | None = None,
    store: JobStore | None = None,
) -> dict[str, Any]:
    """Grid/optuna backtest + experiment record in one step (CLI convenience).

    `grid` doubles as the optuna search space when optimizer="optuna" — the
    two file formats are self-distinguishing (dict-of-lists vs typed dims).
    """
    import tempfile

    store = store or JobStore()
    if isinstance(grid, (str, Path)):
        grid_path = Path(grid)
    else:
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
        store=store,
    )
    row = record_experiment(job_id, payload, store=store)
    return {"experiment": row, "backtest": payload}
