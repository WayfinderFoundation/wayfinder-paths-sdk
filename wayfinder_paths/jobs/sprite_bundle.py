"""Portable job workspaces for remote computation; no machine credentials."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
from collections.abc import Collection, Mapping, Sequence
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
# sprite-leases holds this node's lease tokens; they never enter a bundle.
_SKIP = {
    "__pycache__",
    ".git",
    ".venv",
    "background_ops",
    "running_ops",
    "sprite-leases",
}
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
    base_files: dict[str, str]


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
    base_files: dict[str, str]


class PackedInputs(ArchiveInfo):
    request: PhaseRequest


class PackedBase(ArchiveInfo):
    checksums: dict[str, str]


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
    # A fixed gzip timestamp keeps an unchanged base byte-identical when it is
    # packed again, so a lease reuses the base it already holds.
    with (
        destination.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", dereference=False) as archive,
    ):
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


def extract_archive(
    source: Path, destination: Path, *, reserved: Collection[str] = ()
) -> frozenset[str]:
    """Validate the entire archive before writing; never allow links/devices.

    Returns the extracted names. A name in ``reserved`` (the other archive of
    the same workspace) is refused before anything is written.
    """
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
            if relative.as_posix() in reserved:
                raise ValueError(f"A path is present in both archives: {member.name}")
            if not (root / relative).resolve().is_relative_to(root):
                raise ValueError("Archive escapes destination")
            names.add(relative)
            total += member.size
            if len(names) > MAX_FILES or total > MAX_EXPANDED_BYTES:
                raise ValueError("Archive expansion limit exceeded")
            members.append(member)
        archive.extractall(root, members=members, filter="data")
    return frozenset(name.as_posix() for name in names)


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
        "files": _checksums(root, files),
        "base_files": {},
    }
    return {
        **_stage(root, files, request["files"], destination, request=request),
        "request": request,
    }


def pack_base(root: Path, paths: Sequence[str], destination: Path) -> PackedBase:
    """Snapshot inputs shared by every phase of a lease, such as a dataset.

    The archive is reproducible: packing unchanged files again yields the same
    bytes, so the lease that already holds this base is reused.
    """
    files = _phase_files(root, paths)
    checksums = _checksums(root, files)
    return {**_stage(root, files, checksums, destination), "checksums": checksums}


def pack_inputs(
    root: Path,
    paths: Sequence[str],
    request: PhaseCall,
    destination: Path,
    *,
    expected_sdk_commit: str | None = None,
    base: PackedBase | None = None,
) -> PackedInputs:
    """Snapshot only the named inputs for one registered compute phase.

    With a ``base`` (see ``pack_base``) these inputs are the delta extracted
    over it; a file cannot be in both.
    """
    files = _phase_files(root, paths)
    checksums = _checksums(root, files)
    base_files = base["checksums"] if base is not None else {}
    overlap = sorted(checksums.keys() & base_files.keys())
    if overlap:
        raise ValueError(f"Phase input is already in the base: {overlap[0]}")
    phase_request: PhaseRequest = {
        "protocol": PHASE_PROTOCOL,
        "op": PHASE_OP,
        "phase": request["phase"],
        "args": request["args"],
        "expected_sdk_commit": expected_sdk_commit,
        "files": checksums,
        "base_files": dict(base_files),
    }
    return {
        **_stage(root, files, checksums, destination, request=phase_request),
        "request": phase_request,
    }


def _phase_files(root: Path, paths: Sequence[str]) -> list[Path]:
    relative = [safe_relative(value) for value in paths]
    if any(path.parts[0] == OUTPUTS_DIR for path in relative):
        raise ValueError("Phase inputs cannot come from the outputs directory")
    return workspace_files(root, [root / path for path in relative])


def _checksums(root: Path, files: list[Path]) -> dict[str, str]:
    return {file.relative_to(root).as_posix(): sha256(file) for file in files}


def _stage(
    root: Path,
    files: list[Path],
    checksums: Mapping[str, str],
    destination: Path,
    *,
    request: Mapping[str, Any] | None = None,
) -> ArchiveInfo:
    # Archive metadata is added without modifying the user's job or repository.
    with tempfile.TemporaryDirectory() as temporary:
        staged = SpriteWorkspace(Path(temporary))
        for file in files:
            relative = file.relative_to(root).as_posix()
            target = staged.file(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
            # The source's modification time, not the copy's, keeps repeated
            # packing of unchanged files byte-identical.
            modified = file.stat().st_mtime_ns
            os.utime(target, ns=(modified, modified))
            if sha256(target) != checksums[relative]:
                raise ValueError(
                    "Workspace changed while packaging; retry the submission"
                )
        if request is not None:
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


def restore_workspace_paths(value: Any, artifacts: Path) -> Any:
    """A run's JSON report with the runner workspace's paths and revision mapped
    back to the source repository's, as apply_job_outputs maps the files it
    writes, so its paths name the job rather than the runner's copy."""
    workspace = SpriteWorkspace(artifacts)
    request: WorkspaceRequest = json.loads(
        workspace.request_file.read_text(encoding="utf-8")
    )
    root, revision = _workspace_identity(workspace, request)
    source_root = request["source_root"]

    def restore(item: Any) -> Any:
        if isinstance(item, str):
            if item == revision:
                return request["source_revision"]
            if root is None:
                return item
            if item == root:
                return source_root
            return item.replace(root + "/", source_root + "/")
        if isinstance(item, list):
            return [restore(entry) for entry in item]
        if isinstance(item, dict):
            return {restore(key): restore(entry) for key, entry in item.items()}
        return item

    return restore(value)


def _workspace_identity(
    workspace: SpriteWorkspace, request: WorkspaceRequest
) -> tuple[str | None, str | None]:
    """The run's workspace root and revision, each None where it is the source's."""
    try:
        runtime = json.loads(workspace.runtime_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None  # Runtimes before sprite-runtime.json did not record them.
    root, revision = runtime.get("workspace_root"), runtime.get("workspace_revision")
    if not (
        isinstance(root, str)
        and Path(root).is_absolute()
        and root not in {"/", request["source_root"]}
    ):
        root = None
    if not (
        isinstance(revision, str)
        and re.fullmatch(r"[0-9a-f]{12,}", revision)
        and revision != request["source_revision"]
    ):
        revision = None
    return root, revision


def _restorations(
    workspace: SpriteWorkspace, request: WorkspaceRequest
) -> list[tuple[bytes, bytes]]:
    root, revision = _workspace_identity(workspace, request)
    pairs: list[tuple[bytes, bytes]] = []
    if root is not None:
        pairs.append(((root + "/").encode(), (request["source_root"] + "/").encode()))
    # Rebasing absolute paths in job.yaml changes the copy's revision hash;
    # stamps must name the revision the job was packed at. Only the quoted
    # JSON value is mapped: a short hex id could also occur inside other hashes.
    if revision is not None:
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
