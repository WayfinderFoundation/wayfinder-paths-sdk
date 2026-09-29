"""Execute an SDK compute workspace or pure phase in a dedicated child process."""

from __future__ import annotations

import argparse
import functools
import importlib.metadata
import json
import re
import runpy
import subprocess
import sys
import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import chdir, contextmanager
from pathlib import Path
from typing import Any

import httpx
import yaml
from loguru import logger

from wayfinder_paths.jobs.compute_phase import resolve_phase
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import safe_job_id
from wayfinder_paths.jobs.sprite_bundle import (
    OPERATIONS,
    PHASE_OP,
    PHASE_PROTOCOL,
    PROTOCOL,
    PhaseRequest,
    SpriteWorkspace,
    WorkspaceRequest,
    extract_archive,
    sha256,
)
from wayfinder_paths.jobs.store import JobStore

MAX_SUMMARY_BYTES = 256 * 1024
OperationExecutor = Callable[[WorkspaceRequest, Path], Any]


@contextmanager
def _execution_context(
    root: Path, *, script: Path | None = None, argv: Sequence[str] = ()
) -> Iterator[None]:
    """Scope process state to one operation, including failing scripts.

    This is for a dedicated worker process, not concurrent threads.
    """
    previous_path = sys.path
    previous_argv = sys.argv
    path_entries = previous_path[:]
    arguments = previous_argv[:]
    try:
        with chdir(root):
            sys.path.insert(0, str(root))
            if script is not None:
                sys.path.insert(0, str(script.parent))
                sys.argv = [str(script), *argv]
            yield
    finally:
        previous_path[:] = path_entries
        previous_argv[:] = arguments
        sys.path = previous_path
        sys.argv = previous_argv


def _rebase(value: Any, source: str, destination: str) -> Any:
    if isinstance(value, str):
        return value.replace(source + "/", destination + "/")
    if isinstance(value, list):
        return [_rebase(item, source, destination) for item in value]
    if isinstance(value, dict):
        return {key: _rebase(item, source, destination) for key, item in value.items()}
    return value


def _contains_path(file: Path, path: str) -> bool:
    # Large JSON datasets normally contain no paths. Scan them without loading
    # another full copy into memory just to prepare the workspace.
    needle = path.encode()
    tail = b""
    with file.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            combined = tail + chunk
            if needle in combined:
                return True
            tail = combined[-len(needle) :]
    return False


GIT_TIMEOUT_SECONDS = 10
SDK_REPOSITORY = "WayfinderFoundation/wayfinder-paths-sdk"
_SDK_ROOT = Path(__file__).resolve().parents[2]
_COMMIT = re.compile(r"[0-9a-f]{40}")
_warned: set[str] = set()


def _warn_once(message: str, *args: Any) -> None:
    """Every runner resolves the commit again (a new commit counts); each warning is
    logged once per process."""
    text = message.format(*args)
    if text not in _warned:
        _warned.add(text)
        logger.warning(text)


def _git(*args: str) -> str:
    return (
        subprocess.check_output(
            ["git", "-C", str(_SDK_ROOT), *args],
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        .decode()
        .strip()
    )


def _marker_commit() -> str | None:
    """.sdk-commit, written by the Shell image and by the Sprite runtime installer."""
    marker = _SDK_ROOT / ".sdk-commit"
    commit = marker.read_text().strip() if marker.exists() else ""
    return commit if _COMMIT.fullmatch(commit) else None


def _modified_checkout() -> str:
    return _git(
        "status",
        "--porcelain",
        "--",
        "wayfinder_paths",
        "pyproject.toml",
        "poetry.lock",
    )


def installed_sdk_commit() -> str | None:
    """Checkpoint marker or an unchanged source checkout can satisfy a pin."""
    marker = _marker_commit()
    if marker is not None or not (_SDK_ROOT / ".git").exists():
        return marker
    try:
        # Include staged and untracked SDK changes: HEAD alone would falsely
        # identify a developer's modified implementation as the pinned release.
        return None if _modified_checkout() else _git("rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None


def node_sdk_commit() -> str | None:
    """The public SDK commit a lease's Sprite installs to run this node's jobs.

    In order: the image marker (``.sdk-commit``); a git checkout's HEAD, with a warning
    when the Sprite cannot match it exactly; a pip install from git (its recorded
    commit); a PyPI release (its ``v<version>`` tag, resolved on GitHub). None leaves an
    SDK runner profile to refuse the booking, so the job runs locally.
    """
    marker = _marker_commit()
    if marker is not None:
        return marker
    if (_SDK_ROOT / ".git").exists():
        return _checkout_commit()
    return _distribution_commit()


def _checkout_commit() -> str | None:
    """HEAD when a remote has it; otherwise its nearest main ancestor, which a fresh
    Sprite can download."""
    try:
        head = _git("rev-parse", "HEAD")
        modified = _modified_checkout()
        pushed = _git("branch", "--remotes", "--contains", head)
        commit = head if pushed else _main_ancestor(head)
        newer = _git("rev-list", "--count", f"{commit}..{head}") if commit else ""
    except (OSError, subprocess.SubprocessError):
        _warn_once("Could not read the SDK checkout's commit; jobs run locally")
        return None
    if commit is None:
        _warn_once(
            "SDK commit {} is not on any remote branch; push it, or its Sprite install fails",
            head[:12],
        )
        commit = head
    elif commit != head:
        _warn_once(
            "SDK commit {} is not pushed; offloaded jobs run its nearest main ancestor {}, "
            "without its {} newer commit(s)",
            head[:12],
            commit[:12],
            newer,
        )
    if modified:
        _warn_once(
            "Offloaded jobs run SDK commit {} without this checkout's uncommitted changes",
            commit[:12],
        )
    return commit


def _main_ancestor(head: str) -> str | None:
    """The nearest commit HEAD shares with the remote main branch (local refs only)."""
    for ref in ("refs/remotes/origin/HEAD", "refs/remotes/origin/main"):
        try:
            return _git("merge-base", head, ref)
        except subprocess.CalledProcessError:
            continue
    return None


def _distribution_commit() -> str | None:
    try:
        distribution = importlib.metadata.distribution("wayfinder-paths")
    except importlib.metadata.PackageNotFoundError:
        return None
    # PEP 610: a pip install from git records the commit it installed.
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    commit = (direct.get("vcs_info") or {}).get("commit_id", "")
    if _COMMIT.fullmatch(commit):
        return commit
    return _release_commit(distribution.version)


@functools.cache
def _release_commit(version: str) -> str | None:
    try:
        response = httpx.get(
            f"https://api.github.com/repos/{SDK_REPOSITORY}/commits/v{version}",
            headers={"Accept": "application/vnd.github+json"},
            timeout=10,
        )
        response.raise_for_status()
        commit = response.json()["sha"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        _warn_once(
            "Could not resolve SDK release v{} to a commit; jobs run locally", version
        )
        return None
    return commit if isinstance(commit, str) and _COMMIT.fullmatch(commit) else None


def prepare(source: Path, root: Path, base: Path | None = None) -> WorkspaceRequest:
    """Extract, verify and rebase the request into an isolated workspace."""
    workspace = SpriteWorkspace(root)
    names = extract_archive(source, workspace.root)
    request = json.loads(workspace.request_file.read_text(encoding="utf-8"))
    return _prepare_job(request, workspace, _extract_base(base, workspace, names))


def _extract_base(
    base: Path | None, workspace: SpriteWorkspace, names: frozenset[str]
) -> frozenset[str]:
    # After the job archive, so a base failure belongs to a known request; a
    # path in both is refused, so the tree equals extracting the base first.
    if base is None:
        return frozenset()
    return extract_archive(base, workspace.root, reserved=names)


def _verify_inputs(
    request: WorkspaceRequest | PhaseRequest,
    workspace: SpriteWorkspace,
    base_names: frozenset[str],
) -> None:
    expected = request.get("expected_sdk_commit")
    if expected and installed_sdk_commit() != expected:
        raise ValueError("Sprite SDK commit differs from the requested SDK commit")
    base_files = request.get("base_files") or {}
    if base_files.keys() & request["files"].keys():
        raise ValueError("A path is listed in both the base and the job request")
    if set(base_files) != base_names:
        raise ValueError("Base archive does not match the request's base files")
    for name, digest in {**request["files"], **base_files}.items():
        file = workspace.file(name)
        if sha256(file) != digest:
            raise ValueError(f"Workspace checksum mismatch: {name}")


def _prepare_phase(
    request: PhaseRequest, workspace: SpriteWorkspace, base_names: frozenset[str]
) -> PhaseRequest:
    if request.get("op") != PHASE_OP or not isinstance(request.get("args"), dict):
        raise ValueError("Unsupported compute phase request")
    _verify_inputs(request, workspace, base_names)
    workspace.outputs_dir.mkdir()
    # A JobStore() inside the phase must discover this copy, never a source
    # repository above a local runner's run directory.
    marker = workspace.file("pyproject.toml")
    if not marker.exists():
        marker.write_text(
            '[project]\nname = "sprite-phase"\nversion = "0.0.0"\n', encoding="utf-8"
        )
    return request


def _prepare_job(
    request: WorkspaceRequest, workspace: SpriteWorkspace, base_names: frozenset[str]
) -> WorkspaceRequest:
    if request.get("protocol") != PROTOCOL or request.get("op") not in OPERATIONS:
        raise ValueError("Unsupported SDK workspace protocol or operation")
    source_root = request["source_root"]
    if (
        not isinstance(source_root, str)
        or not Path(source_root).is_absolute()
        or source_root == "/"
    ):
        raise ValueError("Invalid source repository root")
    _verify_inputs(request, workspace, base_names)
    # JobStore discovers this isolated root rather than the SDK's installation.
    workspace.file("pyproject.toml").write_text(
        '[project]\nname = "sprite-workspace"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    _rebase_workspace(request, workspace)
    # Lets apply_job_outputs map rebased paths and stamps back to the source.
    job_root = workspace.file(f".wayfinder/jobs/{safe_job_id(request['job_id'])}")
    workspace.runtime_file.write_text(
        json.dumps(
            {
                "workspace_root": str(workspace.root),
                "workspace_revision": compute_workspace_revision(job_root),
            }
        ),
        encoding="utf-8",
    )
    return request


def _rebase_workspace(request: WorkspaceRequest, workspace: SpriteWorkspace) -> None:
    source_root = request["source_root"]
    for name in request["files"]:
        file = workspace.file(name)
        if file.suffix in {".json", ".yaml", ".yml"}:
            if not _contains_path(file, source_root + "/"):
                continue
            text = file.read_text(encoding="utf-8")
            value = json.loads(text) if file.suffix == ".json" else yaml.safe_load(text)
            value = _rebase(value, source_root, str(workspace.root))
            file.write_text(
                json.dumps(value)
                if file.suffix == ".json"
                else yaml.safe_dump(value, sort_keys=False),
                encoding="utf-8",
            )
    request["options"] = _rebase(request["options"], source_root, str(workspace.root))


def _run_script(options: dict[str, Any], workspace: SpriteWorkspace) -> Any:
    script = workspace.file(options["path"])
    with _execution_context(
        workspace.root, script=script, argv=options.get("argv", [])
    ):
        try:
            runpy.run_path(str(script), run_name="__main__")
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise RuntimeError(
                    f"Backtest script exited with status {exc.code}"
                ) from exc
    result = workspace.file("result.json")
    return (
        json.loads(result.read_text(encoding="utf-8"))
        if result.exists()
        else {"script": options["path"], "finished": True}
    )


def execute_operation(request: WorkspaceRequest, root: Path) -> Any:
    """Dispatch a portable request through the existing SDK operation handlers."""
    op = request["op"]
    options = dict(request["options"])
    job_id = safe_job_id(request["job_id"])
    if any(key in options for key in ("store", "job_id")):
        raise ValueError("Operation options cannot replace the job or store")
    if op == "script":
        return _run_script(options, SpriteWorkspace(root))
    if op == "preflight":
        from wayfinder_paths.jobs.execution.preflight import run_preflight

        return run_preflight(job_id, store=JobStore(repo_root=root), **options)
    if op == "validate_job":
        from wayfinder_paths.jobs.execution.validation import validate_execution_job

        return validate_execution_job(job_id, store=JobStore(repo_root=root), **options)
    from wayfinder_paths.jobs.execution.op_runner import _run

    # Keep the complete result in the archive even when the agent sees a summary.
    if op in {"backtest_job", "experiments"}:
        options["full"] = True
    return _run(op, {"job_id": job_id, **options})


def _summarize(op: str, payload: Any, result_file: str) -> Any:
    summary = payload
    if op == "backtest_job":
        from wayfinder_paths.jobs.execution.job import summarize_backtest_payload

        summary = summarize_backtest_payload(payload)
    encoded = json.dumps(summary, default=str)
    if len(encoded.encode("utf-8")) > MAX_SUMMARY_BYTES:
        return {"full_result": result_file}
    # The HTTP summary must satisfy the backend's finite-JSON contract.
    return json.loads(encoded, parse_constant=lambda _: None)


def run(
    source: Path,
    root: Path,
    output: Path,
    artifacts: Path,
    *,
    executor: OperationExecutor | None = None,
    base: Path | None = None,
) -> int:
    """Run one operation or phase and preserve its artifacts on success or failure.

    ``base`` is an optional archive of inputs shared across a lease's jobs,
    extracted into the same workspace; the base never returns with a phase.
    """
    workspace = SpriteWorkspace(root)
    result: dict[str, Any] = {}
    code = 1
    phase = False
    stage = "prepare"
    try:
        names = extract_archive(source, workspace.root)
        request = json.loads(workspace.request_file.read_text(encoding="utf-8"))
        phase = request.get("protocol") == PHASE_PROTOCOL
        base_names = _extract_base(base, workspace, names)
        if phase:
            request = _prepare_phase(request, workspace, base_names)
            function = resolve_phase(request["phase"])
            stage = "execute"
            with _execution_context(workspace.root):
                payload = function(
                    workspace.root, workspace.outputs_dir, dict(request["args"])
                )
            result_file = workspace.phase_result_file
        else:
            request = _prepare_job(request, workspace, base_names)
            execute = executor if executor is not None else execute_operation
            with _execution_context(workspace.root):
                payload = execute(request, workspace.root)
            result_file = workspace.result_file
        result_file.write_text(json.dumps(payload, default=str), encoding="utf-8")
        full_result = result_file.relative_to(workspace.root).as_posix()
        result = {
            "operation": request["op"],
            **(
                {"phase": request["phase"]}
                if phase
                else {
                    "job_id": request["job_id"],
                    "source_revision": request["source_revision"],
                }
            ),
            "summary": _summarize(request["op"], payload, full_result),
            "full_result": full_result,
        }
        code = 0
    except Exception as exc:
        traceback.print_exc()
        result = {"error": str(exc), "error_type": type(exc).__name__}
        if phase:
            # A phase's own exception can be evidence; a runtime that could
            # not start it is infrastructure.
            result["stage"] = stage
        error_file = workspace.phase_error_file if phase else workspace.error_file
        error_file.parent.mkdir(parents=True, exist_ok=True)
        error_file.write_text(json.dumps(result), encoding="utf-8")
    # A job uploads its whole isolated workspace, including model binaries,
    # charts, traces, folds, ledgers, source inputs and partial diagnostics on
    # failure. A phase returns only outputs/: its inputs never travel back.
    workspace.collect(artifacts)
    output.write_text(
        json.dumps(result, default=str, allow_nan=False), encoding="utf-8"
    )
    return code


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument(
        "--base", type=Path, help="Archive of inputs shared across a lease's jobs"
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args(argv)
    if args.collect_only:
        SpriteWorkspace(args.root).collect(args.artifacts.resolve())
        return
    raise SystemExit(
        run(
            args.bundle.resolve(),
            args.root.resolve(),
            args.output.resolve(),
            args.artifacts.resolve(),
            base=args.base.resolve() if args.base else None,
        )
    )


if __name__ == "__main__":
    main()
