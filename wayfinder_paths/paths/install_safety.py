"""Safe extraction and at-rest verification of installed path bundles.

A path bundle is untrusted input even after review: it is checked against the
sha256 the registry published before anything is extracted, extracted only into
a fresh staging directory, and re-checked against its lockfile entry before
every activation (including the boot-time restore on Shells boxes).
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, ZipInfo

MAX_ENTRIES = 2000
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
# Small files compress well legitimately; the ratio only applies above this.
RATIO_FLOOR_BYTES = 1024 * 1024
_CHUNK = 1024 * 1024

# Created inside an installed path by the SDK or the running path, never by the
# bundle: install writes the first two, and the bundle builder excludes the
# directories (builder._DEFAULT_IGNORE_DIRS). Their contents are not part of
# the reviewed artifact, and a bundle that contains them is refused.
RUNTIME_ENTRIES = frozenset(
    {
        "bundle.zip",
        "install-intent.json",
        ".build",
        ".venv",
        ".wayfinder",
        ".wayfinder_runs",
        ".wf-artifacts",
        ".wf-state",
    }
)
# Interpreter caches, ignored wherever they appear when looking for extra files.
CACHE_DIRS = frozenset({"__pycache__"})

_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")


class PathInstallError(Exception):
    pass


def safe_path_component(value: str, *, label: str) -> str:
    """A slug or version that is safe to use as one directory name."""
    if not _SAFE_COMPONENT.fullmatch(value or "") or value in {".", ".."}:
        raise PathInstallError(f"Invalid path {label}: {value!r}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _member_path(info: ZipInfo) -> PurePosixPath | None:
    """The member's relative path, None for a directory entry; raises for
    anything that isn't a plain relative file path."""
    name = info.filename
    if info.is_dir():
        return None
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or ":" in name
        or name.startswith("/")
        or any(ord(char) < 32 for char in name)
    ):
        raise PathInstallError(f"Bundle contains an unsafe path: {name!r}")
    rel = PurePosixPath(name)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise PathInstallError(f"Bundle contains an unsafe path: {name!r}")
    if rel.parts[0] in RUNTIME_ENTRIES:
        raise PathInstallError(f"Bundle contains a reserved path: {name!r}")
    # Unix mode bits, when the archiver recorded them. Many writers (Python's
    # zipfile, the backend rebuild) store permissions without a file type, so
    # only a type other than a regular file is refused.
    mode = info.external_attr >> 16
    if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
        raise PathInstallError(f"Bundle contains a link or special file: {name!r}")
    if mode & (stat.S_ISUID | stat.S_ISGID):
        raise PathInstallError(f"Bundle contains a setuid or setgid file: {name!r}")
    if info.flag_bits & 0x1:
        raise PathInstallError(f"Bundle contains an encrypted entry: {name!r}")
    return rel


def _checked_members(zf: ZipFile) -> list[tuple[ZipInfo, PurePosixPath]]:
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise PathInstallError(f"Bundle has more than {MAX_ENTRIES} entries")
    members = []
    seen: set[str] = set()
    directories: set[str] = set()
    total = 0
    for info in infos:
        rel = _member_path(info)
        if rel is None:
            continue
        key = rel.as_posix().casefold()
        if key in seen or key in directories:
            raise PathInstallError(f"Bundle contains a duplicate path: {rel}")
        seen.add(key)
        directories.update(parent.as_posix().casefold() for parent in rel.parents)
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise PathInstallError("Bundle exceeds the uncompressed size limit")
        if (
            info.file_size > RATIO_FLOOR_BYTES
            and info.file_size > MAX_COMPRESSION_RATIO * max(info.compress_size, 1)
        ):
            raise PathInstallError(f"Bundle entry is too highly compressed: {rel}")
        members.append((info, rel))
    if seen & directories:
        raise PathInstallError("Bundle contains a file that is also a directory")
    return members


def safe_extract_zip(zip_path: Path, *, dest_dir: Path) -> list[str]:
    """Extract every regular file of ``zip_path`` into ``dest_dir``, which must
    not exist yet. Raises on the first unsafe entry; the caller discards the
    directory. Files are written 0644, never executable."""
    dest_dir.mkdir(parents=True, exist_ok=False)
    root = dest_dir.resolve()
    extracted = []
    budget = MAX_TOTAL_BYTES
    with ZipFile(zip_path, "r") as zf:
        for info, rel in _checked_members(zf):
            target = root.joinpath(*rel.parts)
            if root not in target.parents:
                raise PathInstallError(
                    f"Bundle entry escapes the install directory: {rel}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.parent.resolve() != target.parent:
                raise PathInstallError(
                    f"Bundle entry escapes the install directory: {rel}"
                )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            written = 0
            with (
                zf.open(info, "r") as src,
                os.fdopen(os.open(target, flags, 0o644), "wb") as out,
            ):
                for chunk in iter(lambda: src.read(_CHUNK), b""):
                    written += len(chunk)
                    budget -= len(chunk)
                    # Never trust the declared size alone.
                    if written > info.file_size or budget < 0:
                        raise PathInstallError(
                            f"Bundle entry is larger than declared: {rel}"
                        )
                    out.write(chunk)
            extracted.append(rel.as_posix())
    extracted.sort()
    return extracted


def verify_installed_tree(*, install_dir: Path, expected_sha256: str) -> list[str]:
    """Check an installed path before activation: ``bundle.zip`` still has the
    sha256 recorded at install, and every bundle file on disk is byte-identical
    to the bundle. Raises on any difference; returns warnings for extra files
    the bundle didn't contain (outside the runtime directories)."""
    bundle = install_dir / "bundle.zip"
    expected = str(expected_sha256 or "").strip().lower()
    if not expected:
        raise PathInstallError(
            "Installed path has no recorded bundle sha256; reinstall it"
        )
    if not bundle.is_file() or bundle.is_symlink():
        raise PathInstallError("Installed path is missing its bundle; reinstall it")
    if sha256_file(bundle) != expected:
        raise PathInstallError(
            "Installed bundle does not match its recorded sha256; reinstall it"
        )
    root = install_dir.resolve()
    members: set[str] = set()
    with ZipFile(bundle, "r") as zf:
        for info, rel in _checked_members(zf):
            members.add(rel.as_posix())
            on_disk = root.joinpath(*rel.parts)
            if on_disk.is_symlink() or not on_disk.is_file():
                raise PathInstallError(f"Installed file is missing or replaced: {rel}")
            if on_disk.stat().st_mode & (
                stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH | stat.S_ISUID | stat.S_ISGID
            ):
                raise PathInstallError(f"Installed file became executable: {rel}")
            digest = hashlib.sha256()
            with zf.open(info, "r") as src:
                for chunk in iter(lambda: src.read(_CHUNK), b""):
                    digest.update(chunk)
            if sha256_file(on_disk) != digest.hexdigest():
                raise PathInstallError(f"Installed file was modified: {rel}")
    warnings = []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(current)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in CACHE_DIRS and not (here == root and name in RUNTIME_ENTRIES)
        ]
        names = filenames + [name for name in dirnames if (here / name).is_symlink()]
        for name in names:
            rel = (here / name).relative_to(root)
            if rel.parts[0] in RUNTIME_ENTRIES:
                continue
            if rel.as_posix() not in members:
                warnings.append(f"File not in the reviewed bundle: {rel.as_posix()}")
    return sorted(warnings)
