from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs.execution import experiments as experiments_module
from wayfinder_paths.jobs.execution.experiments import (
    list_experiments,
    promote_params,
    run_experiment,
)
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.tests.test_jobs_preflight import _make_job


def _count_backtests(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = experiments_module.backtest_execution_job

    def counting(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(experiments_module, "backtest_execution_job", counting)
    return calls


def _journal_entries(store: JobStore, job_id: str, kind: str) -> list[dict[str, Any]]:
    path = store.job_dir(job_id) / "journal.jsonl"
    if not path.exists():
        return []
    return [
        row
        for row in (
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if row["type"] == kind
    ]


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
    assert len(experiment["semantic_hash"]) == 64
    rows = list_experiments(job_id, store=store)
    assert len(rows) == 1
    assert (root / "results" / "backtest" / "experiments.jsonl").exists()

    repeated = run_experiment(
        job_id,
        {"threshold": [100.0, 10.0]},  # coordinate order is not semantic
        rank_by="sharpe",
        store=store,
    )["experiment"]
    assert repeated["semantic_hash"] == experiment["semantic_hash"]


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


# ── submission dedup (evidence reuse) ────────────────────────────────────────


def test_identical_submission_dedups_to_prior_green_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit's dominant redundancy class (71/107 identical grid runs in
    one burst): an identical submission against a green prior must skip
    compute entirely, journal the linkage, and surface the prior result."""
    store, job_id, root = _make_job(tmp_path)
    calls = _count_backtests(monkeypatch)

    first = run_experiment(job_id, {"threshold": [10.0, 100.0]}, store=store)
    assert len(calls) == 1
    assert first["experiment"]["submission_hash"]

    second = run_experiment(job_id, {"threshold": [10.0, 100.0]}, store=store)
    assert len(calls) == 1, "identical submission must skip compute"
    assert second["experiment"]["reused"] is True
    assert second["experiment"]["grid_id"] == first["experiment"]["grid_id"]
    backtest = second["backtest"]
    assert backtest["reused_from_grid_id"] == first["experiment"]["grid_id"]
    assert backtest["result"]["ranked"], "prior grid summary surfaced"
    reused = _journal_entries(store, job_id, "experiment_reused")
    assert len(reused) == 1
    assert reused[0]["grid_id"] == first["experiment"]["grid_id"]
    assert reused[0]["submission_hash"] == first["experiment"]["submission_hash"]
    assert len(list_experiments(job_id, store=store)) == 1, "no duplicate row"

    # Coordinate order is not semantic (#692) — reordering still dedups.
    reordered = run_experiment(job_id, {"threshold": [100.0, 10.0]}, store=store)
    assert len(calls) == 1
    assert reordered["experiment"]["reused"] is True


def test_changed_submission_runs_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root = _make_job(tmp_path)
    calls = _count_backtests(monkeypatch)
    run_experiment(job_id, {"threshold": [10.0, 100.0]}, store=store)

    # Different search coordinates → different question → runs.
    run_experiment(job_id, {"threshold": [10.0, 50.0]}, store=store)
    assert len(calls) == 2

    # Same coordinates but the workspace revision moved → runs.
    script = root / "workspace" / "src" / "strategy.py"
    script.write_text(
        script.read_text(encoding="utf-8") + "\n# tweak\n", encoding="utf-8"
    )
    run_experiment(job_id, {"threshold": [10.0, 100.0]}, store=store)
    assert len(calls) == 3

    # Dataset content moved → runs.
    bars_path = root / "results" / "backtest" / "input_bars.json"
    bars_path.write_text(
        json.dumps(json.loads(bars_path.read_text(encoding="utf-8")), indent=2),
        encoding="utf-8",
    )
    run_experiment(job_id, {"threshold": [10.0, 100.0]}, store=store)
    assert len(calls) == 4
    assert len(list_experiments(job_id, store=store)) == 4


def test_experiment_kill_switch_forces_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id, root = _make_job(tmp_path)
    calls = _count_backtests(monkeypatch)
    run_experiment(job_id, {"threshold": [10.0, 100.0]}, store=store)
    monkeypatch.setenv("WAYFINDER_EXPERIMENT_ALWAYS_RUN", "1")

    repeated = run_experiment(job_id, {"threshold": [10.0, 100.0]}, store=store)
    assert len(calls) == 2, "kill-switch must force the run"
    assert "reused" not in repeated["experiment"]
    assert len(list_experiments(job_id, store=store)) == 2
