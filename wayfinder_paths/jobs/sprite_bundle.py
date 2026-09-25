"""Portable job workspaces for remote computation; no machine credentials."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict

import yaml

from wayfinder_paths.jobs.compute_lock import job_state_lock
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import safe_job_id
from wayfinder_paths.jobs.store import JobStore

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 20000
PROTOCOL = "wayfinder-sprite-job-v1"
# A registered pure function over explicit inputs; see compute_phase.py.
PHASE_PROTOCOL = "wayfinder-sprite-phase-v1"
PHASE_OP = "evolution_phase"
OUTPUTS_DIR = "outputs"
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
# The strategy definition (what compute_workspace_revision hashes) and its
# version history are inputs to a run, never outputs applied back to the job.
_STRATEGY_PATHS = {"job.yaml", "workspace", "versions"}
_TEXT_SUFFIXES = {".json", ".jsonl", ".yaml", ".yml"}


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


class PhaseCall(TypedDict):
    phase: str
    args: dict[str, Any]


class PhaseRequest(TypedDict):
    protocol: str
    op: str
    phase: str
    args: dict[str, Any]
    expected_sdk_commit: str | None
    files: dict[str, str]


class PackedInputs(ArchiveInfo):
    request: PhaseRequest


class AppliedOutputs(TypedDict):
    updated: list[str]
    appended: list[str]
    skipped: list[str]


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

    @property
    def runtime_file(self) -> Path:
        return self.root / "sprite-runtime.json"

    @property
    def outputs_dir(self) -> Path:
        return self.root / OUTPUTS_DIR

    @property
    def phase_result_file(self) -> Path:
        return self.outputs_dir / "phase-result.json"

    @property
    def phase_error_file(self) -> Path:
        return self.outputs_dir / "phase-error.json"

    def archive(self, destination: Path) -> ArchiveInfo:
        return write_archive(
            self.root, workspace_files(self.root, [self.root]), destination
        )

    def collect(self, destination: Path) -> ArchiveInfo:
        """Archive what a run returns: the whole job workspace, or only a
        phase's outputs directory so its inputs never travel back."""
        try:
            protocol = json.loads(self.request_file.read_text(encoding="utf-8"))[
                "protocol"
            ]
        except (OSError, ValueError, KeyError, TypeError):
            protocol = None
        if protocol != PHASE_PROTOCOL:
            return self.archive(destination)
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        return write_archive(
            self.root, workspace_files(self.root, [self.outputs_dir]), destination
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
    return {**_stage(root, files, request, destination), "request": request}


def pack_inputs(
    root: Path,
    paths: Sequence[str],
    request: PhaseCall,
    destination: Path,
    *,
    expected_sdk_commit: str | None = None,
) -> PackedInputs:
    """Snapshot only the named inputs for one registered compute phase."""
    relative = [safe_relative(value) for value in paths]
    if any(path.parts[0] == OUTPUTS_DIR for path in relative):
        raise ValueError("Phase inputs cannot come from the outputs directory")
    files = workspace_files(root, [root / path for path in relative])
    phase_request: PhaseRequest = {
        "protocol": PHASE_PROTOCOL,
        "op": PHASE_OP,
        "phase": request["phase"],
        "args": request["args"],
        "expected_sdk_commit": expected_sdk_commit,
        "files": {file.relative_to(root).as_posix(): sha256(file) for file in files},
    }
    return {**_stage(root, files, phase_request, destination), "request": phase_request}


def _stage(
    root: Path, files: list[Path], request: Mapping[str, Any], destination: Path
) -> ArchiveInfo:
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
        return staged.archive(destination)


def apply_job_outputs(store: JobStore, artifacts: Path) -> AppliedOutputs:
    """Three-way apply against the packed checksums: a file the job changed
    during the run keeps the job's version, except append-only ``.jsonl``
    ledgers, which receive only the run's new rows."""
    workspace = SpriteWorkspace(artifacts)
    request: WorkspaceRequest = json.loads(
        workspace.request_file.read_text(encoding="utf-8")
    )
    if request.get("protocol") != PROTOCOL or request.get("source_root") != str(
        store.repo_root
    ):
        raise ValueError("Artifacts were not packed from this repository")
    job_id = safe_job_id(request["job_id"])
    prefix = f".wayfinder/jobs/{job_id}"
    copied, source = workspace.file(prefix), store.job_dir(job_id)
    restorations = _restorations(workspace, request)
    report: AppliedOutputs = {"updated": [], "appended": [], "skipped": []}
    with job_state_lock(store.repo_root, job_id, name="runner_outputs"):
        for file in sorted(copied.rglob("*")):
            relative = file.relative_to(copied)
            if (
                file.is_symlink()
                or not file.is_file()
                or relative.parts[0] in _STRATEGY_PATHS
            ):
                continue
            name = f"{prefix}/{relative.as_posix()}"
            base = request["files"].get(name)
            if base == sha256(file):
                continue
            produced = file.read_bytes()
            target = source / relative
            current = target.read_bytes() if target.exists() else None
            if file.suffix == ".jsonl":
                start = _prefix_length(produced, base)
                job_still_extends_base = (
                    base is None
                    if current is None
                    else _prefix_length(current, base) is not None
                )
                if start is not None and job_still_extends_base:
                    rows = _restore(produced[start:], restorations)
                    if current and not current.endswith(b"\n"):
                        rows = b"\n" + rows
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("ab") as stream:
                        stream.write(rows)
                    report["appended"].append(name)
                    continue
            if file.suffix in _TEXT_SUFFIXES:
                produced = _restore(produced, restorations)
            if current is not None and _equivalent(current, produced, file.suffix):
                continue
            unchanged_since_packing = (
                base is None
                if current is None
                else hashlib.sha256(current).hexdigest() == base
            )
            if not unchanged_since_packing:
                report["skipped"].append(name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=f".{target.name}.", delete=False
            ) as staged:
                staged.write(produced)
            os.replace(staged.name, target)
            report["updated"].append(name)
    return report


def _restorations(
    workspace: SpriteWorkspace, request: WorkspaceRequest
) -> list[tuple[bytes, bytes]]:
    try:
        runtime = json.loads(workspace.runtime_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []  # Runtimes before sprite-runtime.json did not record them.
    pairs: list[tuple[bytes, bytes]] = []
    root, revision = runtime.get("workspace_root"), runtime.get("workspace_revision")
    if (
        isinstance(root, str)
        and Path(root).is_absolute()
        and root not in {"/", request["source_root"]}
    ):
        pairs.append(((root + "/").encode(), (request["source_root"] + "/").encode()))
    # Rebasing absolute paths in job.yaml changes the copy's revision hash;
    # stamps must name the revision the job was packed at. Only the quoted
    # JSON value is mapped: a short hex id could also occur inside other hashes.
    if (
        isinstance(revision, str)
        and re.fullmatch(r"[0-9a-f]{12,}", revision)
        and revision != request["source_revision"]
    ):
        pairs.append(
            (f'"{revision}"'.encode(), f'"{request["source_revision"]}"'.encode())
        )
    return pairs


def _restore(data: bytes, restorations: list[tuple[bytes, bytes]]) -> bytes:
    for old, new in restorations:
        data = data.replace(old, new)
    return data


def _prefix_length(data: bytes, digest: str | None) -> int | None:
    # Line-aligned, so an append-only ledger's packed rows are found exactly.
    hasher = hashlib.sha256()
    if digest is None or hasher.hexdigest() == digest:
        return 0
    start = 0
    while (end := data.find(b"\n", start)) != -1:
        hasher.update(data[start : end + 1])
        start = end + 1
        if hasher.hexdigest() == digest:
            return start
    hasher.update(data[start:])
    return len(data) if hasher.hexdigest() == digest else None


def _equivalent(current: bytes, produced: bytes, suffix: str) -> bool:
    # The runtime re-serializes JSON/YAML inputs it rebases; same content is
    # not an output.
    if current == produced:
        return True
    try:
        if suffix == ".json":
            return json.loads(current) == json.loads(produced)
        if suffix in {".yaml", ".yml"}:
            return yaml.safe_load(current) == yaml.safe_load(produced)
    except (ValueError, yaml.YAMLError):
        pass
    return False
