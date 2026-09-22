from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from wayfinder_paths.jobs.sprite_bundle import (
    SpriteWorkspace,
    extract_archive,
    pack_job,
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
