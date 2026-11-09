"""Extraction and at-rest verification of installed bundles. Archives here
are built in memory and are inert: nothing in them is ever executed."""

import os
import stat
import zipfile
from pathlib import Path

import pytest

from wayfinder_paths.paths import install_safety
from wayfinder_paths.paths.install_safety import (
    PathInstallError,
    safe_extract_zip,
    safe_path_component,
    sha256_file,
    verify_installed_tree,
)


def _zip(path: Path, entries: list[tuple[str, bytes, int]]) -> Path:
    """entries: (name, data, unix mode or 0 for none recorded)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = mode << 16
            zf.writestr(info, data)
    return path


CLEAN = [
    ("wfpath.yaml", b"slug: demo\n", 0o644),
    ("skill/SKILL.md", b"# Demo\n", stat.S_IFREG | 0o644),
    ("scripts/run.py", b"print('hi')\n", 0o755),
    ("notes.txt", b"plain\n", 0),
]


def test_clean_bundle_extracts_without_exec_bits(tmp_path: Path) -> None:
    bundle = _zip(tmp_path / "b.zip", CLEAN)
    dest = tmp_path / "out"
    extracted = safe_extract_zip(bundle, dest_dir=dest)
    assert extracted == sorted(name for name, _, _ in CLEAN)
    run = dest / "scripts" / "run.py"
    assert run.read_bytes() == b"print('hi')\n"
    assert stat.S_IMODE(run.stat().st_mode) & 0o111 == 0


@pytest.mark.parametrize(
    "entries",
    [
        [("link", b"/etc/passwd", stat.S_IFLNK | 0o777)],
        [("fifo", b"", stat.S_IFIFO | 0o644)],
        [("suid.sh", b"echo", stat.S_IFREG | stat.S_ISUID | 0o755)],
        [("sgid.sh", b"echo", stat.S_ISGID | 0o755)],
        [("../escape.txt", b"x", 0)],
        [("a/../../escape.txt", b"x", 0)],
        [("/abs.txt", b"x", 0)],
        [("dir\\evil.txt", b"x", 0)],
        [("C:evil.txt", b"x", 0)],
        [("bundle.zip", b"x", 0)],
        [(".wf-state/x", b"x", 0)],
        [("install-intent.json", b"{}", 0)],
        [("Readme.md", b"a", 0), ("README.md", b"b", 0)],
        [("a", b"file", 0), ("a/b", b"nested", 0)],
    ],
    ids=[
        "symlink",
        "fifo",
        "setuid",
        "setgid",
        "dotdot",
        "nested-dotdot",
        "absolute",
        "backslash",
        "drive",
        "reserved-bundle",
        "reserved-state",
        "reserved-intent",
        "case-duplicate",
        "file-and-dir",
    ],
)
def test_unsafe_entries_are_refused_and_nothing_escapes(
    tmp_path: Path, entries
) -> None:
    bundle = _zip(tmp_path / "b.zip", entries)
    with pytest.raises(PathInstallError):
        safe_extract_zip(bundle, dest_dir=tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_zip_bomb_and_entry_count_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bomb = _zip(tmp_path / "bomb.zip", [("zeros.bin", b"\0" * (4 * 1024 * 1024), 0)])
    with pytest.raises(PathInstallError, match="compressed"):
        safe_extract_zip(bomb, dest_dir=tmp_path / "bomb")

    many = _zip(tmp_path / "many.zip", [(f"f{i}.txt", b"x", 0) for i in range(11)])
    monkeypatch.setattr(install_safety, "MAX_ENTRIES", 10)
    with pytest.raises(PathInstallError, match="entries"):
        safe_extract_zip(many, dest_dir=tmp_path / "many")

    monkeypatch.setattr(install_safety, "MAX_ENTRIES", 2000)
    monkeypatch.setattr(install_safety, "MAX_TOTAL_BYTES", 5)
    with pytest.raises(PathInstallError, match="size limit"):
        safe_extract_zip(many, dest_dir=tmp_path / "big")


def test_extract_refuses_an_existing_destination(tmp_path: Path) -> None:
    bundle = _zip(tmp_path / "b.zip", CLEAN)
    (tmp_path / "out").mkdir()
    with pytest.raises(FileExistsError):
        safe_extract_zip(bundle, dest_dir=tmp_path / "out")


@pytest.mark.parametrize(
    "value", ["", ".", "..", "../x", "a/b", "-flag", "a b", "x" * 200]
)
def test_unsafe_slug_or_version_is_refused(value: str) -> None:
    with pytest.raises(PathInstallError):
        safe_path_component(value, label="slug")


def _installed(tmp_path: Path) -> tuple[Path, str]:
    install_dir = tmp_path / "paths" / "demo" / "1.0.0"
    bundle = _zip(tmp_path / "b.zip", CLEAN)
    safe_extract_zip(bundle, dest_dir=install_dir)
    bundle.replace(install_dir / "bundle.zip")
    return install_dir, sha256_file(install_dir / "bundle.zip")


def test_verify_accepts_an_untouched_install_and_runtime_files(tmp_path: Path) -> None:
    install_dir, sha = _installed(tmp_path)
    (install_dir / ".wf-state").mkdir()
    (install_dir / ".wf-state" / "run.json").write_text("{}")
    (install_dir / "scripts" / "__pycache__").mkdir()
    (install_dir / "scripts" / "__pycache__" / "run.cpython-312.pyc").write_bytes(b"x")
    (install_dir / "install-intent.json").write_text("{}")
    assert verify_installed_tree(install_dir=install_dir, expected_sha256=sha) == []


def test_verify_warns_about_files_not_in_the_bundle(tmp_path: Path) -> None:
    install_dir, sha = _installed(tmp_path)
    (install_dir / "skill" / "extra.md").write_text("added later")
    assert verify_installed_tree(install_dir=install_dir, expected_sha256=sha) == [
        "File not in the reviewed bundle: skill/extra.md"
    ]


@pytest.mark.parametrize(
    "tamper",
    ["bundle", "file", "missing", "symlink", "exec", "no-sha"],
)
def test_verify_refuses_a_tampered_install(tmp_path: Path, tamper: str) -> None:
    install_dir, sha = _installed(tmp_path)
    target = install_dir / "scripts" / "run.py"
    if tamper == "bundle":
        with (install_dir / "bundle.zip").open("ab") as handle:
            handle.write(b"\0")
    elif tamper == "file":
        target.write_bytes(b"print('tampered')\n")
    elif tamper == "missing":
        target.unlink()
    elif tamper == "symlink":
        target.unlink()
        os.symlink(tmp_path / "b.zip", target)
    elif tamper == "exec":
        target.chmod(0o755)
    elif tamper == "no-sha":
        sha = ""
    with pytest.raises(PathInstallError):
        verify_installed_tree(install_dir=install_dir, expected_sha256=sha)
