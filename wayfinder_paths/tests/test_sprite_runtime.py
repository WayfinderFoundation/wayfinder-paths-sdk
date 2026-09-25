from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs.compute_phase import compute_phase, phase_name
from wayfinder_paths.jobs.sprite_bundle import (
    SpriteWorkspace,
    WorkspaceRequest,
    extract_archive,
    pack_inputs,
    pack_job,
    workspace_files,
    write_archive,
)
from wayfinder_paths.jobs.sprite_runtime import execute_operation, main, prepare, run
from wayfinder_paths.tests import compute_phase_fixtures as phases
from wayfinder_paths.tests.test_jobs_preflight import _make_job


def test_runtime_rejects_wrong_sdk_commit_before_computation(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path / "source")
    archive = tmp_path / "workspace.tar.gz"
    pack_job(store, job_id, archive, expected_sdk_commit="0" * 40)
    with pytest.raises(ValueError, match="SDK commit differs"):
        prepare(archive, tmp_path / "remote")


def test_runtime_rejects_corrupted_workspace_file(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path / "source")
    archive = tmp_path / "workspace.tar.gz"
    pack_job(store, job_id, archive)
    staging = tmp_path / "staging"
    extract_archive(archive, staging)
    (staging / ".wayfinder/jobs" / job_id / "job.yaml").write_text("corrupted")
    write_archive(staging, workspace_files(staging, [staging]), archive)
    with pytest.raises(ValueError, match="Workspace checksum mismatch"):
        prepare(archive, tmp_path / "remote")


@pytest.mark.parametrize("fails", [False, True])
def test_runtime_restores_process_state_and_collects_partial_artifacts(
    tmp_path: Path, fails: bool
) -> None:
    store, job_id, _ = _make_job(tmp_path / "source")
    archive = tmp_path / "workspace.tar.gz"
    pack_job(store, job_id, archive, op="script")
    remote = SpriteWorkspace(tmp_path / "remote")
    summary = tmp_path / "summary.json"
    artifacts = tmp_path / "artifacts.tar.gz"
    previous_cwd = Path.cwd()
    previous_path = sys.path[:]
    previous_argv = sys.argv[:]

    def execute(request: WorkspaceRequest, root: Path) -> dict[str, Any]:
        assert root == remote.root == Path.cwd()
        assert request["job_id"] == job_id
        remote.file("partial.txt").write_text("preserve this")
        sys.path.append("test-only-import-path")
        sys.argv.append("test-only-argument")
        sys.argv = ["test-only-script"]
        if fails:
            raise RuntimeError("operation failed")
        return {"undefined": float("nan"), "completed": True}

    code = run(archive, remote.root, summary, artifacts, executor=execute)
    assert code == int(fails)
    assert Path.cwd() == previous_cwd
    assert sys.path == previous_path
    assert sys.argv == previous_argv
    collected = SpriteWorkspace(tmp_path / "collected")
    extract_archive(artifacts, collected.root)
    assert collected.file("partial.txt").read_text() == "preserve this"
    result = json.loads(summary.read_text())
    if fails:
        assert result["error"] == "operation failed"
        assert collected.error_file.is_file()
    else:
        assert result["summary"] == {"undefined": None, "completed": True}
        assert "NaN" in collected.result_file.read_text()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_script_context_restores_arguments_and_import_paths(
    tmp_path: Path, exit_code: int
) -> None:
    store, job_id, _ = _make_job(tmp_path / "source")
    script = store.repo_root / "script.py"
    script.write_text(
        "import sys\nassert sys.argv[1:] == ['argument']\n"
        f"raise SystemExit({exit_code})\n"
    )
    packed = pack_job(
        store,
        job_id,
        tmp_path / "workspace.tar.gz",
        op="script",
        options={"path": "script.py", "argv": ["argument"]},
        extra_paths=["script.py"],
    )
    previous_cwd = Path.cwd()
    previous_path = sys.path[:]
    previous_argv = sys.argv[:]
    if exit_code:
        with pytest.raises(RuntimeError, match=f"status {exit_code}"):
            execute_operation(packed["request"], store.repo_root)
    else:
        assert execute_operation(packed["request"], store.repo_root) == {
            "script": "script.py",
            "finished": True,
        }
    assert Path.cwd() == previous_cwd
    assert sys.path == previous_path
    assert sys.argv == previous_argv


def _phase_archive(
    tmp_path: Path, phase: Any, args: dict[str, Any], **kwargs: Any
) -> Path:
    root = tmp_path / "source"
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data/prices.txt").write_text("1 2 3")
    (root / "data/large-dataset.bin").write_bytes(b"d" * 100000)
    archive = tmp_path / "inputs.tar.gz"
    pack_inputs(
        root,
        ["data"],
        {"phase": phase if isinstance(phase, str) else phase_name(phase), "args": args},
        archive,
        **kwargs,
    )
    return archive


def _collected(tmp_path: Path, artifacts: Path) -> list[str]:
    destination = tmp_path / "collected"
    extract_archive(artifacts, destination)
    return sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    )


def test_phase_runs_registered_function_and_returns_only_outputs(
    tmp_path: Path,
) -> None:
    archive = _phase_archive(
        tmp_path, phases.score_prices, {"prices": "data/prices.txt", "scale": 2}
    )
    summary, artifacts = tmp_path / "summary.json", tmp_path / "artifacts.tar.gz"
    assert run(archive, tmp_path / "remote", summary, artifacts) == 0
    result = json.loads(summary.read_text())
    assert result == {
        "operation": "evolution_phase",
        "phase": phase_name(phases.score_prices),
        "summary": {"count": 3, "total": 12.0, "undefined": None},
        "full_result": "outputs/phase-result.json",
    }
    # The dataset never round-trips: only the outputs directory comes back.
    assert _collected(tmp_path, artifacts) == [
        "outputs/phase-result.json",
        "outputs/scaled.txt",
    ]
    assert "NaN" in (tmp_path / "collected/outputs/phase-result.json").read_text()


def test_failed_phase_keeps_partial_outputs_and_its_error(tmp_path: Path) -> None:
    archive = _phase_archive(tmp_path, phases.rejected_candidate, {})
    summary, artifacts = tmp_path / "summary.json", tmp_path / "artifacts.tar.gz"
    assert run(archive, tmp_path / "remote", summary, artifacts) == 1
    assert json.loads(summary.read_text())["error_type"] == "ValueError"
    assert json.loads(summary.read_text())["stage"] == "execute"
    assert _collected(tmp_path, artifacts) == [
        "outputs/diagnostics.txt",
        "outputs/phase-error.json",
    ]
    error = json.loads((tmp_path / "collected/outputs/phase-error.json").read_text())
    assert error["error"] == "candidate violates its contract"


@pytest.mark.parametrize(
    "phase,match",
    [
        (phase_name(phases.unregistered), "Unregistered compute phase"),
        ("os:system", "Unknown compute phase"),
        ("wayfinder_paths.tests.missing_module:phase", "No module named"),
    ],
)
def test_runtime_only_dispatches_registered_sdk_phases(
    tmp_path: Path, phase: str, match: str
) -> None:
    archive = _phase_archive(tmp_path, phase, {})
    summary, artifacts = tmp_path / "summary.json", tmp_path / "artifacts.tar.gz"
    assert run(archive, tmp_path / "remote", summary, artifacts) == 1
    error = json.loads(summary.read_text())
    assert match in error["error"] and error["stage"] == "prepare"
    assert _collected(tmp_path, artifacts) == ["outputs/phase-error.json"]


def test_phase_rejects_wrong_sdk_commit_and_tampered_inputs(tmp_path: Path) -> None:
    archive = _phase_archive(
        tmp_path, phases.score_prices, {}, expected_sdk_commit="0" * 40
    )
    summary, artifacts = tmp_path / "summary.json", tmp_path / "artifacts.tar.gz"
    assert run(archive, tmp_path / "remote", summary, artifacts) == 1
    assert "SDK commit differs" in json.loads(summary.read_text())["error"]
    archive = _phase_archive(tmp_path, phases.score_prices, {})
    staging = tmp_path / "staging"
    extract_archive(archive, staging)
    (staging / "data/prices.txt").write_text("9 9 9")
    write_archive(staging, workspace_files(staging, [staging]), archive)
    assert run(archive, tmp_path / "remote-2", summary, artifacts) == 1
    assert "checksum mismatch" in json.loads(summary.read_text())["error"]


def test_collect_only_after_a_killed_phase_returns_only_outputs(
    tmp_path: Path,
) -> None:
    archive = _phase_archive(tmp_path, phases.score_prices, {})
    remote = SpriteWorkspace(tmp_path / "remote")
    extract_archive(archive, remote.root)
    remote.outputs_dir.mkdir()
    (remote.outputs_dir / "partial.json").write_text("{}")
    artifacts = tmp_path / "artifacts.tar.gz"
    main(
        [
            "--bundle",
            str(archive),
            "--root",
            str(remote.root),
            "--output",
            str(tmp_path / "summary.json"),
            "--artifacts",
            str(artifacts),
            "--collect-only",
        ]
    )
    assert _collected(tmp_path, artifacts) == ["outputs/partial.json"]


def test_phases_must_be_module_level_sdk_functions() -> None:
    def local(inputs: Path, outputs: Path, args: dict[str, Any]) -> None:
        return None

    with pytest.raises(ValueError, match="module-level"):
        compute_phase(local)
