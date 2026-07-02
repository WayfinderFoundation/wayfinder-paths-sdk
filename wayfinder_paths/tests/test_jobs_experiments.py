from __future__ import annotations

import json
from pathlib import Path

import pytest

from wayfinder_paths.jobs.execution.experiments import (
    list_experiments,
    promote_params,
    run_experiment,
)
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.tests.test_jobs_preflight import _make_job


def test_run_experiment_records_and_ranks(tmp_path: Path) -> None:
    store, job_id, root = _make_job(tmp_path)

    result = run_experiment(
        job_id,
        {"threshold": [10.0, 100.0]},
        rank_by="sharpe",
        store=store,
    )

    experiment = result["experiment"]
    assert experiment["grid_id"]
    assert experiment["rank_by"] == "sharpe"
    assert experiment["run_count"] == 2
    assert experiment["best"]["params"]
    rows = list_experiments(job_id, store=store)
    assert len(rows) == 1
    assert (root / "results" / "backtest" / "experiments.jsonl").exists()


def test_promote_params_direct_updates_job_and_revision(tmp_path: Path) -> None:
    store, job_id, root = _make_job(tmp_path)
    experiment = run_experiment(job_id, {"threshold": [10.0, 100.0]}, store=store)[
        "experiment"
    ]

    result = promote_params(job_id, grid_id=experiment["grid_id"], store=store)

    assert result["mode"] == "direct"
    job = store.load(job_id)
    assert job.execution_params["threshold"] == result["params"]["threshold"]
    assert job.versioning["active_revision"] == result["revision"]
    # backtest was re-run and stamped against the promoted revision
    latest = json.loads(
        (root / "results" / "backtest" / "latest.json").read_text(encoding="utf-8")
    )
    assert latest["revision"] == result["revision"]
    assert compute_workspace_revision(root) == result["revision"]
    revisions = (root / "versions" / "revisions.jsonl").read_text(encoding="utf-8")
    assert result["revision"] in revisions


def test_promote_params_via_proposal_enters_change_flow(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path)
    experiment = run_experiment(job_id, {"threshold": [10.0, 100.0]}, store=store)[
        "experiment"
    ]

    result = promote_params(
        job_id, grid_id=experiment["grid_id"], via_proposal=True, store=store
    )

    assert result["mode"] == "proposal"
    proposal = store.load_proposal(job_id, result["proposal_id"])
    assert proposal["status"] == "pending"
    assert proposal["intent_contract"]["rules_changed"]
    assert (
        proposal["proposed_change"]["execution_params"]["threshold"]
        == result["params"]["threshold"]
    )
    # params NOT applied directly — the change must ride the approve flow
    job = store.load(job_id)
    assert (
        "threshold" not in job.execution_params
        or job.execution_params["threshold"] != result["params"]["threshold"]
    )


def test_promote_params_requires_grid_or_params(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path)

    with pytest.raises(ValueError, match="grid_id"):
        promote_params(job_id, store=store)
