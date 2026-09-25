from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from wayfinder_paths.jobs.sprite_bundle import (
    PHASE_OP,
    PHASE_PROTOCOL,
    SpriteWorkspace,
    extract_archive,
    pack_inputs,
    pack_job,
    sha256,
)
from wayfinder_paths.tests.test_jobs_preflight import _make_job


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", "file"),
        ("/absolute", "file"),
        ("link", "symlink"),
        ("fifo", "fifo"),
    ],
)
def test_archive_rejects_unsafe_entries(tmp_path: Path, name: str, kind: str) -> None:
    source = tmp_path / "bad.tar.gz"
    with tarfile.open(source, "w:gz") as tar:
        member = tarfile.TarInfo(name)
        if kind == "symlink":
            member.type, member.linkname = tarfile.SYMTYPE, "/tmp/other"
        elif kind == "fifo":
            member.type = tarfile.FIFOTYPE
        tar.addfile(member, io.BytesIO())
    with pytest.raises(ValueError):
        extract_archive(source, tmp_path / "out")


def test_bundle_does_not_copy_machine_config_or_follow_symlinks(tmp_path: Path) -> None:
    store, job_id, root = _make_job(tmp_path / "source")
    (store.repo_root / "config.json").write_text('{"api_key":"private"}')
    archive = tmp_path / "workspace.tar.gz"
    pack_job(store, job_id, archive)
    extract_archive(archive, tmp_path / "out")
    assert not (tmp_path / "out/config.json").exists()
    (root / "workspace/link").symlink_to(store.repo_root / "config.json")
    with pytest.raises(ValueError, match="symlink"):
        pack_job(store, job_id, archive)


@pytest.mark.parametrize("relative", ["../outside", "/outside", "nested/../../outside"])
def test_workspace_rejects_paths_outside_root(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError, match="Unsafe bundle path"):
        SpriteWorkspace(tmp_path).file(relative)


def test_workspace_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = SpriteWorkspace(tmp_path / "workspace")
    workspace.root.mkdir()
    workspace.file("link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes its root"):
        workspace.file("link/outside.txt")


def test_overlapping_inputs_are_packaged_once(tmp_path: Path) -> None:
    store, job_id, job_root = _make_job(tmp_path / "source")
    archive = tmp_path / "workspace.tar.gz"
    packed = pack_job(
        store,
        job_id,
        archive,
        extra_paths=[job_root.relative_to(store.repo_root).as_posix()],
    )
    with tarfile.open(archive) as bundle:
        names = bundle.getnames()
    assert len(names) == len(set(names)) == packed["files"]
    assert len(packed["request"]["files"]) == packed["files"] - 1


def _phase_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "data").mkdir(parents=True)
    (root / "data/prices.txt").write_text("1 2 3")
    (root / "data/unrelated.bin").write_bytes(b"x" * 1000)
    (root / "config.json").write_text('{"api_key":"private"}')
    return root


def test_pack_inputs_ships_only_named_inputs_and_the_phase_request(
    tmp_path: Path,
) -> None:
    root = _phase_root(tmp_path)
    archive = tmp_path / "inputs.tar.gz"
    packed = pack_inputs(
        root,
        ["data/prices.txt"],
        {"phase": "wayfinder_paths.example:phase", "args": {"scale": 2}},
        archive,
        expected_sdk_commit="a" * 40,
    )
    assert packed["request"] == {
        "protocol": PHASE_PROTOCOL,
        "op": PHASE_OP,
        "phase": "wayfinder_paths.example:phase",
        "args": {"scale": 2},
        "expected_sdk_commit": "a" * 40,
        "files": {"data/prices.txt": sha256(root / "data/prices.txt")},
    }
    assert packed["sha256"] == sha256(archive) and packed["files"] == 2
    extract_archive(archive, tmp_path / "out")
    assert sorted(
        path.relative_to(tmp_path / "out").as_posix()
        for path in (tmp_path / "out").rglob("*")
        if path.is_file()
    ) == ["data/prices.txt", "sprite-request.json"]


@pytest.mark.parametrize(
    "paths",
    [["../outside"], ["/etc/passwd"], ["outputs/previous.json"], ["config.json"]],
)
def test_pack_inputs_rejects_unsafe_or_output_inputs(
    tmp_path: Path, paths: list[str]
) -> None:
    root = _phase_root(tmp_path)
    (root / "outputs").mkdir()
    (root / "outputs/previous.json").write_text("{}")
    with pytest.raises(ValueError):
        pack_inputs(root, paths, {"phase": "p", "args": {}}, tmp_path / "a.tgz")


def test_pack_inputs_requires_finite_json_arguments(tmp_path: Path) -> None:
    root = _phase_root(tmp_path)
    with pytest.raises(ValueError):
        pack_inputs(
            root,
            ["data/prices.txt"],
            {"phase": "p", "args": {"x": float("nan")}},
            tmp_path / "a.tgz",
        )


def test_phase_workspace_collects_only_its_outputs(tmp_path: Path) -> None:
    root = _phase_root(tmp_path)
    pack_inputs(root, ["data"], {"phase": "p", "args": {}}, tmp_path / "in.tgz")
    workspace = SpriteWorkspace(tmp_path / "remote")
    extract_archive(tmp_path / "in.tgz", workspace.root)
    workspace.outputs_dir.mkdir()
    (workspace.outputs_dir / "model.bin").write_bytes(b"weights")
    info = workspace.collect(tmp_path / "artifacts.tgz")
    extract_archive(tmp_path / "artifacts.tgz", tmp_path / "collected")
    assert info["files"] == 1
    assert (tmp_path / "collected/outputs/model.bin").read_bytes() == b"weights"
    assert not (tmp_path / "collected/data").exists()
