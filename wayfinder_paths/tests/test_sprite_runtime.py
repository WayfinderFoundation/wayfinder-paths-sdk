from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from wayfinder_paths.jobs import sprite_runtime
from wayfinder_paths.jobs.compute_phase import compute_phase, phase_name
from wayfinder_paths.jobs.sprite_bundle import (
    SpriteWorkspace,
    WorkspaceRequest,
    extract_archive,
    pack_base,
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


def _based_phase(tmp_path: Path, **args: Any) -> tuple[Path, Path]:
    """A phase reading prices from a base archive and its scale from the delta."""
    root = tmp_path / "source"
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data/prices.txt").write_text("1 2 3")
    (root / "data/large-dataset.bin").write_bytes(b"d" * 100000)
    (root / "candidate").mkdir(exist_ok=True)
    (root / "candidate/scale.txt").write_text("2")
    base = pack_base(root, ["data"], tmp_path / "base.tar.gz")
    pack_inputs(
        root,
        ["candidate"],
        {
            "phase": phase_name(phases.score_prices),
            "args": {"prices": "data/prices.txt", "scale": 2, **args},
        },
        tmp_path / "inputs.tar.gz",
        base=base,
    )
    return tmp_path / "inputs.tar.gz", tmp_path / "base.tar.gz"


def test_runtime_extracts_the_base_under_the_phase_inputs(tmp_path: Path) -> None:
    archive, base = _based_phase(tmp_path)
    summary, artifacts = tmp_path / "summary.json", tmp_path / "artifacts.tar.gz"
    with pytest.raises(SystemExit) as exited:
        main(
            [
                "--bundle",
                str(archive),
                "--base",
                str(base),
                "--root",
                str(tmp_path / "remote"),
                "--output",
                str(summary),
                "--artifacts",
                str(artifacts),
            ]
        )
    assert exited.value.code == 0
    assert json.loads(summary.read_text())["summary"]["total"] == 12.0
    # The base, like every input, stays on the worker.
    assert _collected(tmp_path, artifacts) == [
        "outputs/phase-result.json",
        "outputs/scaled.txt",
    ]


def _failure(tmp_path: Path, archive: Path, base: Path | None) -> dict[str, Any]:
    summary, artifacts = tmp_path / "summary.json", tmp_path / "artifacts.tar.gz"
    remote = tmp_path / f"remote-{len(list(tmp_path.glob('remote-*')))}"
    assert run(archive, remote, summary, artifacts, base=base) == 1
    assert _collected(tmp_path / remote.name, artifacts) == ["outputs/phase-error.json"]
    return json.loads(summary.read_text())


def test_a_base_failure_is_a_prepare_failure(tmp_path: Path) -> None:
    archive, base = _based_phase(tmp_path)
    missing = _failure(tmp_path, archive, None)
    assert missing["stage"] == "prepare"
    assert "Base archive does not match" in missing["error"]
    staging = tmp_path / "staging"
    extract_archive(base, staging)
    (staging / "data/prices.txt").write_text("9 9 9")
    tampered = tmp_path / "tampered.tar.gz"
    write_archive(staging, workspace_files(staging, [staging]), tampered)
    corrupt = _failure(tmp_path, archive, tampered)
    assert corrupt["stage"] == "prepare"
    assert "checksum mismatch: data/prices.txt" in corrupt["error"]
    # A path present in both archives is refused before anything is written.
    (staging / "candidate").mkdir()
    (staging / "candidate/scale.txt").write_text("2")
    overlapping = tmp_path / "overlapping.tar.gz"
    write_archive(staging, workspace_files(staging, [staging]), overlapping)
    both = _failure(tmp_path, archive, overlapping)
    assert both["stage"] == "prepare"
    assert "present in both archives: candidate/scale.txt" in both["error"]


def test_a_base_without_base_files_in_the_request_is_refused(tmp_path: Path) -> None:
    _, base = _based_phase(tmp_path)
    archive = _phase_archive(tmp_path / "plain", phases.score_prices, {})
    unexpected = _failure(tmp_path, archive, base)
    assert "present in both archives" in unexpected["error"]
    root = tmp_path / "other"
    (root / "extra").mkdir(parents=True)
    (root / "extra/file.txt").write_text("x")
    other = tmp_path / "other.tar.gz"
    pack_base(root, ["extra"], other)
    mismatched = _failure(tmp_path, archive, other)
    assert "Base archive does not match" in mismatched["error"]


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    messages: list[str] = []
    monkeypatch.setattr(sprite_runtime, "_warned", set())
    monkeypatch.setattr(sprite_runtime.logger, "warning", messages.append)
    return messages


def test_a_checkout_offloads_its_head_and_warns_when_the_sprite_cannot_match_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warnings: list[str]
) -> None:
    root = tmp_path / "sdk"
    (root / "wayfinder_paths").mkdir(parents=True)
    (root / "wayfinder_paths" / "__init__.py").write_text("")
    _git(tmp_path, "init", "-q", str(root))
    _git(root, "add", ".")
    _git(
        root, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "sdk"
    )
    monkeypatch.setattr(sprite_runtime, "_SDK_ROOT", root)
    head = _git(root, "rev-parse", "HEAD")
    assert sprite_runtime.node_sdk_commit() == sprite_runtime.node_sdk_commit() == head
    assert (
        warnings
        == [  # Once per process.
            f"SDK commit {head[:12]} is not on any remote branch; push it, or its Sprite install fails"
        ]
    )
    _git(tmp_path, "init", "-q", "--bare", str(tmp_path / "remote.git"))
    _git(root, "remote", "add", "origin", str(tmp_path / "remote.git"))
    _git(root, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git(root, "fetch", "-q", "origin")
    warnings.clear()
    assert sprite_runtime.node_sdk_commit() == head and not warnings
    # Local commits on top of main: the nearest main ancestor, which GitHub has.
    for message in ("local one", "local two"):
        _git(
            root,
            "-c",
            "user.email=a@b.c",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            message,
        )
    local = _git(root, "rev-parse", "HEAD")
    assert sprite_runtime.node_sdk_commit() == head
    assert warnings == [
        f"SDK commit {local[:12]} is not pushed; offloaded jobs run its nearest main "
        f"ancestor {head[:12]}, without its 2 newer commit(s)"
    ]
    warnings.clear()
    (root / "wayfinder_paths" / "__init__.py").write_text("# changed")
    assert sprite_runtime.node_sdk_commit() == head
    assert warnings == [
        f"Offloaded jobs run SDK commit {head[:12]} without this checkout's uncommitted changes"
    ]
    # A pin check never accepts modified code as the commit.
    assert sprite_runtime.installed_sdk_commit() is None
    marker = root / ".sdk-commit"
    marker.write_text("")  # An empty marker names nothing: the checkout decides.
    assert sprite_runtime.node_sdk_commit() == head
    marker.write_text("c" * 40 + "\n")
    assert (
        sprite_runtime.node_sdk_commit()
        == sprite_runtime.installed_sdk_commit()
        == "c" * 40
    )


class _Distribution:
    def __init__(self, direct_url: str | None, version: str = "0.11.1") -> None:
        self.direct_url, self.version = direct_url, version

    def read_text(self, name: str) -> str | None:
        return self.direct_url if name == "direct_url.json" else None


def test_installed_packages_name_their_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warnings: list[str]
) -> None:
    monkeypatch.setattr(
        sprite_runtime, "_SDK_ROOT", tmp_path
    )  # No marker, no checkout.
    commit = "d" * 40
    git_install = json.dumps(
        {"url": "https://github.com/x", "vcs_info": {"vcs": "git", "commit_id": commit}}
    )
    monkeypatch.setattr(
        sprite_runtime.importlib.metadata,
        "distribution",
        lambda name: _Distribution(git_install),
    )
    assert sprite_runtime.node_sdk_commit() == commit
    # A PyPI release resolves its v<version> tag on GitHub, once.
    requests: list[str] = []

    def github(url: str, **kwargs: Any) -> httpx.Response:
        requests.append(url)
        return httpx.Response(
            200, json={"sha": "e" * 40}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(sprite_runtime.httpx, "get", github)
    monkeypatch.setattr(
        sprite_runtime.importlib.metadata,
        "distribution",
        lambda name: _Distribution(None, "9.9.9"),
    )
    sprite_runtime._release_commit.cache_clear()
    assert (
        sprite_runtime.node_sdk_commit() == sprite_runtime.node_sdk_commit() == "e" * 40
    )
    assert requests == [
        f"https://api.github.com/repos/{sprite_runtime.SDK_REPOSITORY}/commits/v9.9.9"
    ]
    monkeypatch.setattr(
        sprite_runtime.httpx,
        "get",
        lambda url, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")),
    )
    monkeypatch.setattr(
        sprite_runtime.importlib.metadata,
        "distribution",
        lambda name: _Distribution(None, "9.9.8"),
    )
    assert sprite_runtime.node_sdk_commit() is None
    assert warnings == [
        "Could not resolve SDK release v9.9.8 to a commit; jobs run locally"
    ]
