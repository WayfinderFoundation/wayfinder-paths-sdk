from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.job import backtest_execution_job, validate_job
from wayfinder_paths.jobs.execution.preflight import run_preflight
from wayfinder_paths.jobs.gating import compute_workspace_revision, evaluate_live_gate
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import snapshot_job
from wayfinder_paths.tests.test_jobs_preflight import STRATEGY, _bars


def _make_job(
    tmp_path: Path, *, contract: str = "jobs_v1"
) -> tuple[JobStore, str, Path]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "gate-demo",
        script=".wayfinder/jobs/gate-demo/workspace/src/strategy.py",
        interval_seconds=300,
        execution_contract=contract,  # type: ignore[arg-type]
    )
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "5m"
    job.execution_spec = spec.to_dict()
    job.execution_params = {"symbols": ["SNX"]}
    store.save(job)
    root = store.job_dir(job.id)
    script = root / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(STRATEGY.lstrip(), encoding="utf-8")
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(_bars()), encoding="utf-8"
    )
    return store, job.id, root


def _run_full_gate_pipeline(store: JobStore, job_id: str) -> None:
    backtest_execution_job(job_id, store=store)
    run_preflight(job_id, store=store)
    validate_job(job_id, store=store)


def test_gate_passes_after_full_pipeline(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path)
    _run_full_gate_pipeline(store, job_id)

    gate = evaluate_live_gate(job_id, store=store)

    assert gate["live_ready"] is True, gate["reasons"]
    assert gate["revision"]
    assert gate["backtest"]["revision"] == gate["revision"]


def test_gate_fails_without_artifacts(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path)

    gate = evaluate_live_gate(job_id, store=store)

    assert gate["live_ready"] is False
    joined = " ".join(gate["reasons"])
    assert "validation" in joined
    assert "backtest" in joined
    assert "preflight" in joined


def test_gate_fails_when_workspace_changes_after_backtest(tmp_path: Path) -> None:
    store, job_id, root = _make_job(tmp_path)
    _run_full_gate_pipeline(store, job_id)
    script = root / "workspace" / "src" / "strategy.py"
    script.write_text(
        script.read_text(encoding="utf-8") + "\n# tweak\n", encoding="utf-8"
    )

    gate = evaluate_live_gate(job_id, store=store)

    assert gate["live_ready"] is False
    assert any("revision" in reason for reason in gate["reasons"])


def test_gate_refuses_legacy_contract(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path, contract="legacy")

    gate = evaluate_live_gate(job_id, store=store)

    assert gate["live_ready"] is False
    assert any("legacy" in reason for reason in gate["reasons"])


def test_candidate_revision_matches_promoted_revision(tmp_path: Path) -> None:
    """Promotion copies the candidate byte-for-byte, so a hash computed on the
    candidate dir must equal the post-promotion active revision."""
    store, job_id, root = _make_job(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    shutil.copytree(root / "workspace", candidate / "workspace")
    shutil.copy2(root / "job.yaml", candidate / "job.yaml")

    candidate_revision = compute_workspace_revision(candidate)

    shutil.rmtree(root / "workspace")
    shutil.copytree(candidate / "workspace", root / "workspace")
    (root / "job.yaml").write_bytes((candidate / "job.yaml").read_bytes())
    assert compute_workspace_revision(root) == candidate_revision


def test_snapshot_carries_gate_and_keeps_existing_keys(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path)
    _run_full_gate_pipeline(store, job_id)

    snapshot = snapshot_job(job_id, store=store)

    # Existing contract keys the backend sync view indexes must stay present.
    for key in (
        "job",
        "scorecard",
        "backtest",
        "forward",
        "runner_links",
        "proposals",
        "proposal_queue",
        "reports",
    ):
        assert key in snapshot, key
    assert snapshot["execution_contract"] == "jobs_v1"
    assert snapshot["validation"]["status"] == "passed"
    assert snapshot["gate"]["live_ready"] is True
    assert snapshot["gate"]["revision"]


def test_validation_report_is_revision_stamped(tmp_path: Path) -> None:
    store, job_id, root = _make_job(tmp_path)
    backtest_execution_job(job_id, store=store)
    run_preflight(job_id, store=store)

    report = validate_job(job_id, store=store)

    assert report["revision"] == compute_workspace_revision(root)


def _noop(value: Any) -> Any:
    return value
