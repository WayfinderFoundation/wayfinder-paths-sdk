"""Portable job workspaces for remote computation; no machine credentials."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict

from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import safe_job_id
from wayfinder_paths.jobs.store import JobStore

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 20000
PROTOCOL = "wayfinder-sprite-job-v1"
OPERATIONS = frozenset(
    {
        "backtest_job",
        "experiments",
        "robustness_check",
        "validate_job",
        "preflight",
        "pair_check",
        "signal_check",
        "signal_scan",
        "holdout_check",
        "rank_check",
        "derive_features",
        "attribution",
        "chart",
        "analogs",
        "script",
    }
)
_SKIP = {"__pycache__", ".git", ".venv", "background_ops", "running_ops"}
_FORBIDDEN = {".env", "config.json", "wallets.json", "credentials.json"}


class ArchiveMetadata(TypedDict):
    sha256: str
    size: int


class ArchiveInfo(ArchiveMetadata):
    files: int


class WorkspaceRequest(TypedDict):
    protocol: str
    job_id: str
    op: str
    options: dict[str, Any]
    source_root: str
    source_revision: str
    expected_sdk_commit: str | None
    files: dict[str, str]


class PackedJob(ArchiveInfo):
    request: WorkspaceRequest


@dataclass(frozen=True, slots=True)
class SpriteWorkspace:
    """Portable workspace layout shared by packaging and execution."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    def file(self, relative: str) -> Path:
        path = self.root / safe_relative(relative)
        if not path.resolve().is_relative_to(self.root):
            raise ValueError(f"Workspace path escapes its root: {relative}")
        return path

    @property
    def request_file(self) -> Path:
        return self.root / "sprite-request.json"

    @property
    def result_file(self) -> Path:
        return self.root / "operation-result.json"

    @property
    def error_file(self) -> Path:
        return self.root / "operation-error.json"

    def archive(self, destination: Path) -> ArchiveInfo:
        return write_archive(
            self.root, workspace_files(self.root, [self.root]), destination
        )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative(value: str) -> Path:
    path = PurePosixPath(value)
    if (
        not value
        or not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError(f"Unsafe bundle path: {value!r}")
    if any(part in _SKIP for part in path.parts):
        raise ValueError(f"Runtime/cache directory is not portable: {value}")
    return Path(*path.parts)


def write_archive(root: Path, files: list[Path], destination: Path) -> ArchiveInfo:
    lexical_root = root
    root = root.resolve()
    files = sorted(set(files))
    if len(files) > MAX_FILES:
        raise ValueError("Workspace has too many files")
    size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz", dereference=False) as archive:
        for file in files:
            relative = file.relative_to(lexical_root)
            safe_relative(relative.as_posix())
            if (
                file.is_symlink()
                or not file.is_file()
                or not file.resolve().is_relative_to(root)
            ):
                raise ValueError(f"Bundle entries must be regular files: {relative}")
            size += file.stat().st_size
            if size > MAX_EXPANDED_BYTES:
                raise ValueError("Expanded workspace exceeds 2 GiB")
            archive.add(file, arcname=relative.as_posix(), recursive=False)
    if destination.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Compressed workspace exceeds 512 MiB")
    return {
        "sha256": sha256(destination),
        "size": destination.stat().st_size,
        "files": len(files),
    }


def extract_archive(source: Path, destination: Path) -> None:
    """Validate the entire archive before writing; never allow links/devices."""
    if source.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Compressed workspace exceeds 512 MiB")
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    total = 0
    names: set[Path] = set()
    with tarfile.open(source, "r:gz") as archive:
        members: list[tarfile.TarInfo] = []
        for member in archive:
            relative = safe_relative(member.name)
            if not member.isfile() or relative in names:
                raise ValueError(
                    "Archive contains a link, directory, special file, or duplicate"
                )
            if not (root / relative).resolve().is_relative_to(root):
                raise ValueError("Archive escapes destination")
            names.add(relative)
            total += member.size
            if len(names) > MAX_FILES or total > MAX_EXPANDED_BYTES:
                raise ValueError("Archive expansion limit exceeded")
            members.append(member)
        archive.extractall(root, members=members, filter="data")


def workspace_files(root: Path, paths: list[Path]) -> list[Path]:
    """Select portable files while rejecting credentials and symbolic links."""
    files: set[Path] = set()
    resolved_root = root.resolve()
    for path in paths:
        if not path.resolve().is_relative_to(resolved_root):
            raise ValueError("Bundle inputs must stay inside the source repository")
        if not path.exists():
            raise FileNotFoundError(path)
        for file in [path] if path.is_file() else path.rglob("*"):
            relative = file.relative_to(root)
            if any(part in _SKIP for part in relative.parts) or file.suffix in {
                ".pyc",
                ".lock",
            }:
                continue
            if file.is_symlink():
                raise ValueError(
                    f"Copy symlink targets into the workspace first: {relative}"
                )
            if file.is_file():
                if (
                    file.name.startswith(".env")
                    or file.name.endswith((".pem", ".key"))
                    or file.name in {"wallets.json", "credentials.json"}
                ):
                    raise ValueError(
                        f"Credential file must not enter a compute bundle: {relative}"
                    )
                if len(relative.parts) == 1 and file.name in _FORBIDDEN:
                    raise ValueError(
                        f"Machine configuration must not enter a compute bundle: {relative}"
                    )
                files.add(file)
    return sorted(files)


def pack_job(
    store: JobStore,
    job_id: str,
    destination: Path,
    *,
    op: str = "backtest_job",
    options: dict[str, Any] | None = None,
    extra_paths: list[str] | None = None,
    expected_sdk_commit: str | None = None,
) -> PackedJob:
    """Snapshot one job and its extra inputs without changing the source tree."""
    if op not in OPERATIONS:
        raise ValueError(f"Unsupported compute operation: {op}")
    job_id = safe_job_id(job_id)
    root = store.repo_root
    paths = [store.job_dir(job_id)]
    paths.extend(root / safe_relative(value) for value in extra_paths or [])
    files = workspace_files(root, paths)
    request: WorkspaceRequest = {
        "protocol": PROTOCOL,
        "job_id": job_id,
        "op": op,
        "options": options or {},
        "source_root": str(root),
        "source_revision": compute_workspace_revision(store.job_dir(job_id)),
        "expected_sdk_commit": expected_sdk_commit,
        "files": {file.relative_to(root).as_posix(): sha256(file) for file in files},
    }
    # Archive metadata is added without modifying the user's job or repository.
    with tempfile.TemporaryDirectory() as temporary:
        staged = SpriteWorkspace(Path(temporary))
        for file in files:
            relative = file.relative_to(root).as_posix()
            target = staged.file(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
            if sha256(target) != request["files"][relative]:
                raise ValueError(
                    "Workspace changed while packaging; retry the submission"
                )
        staged.request_file.write_text(
            json.dumps(request, allow_nan=False), encoding="utf-8"
        )
        result = staged.archive(destination)
    return {**result, "request": request}
