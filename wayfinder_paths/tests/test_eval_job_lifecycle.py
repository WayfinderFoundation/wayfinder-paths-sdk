"""The lifecycle evals stay runnable deterministically: every case's expected
artifacts pass its validator, and the validators reject the wrong thing."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def load_module():
    path = REPO / "scripts" / "eval_job_lifecycle.py"
    spec = importlib.util.spec_from_file_location("eval_job_lifecycle", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_job_lifecycle"] = module
    spec.loader.exec_module(module)
    return module


def test_every_lifecycle_stage_has_a_case() -> None:
    module = load_module()
    assert {case.stage for case in module.CASES} == set(module.STAGES)


@pytest.mark.parametrize("case_id", [c.id for c in load_module().CASES])
def test_expected_artifacts_pass_their_validator(tmp_path: Path, case_id: str) -> None:
    module = load_module()
    case = next(c for c in module.CASES if c.id == case_id)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    case.expected(workspace)
    report = case.validate(workspace)
    assert report["status"] == "passed", report["failed"]


def test_validators_reject_a_launched_creation_case(tmp_path: Path) -> None:
    module = load_module()
    case = next(c for c in module.CASES if c.id == "freestyle_hormuz_created")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    case.expected(workspace)
    from wayfinder_paths.jobs.launch import launch_job
    from wayfinder_paths.jobs.store import JobStore

    with module.Sandbox():
        launch_job(case.job_id, store=JobStore(repo_root=workspace))
    report = case.validate(workspace)
    assert report["status"] == "failed" and "not_launched" in report["failed"]


def test_run_case_writes_a_report(tmp_path: Path) -> None:
    module = load_module()
    case = next(c for c in module.CASES if c.id == "evolution_cadence")
    result = module.run_case(case, live=False, judge=False, output_dir=tmp_path / "out")
    assert result["status"] == "passed"
    assert (tmp_path / "out" / case.id / "validator.json").exists()
    assert (
        tmp_path
        / "out"
        / case.id
        / "workspace"
        / ".wayfinder"
        / "jobs"
        / case.job_id
        / "job.yaml"
    ).exists()
