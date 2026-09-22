from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs.sprite_bundle import (
    SpriteWorkspace,
    WorkspaceRequest,
    extract_archive,
    pack_job,
    workspace_files,
    write_archive,
)
from wayfinder_paths.jobs.sprite_runtime import execute_operation, prepare, run
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
