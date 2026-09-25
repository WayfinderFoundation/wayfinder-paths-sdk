"""Execute an SDK compute workspace or pure phase in a dedicated child process."""

from __future__ import annotations

import argparse
import json
import runpy
import sys
import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import chdir, contextmanager
from pathlib import Path
from typing import Any

import yaml

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


def installed_sdk_commit() -> str | None:
    """Checkpoint marker or an unchanged source checkout can satisfy a pin."""
    import subprocess

    sdk_root = Path(__file__).resolve().parents[2]
    marker = sdk_root / ".sdk-commit"
    if marker.exists():
        return marker.read_text().strip()
    if not (sdk_root / ".git").exists():
        return None
    try:
        # Include staged and untracked SDK changes: HEAD alone would falsely
        # identify a developer's modified implementation as the pinned release.
        dirty = subprocess.check_output(
            [
                "git",
                "-C",
                str(sdk_root),
                "status",
                "--porcelain",
                "--",
                "wayfinder_paths",
                "pyproject.toml",
                "poetry.lock",
            ],
            stderr=subprocess.DEVNULL,
        )
        if dirty.strip():
            return None
        return (
            subprocess.check_output(
                ["git", "-C", str(sdk_root), "rev-parse", "HEAD"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return None


def prepare(source: Path, root: Path) -> WorkspaceRequest:
    """Extract, verify and rebase the request into an isolated workspace."""
    workspace = SpriteWorkspace(root)
    extract_archive(source, workspace.root)
    return _prepare_job(
        json.loads(workspace.request_file.read_text(encoding="utf-8")), workspace
    )


def _verify_inputs(
    request: WorkspaceRequest | PhaseRequest, workspace: SpriteWorkspace
) -> None:
    expected = request.get("expected_sdk_commit")
    if expected and installed_sdk_commit() != expected:
        raise ValueError("Sprite SDK commit differs from the requested SDK commit")
    for name, digest in request["files"].items():
        file = workspace.file(name)
        if sha256(file) != digest:
            raise ValueError(f"Workspace checksum mismatch: {name}")


def _prepare_phase(request: PhaseRequest, workspace: SpriteWorkspace) -> PhaseRequest:
    if request.get("op") != PHASE_OP or not isinstance(request.get("args"), dict):
        raise ValueError("Unsupported compute phase request")
    _verify_inputs(request, workspace)
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
    request: WorkspaceRequest, workspace: SpriteWorkspace
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
    _verify_inputs(request, workspace)
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
) -> int:
    """Run one operation or phase and preserve its artifacts on success or failure."""
    workspace = SpriteWorkspace(root)
    result: dict[str, Any] = {}
    code = 1
    phase = False
    stage = "prepare"
    try:
        extract_archive(source, workspace.root)
        request = json.loads(workspace.request_file.read_text(encoding="utf-8"))
        phase = request.get("protocol") == PHASE_PROTOCOL
        if phase:
            request = _prepare_phase(request, workspace)
            function = resolve_phase(request["phase"])
            stage = "execute"
            with _execution_context(workspace.root):
                payload = function(
                    workspace.root, workspace.outputs_dir, dict(request["args"])
                )
            result_file = workspace.phase_result_file
        else:
            request = _prepare_job(request, workspace)
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
        )
    )


if __name__ == "__main__":
    main()
