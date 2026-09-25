"""Configured compute backends with one workspace and run lifecycle contract."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import FrameType
from typing import Any, Self, TypedDict
from urllib.parse import urlsplit

import httpx

from wayfinder_paths.jobs.compute_phase import phase_name, resolve_phase
from wayfinder_paths.jobs.execution.op_process import (
    process_identity_fields,
    recorded_process_alive,
)
from wayfinder_paths.jobs.sprite_bundle import (
    OPERATIONS,
    SpriteWorkspace,
    apply_job_outputs,
    extract_archive,
    pack_inputs,
    pack_job,
    safe_relative,
    sha256,
)
from wayfinder_paths.jobs.sprite_client import TERMINAL_STATUSES, SpriteBacktestsClient
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.monitor_state import atomic_write_json

# A remote provider refusing a worker: no subscription or credit (402/403),
# active/pool limits (409), daily cap (429), or disabled/unavailable (503).
CAPACITY_STATUSES = frozenset({402, 403, 409, 429, 503})
FALLBACKS = frozenset({"local", "none"})
STATUS_ATTEMPTS = 5


class ComputeUnavailable(RuntimeError):
    """The provider cannot take this run now; nothing was started remotely."""


class PhaseFailed(RuntimeError):
    """A phase run ended without a result.

    ``stage`` is ``"execute"`` when the phase function itself raised
    ``error_type``; ``"prepare"`` when the runtime could not start it; and
    ``None`` when the run ended (timeout, cancellation, lost worker) without a
    phase record. ``outputs_path`` holds partial outputs when collected.
    """

    def __init__(
        self,
        message: str,
        *,
        run: dict[str, Any],
        error: str,
        error_type: str | None,
        stage: str | None,
        outputs_path: str | None,
    ):
        super().__init__(message)
        self.run, self.error, self.error_type = run, error, error_type
        self.stage, self.outputs_path = stage, outputs_path


class PhaseOutcome(TypedDict):
    result: Any
    outputs_path: str
    run: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    provider: str
    runs_dir: Path
    configured: bool = False
    # "local" runs locally when a remote provider refuses capacity.
    fallback: str = "local"
    # None matches in-place execution, which has no wall-clock limit.
    timeout_seconds: int | None = None
    retain_runs: int = 10
    extra_paths: tuple[str, ...] = ()
    sdk_commit: str | None = None
    backend: str = ""
    app_name: str = ""
    preset: str = "jobs-v1"
    api_key: str = field(default="", repr=False)


def load_runner_config(
    *,
    repo_root: Path | None = None,
    config: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> RunnerConfig:
    """Environment overrides SDK config; an absent section defaults to local."""
    env = os.environ if environ is None else environ
    root = (repo_root or JobStore().repo_root).resolve()
    if config is None:
        # Respect the normal SDK config-file override. Parse strictly here: a
        # broken remote configuration must never silently execute locally.
        override = env.get("WAYFINDER_CONFIG_PATH") or env.get("WAYFINDER_CONFIG")
        path = Path(override).expanduser() if override else root / "config.json"
        if not path.is_absolute():
            path = root / path
        config = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(config, Mapping):
        raise ValueError("SDK configuration must be an object")
    section = config.get("backtest_runner", {})
    if not isinstance(section, Mapping):
        raise ValueError("backtest_runner must be an object")
    allowed = {
        "provider",
        "fallback",
        "runs_dir",
        "timeout_seconds",
        "retain_runs",
        "extra_paths",
        "sdk_commit",
        "sprites",
    }
    if set(section) - allowed:
        raise ValueError("Unknown backtest_runner configuration field")
    sprites = section.get("sprites", {})
    if not isinstance(sprites, Mapping) or set(sprites) - {
        "backend",
        "app_name",
        "preset",
    }:
        raise ValueError("Invalid backtest_runner.sprites configuration")

    def string(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a nonempty string")
        return value.strip()

    def integer(value: Any, label: str, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"{label} must be an integer")
        try:
            number = int(value)
        except ValueError as exc:
            raise ValueError(f"{label} must be an integer") from exc
        if not 1 <= number <= maximum:
            raise ValueError(f"{label} must be between 1 and {maximum}")
        return number

    provider = string(
        env.get("WAYFINDER_BACKTEST_RUNNER", section.get("provider", "local")),
        "provider",
    )
    if provider not in RUNNERS:
        raise ValueError(
            f"backtest_runner.provider must be one of: {', '.join(sorted(RUNNERS))}"
        )
    fallback = string(
        env.get("WAYFINDER_BACKTEST_FALLBACK", section.get("fallback", "local")),
        "fallback",
    )
    if fallback not in FALLBACKS:
        raise ValueError("backtest_runner.fallback must be local or none")
    directory = Path(
        string(
            env.get(
                "WAYFINDER_BACKTEST_RUNS_DIR",
                section.get("runs_dir", ".wayfinder/backtest_runs"),
            ),
            "runs_dir",
        )
    ).expanduser()
    directory = directory if directory.is_absolute() else root / directory
    timeout = env.get(
        "WAYFINDER_BACKTEST_TIMEOUT_SECONDS", section.get("timeout_seconds")
    )
    if timeout is not None:
        timeout = integer(timeout, "timeout_seconds", 21600)
    retain = integer(
        env.get("WAYFINDER_BACKTEST_RETAIN_RUNS", section.get("retain_runs", 10)),
        "retain_runs",
        10000,
    )
    extra = section.get("extra_paths", [])
    if not isinstance(extra, list) or not all(isinstance(item, str) for item in extra):
        raise ValueError("extra_paths must be a list of repository-relative paths")
    commit = env.get("WAYFINDER_BACKTEST_SDK_COMMIT", section.get("sdk_commit"))
    if commit is not None:
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("sdk_commit must be a full lowercase Git SHA")
    backend = env.get("WAYFINDER_SPRITES_BACKEND", sprites.get("backend", ""))
    app = env.get("WAYFINDER_SPRITES_APP_NAME", sprites.get("app_name", ""))
    preset = env.get("WAYFINDER_SPRITES_PRESET", sprites.get("preset", "jobs-v1"))
    system = config.get("system", {})
    key = env.get("WAYFINDER_API_KEY") or (
        system.get("api_key", "") if isinstance(system, Mapping) else ""
    )
    if provider == "sprites":
        backend = string(backend, "sprites.backend").rstrip("/")
        url = urlsplit(backend)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
            or url.scheme == "http"
            and url.hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError(
                "sprites.backend must be an HTTPS origin (HTTP allowed on loopback)"
            )
        app, preset, key = (
            string(app, "sprites.app_name"),
            string(preset, "sprites.preset"),
            string(key, "WAYFINDER_API_KEY or system.api_key"),
        )
        if not re.fullmatch(r"[A-Za-z0-9_-]+", app):
            raise ValueError("sprites.app_name must be a single path segment")
    return RunnerConfig(
        provider=provider,
        runs_dir=directory.resolve(),
        configured="backtest_runner" in config or "WAYFINDER_BACKTEST_RUNNER" in env,
        fallback=fallback,
        timeout_seconds=timeout,
        retain_runs=retain,
        extra_paths=tuple(extra),
        sdk_commit=commit,
        backend=backend,
        app_name=app,
        preset=preset,
        api_key=key,
    )


class BacktestRunner(ABC):
    """One operation per run; collection always targets a fresh directory."""

    def __init__(self, config: RunnerConfig, *, owner_pid: int | None = None):
        # A local run submitted with an owner cancels itself when that process
        # exits, however it was killed. Remote runs cannot observe the owner.
        self.config = config
        self.owner_pid = owner_pid

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        """Release client resources, without cancelling submitted computations."""
        return None

    def submit(
        self,
        store: JobStore,
        job_id: str,
        *,
        op: str = "backtest_job",
        options: dict[str, Any] | None = None,
        extra_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        if self.config.runs_dir.is_relative_to(store.job_dir(job_id).resolve()):
            raise ValueError("runs_dir must be outside the job being packaged")
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "workspace.tar.gz"
            pack_job(
                store,
                job_id,
                archive,
                op=op,
                options=options,
                extra_paths=[*self.config.extra_paths, *(extra_paths or [])],
                expected_sdk_commit=self.config.sdk_commit,
            )
            return self.submit_archive(archive)

    @abstractmethod
    def submit_archive(self, archive: Path) -> dict[str, Any]:
        """Start a prebuilt job workspace or phase archive; the caller keeps it.

        Raise ComputeUnavailable when the provider refuses capacity before
        anything started, so a configured fallback can run it elsewhere.
        """

    @abstractmethod
    def status(self, run_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def cancel(self, run_id: str) -> None: ...

    @abstractmethod
    def collect(self, run_id: str, destination: Path) -> dict[str, Any]: ...

    def wait(self, run_id: str, *, poll_interval: float = 1.0) -> dict[str, Any]:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        while True:
            status = self.status(run_id)
            if status["status"] in TERMINAL_STATUSES:
                return status
            time.sleep(poll_interval)


RUNNERS: dict[str, type[BacktestRunner]] = {}


def register_runner[R: type[BacktestRunner]](name: str) -> Callable[[R], R]:
    """Make a provider selectable as ``backtest_runner.provider``."""

    def register(runner: R) -> R:
        RUNNERS[name] = runner
        return runner

    return register


@register_runner("sprites")
class SpritesRunner(BacktestRunner):
    def __init__(
        self,
        config: RunnerConfig,
        *,
        client: SpriteBacktestsClient | None = None,
        owner_pid: int | None = None,
    ):
        super().__init__(config, owner_pid=owner_pid)
        self.client = client or SpriteBacktestsClient(
            config.backend, config.app_name, config.api_key
        )
        self._owns_client = client is None

    def submit_archive(self, archive: Path) -> dict[str, Any]:
        try:
            submitted = self.client.submit_archive(
                archive,
                preset=self.config.preset,
                expected_sdk_commit=self.config.sdk_commit,
            )
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            # Only a refused or unreachable lease request means no worker exists;
            # a failure after leasing already cancelled that lease and re-raises.
            if exc.request.method != "POST" or (
                exc.request.url.path != self.client.routes.instance
            ):
                raise
            if isinstance(exc, httpx.TransportError):
                raise ComputeUnavailable(f"Sprites backend unreachable: {exc}") from exc
            if exc.response.status_code not in CAPACITY_STATUSES:
                raise
            raise ComputeUnavailable(
                f"Sprites refused a worker (HTTP {exc.response.status_code}): "
                f"{_detail(exc.response)}"
            ) from exc
        return {**submitted, "provider": "sprites"}

    def status(self, run_id: str) -> dict[str, Any]:
        # A long remote run outlives brief network or backend interruptions.
        for attempt in range(STATUS_ATTEMPTS):
            try:
                return {**self.client.status(run_id), "provider": "sprites"}
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                client_error = (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code < 500
                )
                if client_error or attempt == STATUS_ATTEMPTS - 1:
                    raise
                time.sleep(attempt + 1)
        raise AssertionError("Status retry loop must return or raise")

    def cancel(self, run_id: str) -> None:
        self.client.cancel(run_id)

    def collect(self, run_id: str, destination: Path) -> dict[str, Any]:
        return {**self.client.collect(run_id, destination), "provider": "sprites"}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


@register_runner("local")
class LocalRunner(BacktestRunner):
    def _directory(self, run_id: str) -> Path:
        # Local IDs are UUIDs, never user-supplied filesystem paths.
        return self.config.runs_dir / str(uuid.UUID(run_id))

    def owns(self, run_id: str) -> bool:
        try:
            return self._directory(run_id).is_dir()
        except ValueError:
            return False

    def _prune_runs(self) -> None:
        if not self.config.runs_dir.exists():
            return
        finished: list[tuple[float, Path]] = []
        for directory in self.config.runs_dir.iterdir():
            try:
                record = self.status(directory.name)
            except (OSError, ValueError):
                continue  # receipts/, or a run another process just pruned
            if record["status"] in TERMINAL_STATUSES:
                finished.append(
                    ((directory / "status.json").stat().st_mtime, directory)
                )
        _prune(finished, self.config.retain_runs)

    def submit_archive(
        self, archive: Path, *, extra: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        self._prune_runs()
        run_id = str(uuid.uuid4())
        directory = self._directory(run_id)
        directory.mkdir(parents=True, mode=0o700)
        try:
            shutil.copyfile(archive, directory / "workspace.tar.gz")
            record: dict[str, Any] = {
                "id": run_id,
                "provider": "local",
                "status": "queued",
                "timeout_seconds": self.config.timeout_seconds,
                "result": {},
                "artifacts": {},
                "error": "",
                "runtime": {
                    "capabilities": ["sdk-workspace-v1"],
                    "python": sys.executable,
                },
                **(extra or {}),
            }
            if self.owner_pid is not None:
                record["owner"] = {
                    "pid": self.owner_pid,
                    **process_identity_fields(self.owner_pid),
                }
            atomic_write_json(directory / "status.json", record)
            # Mirror the credential-free checkpoint config, without changing the
            # parent process's environment or using its live execution settings.
            atomic_write_json(
                directory / "runtime-config.json",
                {"system": {"api_key": ""}, "wallets": []},
            )
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                in {
                    "PATH",
                    "HOME",
                    "LANG",
                    "LC_ALL",
                    "TMPDIR",
                    "SYSTEMROOT",
                    "OPENBLAS_NUM_THREADS",
                    "OMP_NUM_THREADS",
                    "MKL_NUM_THREADS",
                }
            }
            env["WAYFINDER_CONFIG_PATH"] = str(directory / "runtime-config.json")
            env["WAYFINDER_BACKTEST_RUNNER"] = "local"
            with (directory / "supervisor.log").open("wb") as log:
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "wayfinder_paths.jobs.local_backtest_worker",
                        str(directory),
                    ],
                    cwd=directory,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    start_new_session=True,
                )
            # Reap children while this client remains alive; unowned runs
            # continue normally after a submitting CLI or MCP process exits.
            threading.Thread(target=proc.wait, daemon=True).start()
            return record
        except Exception:
            shutil.rmtree(directory)
            raise

    def status(self, run_id: str) -> dict[str, Any]:
        directory = self._directory(run_id)
        record = json.loads((directory / "status.json").read_text())
        if record["status"] not in TERMINAL_STATUSES:
            identity_path = directory / "supervisor.json"
            if identity_path.exists():
                identity = json.loads(identity_path.read_text())
                if not recorded_process_alive(identity):
                    # Re-read after observing exit: the worker publishes completion
                    # atomically before leaving, which may race the first read.
                    record = json.loads((directory / "status.json").read_text())
                    if record["status"] not in TERMINAL_STATUSES:
                        _kill_orphaned_group(identity.get("child"))
                        record.update(
                            status="failed",
                            error="Local worker exited without a completion record",
                        )
            elif time.time() - (directory / "status.json").stat().st_mtime > 30:
                record.update(status="failed", error="Local worker did not start")
        return record

    def cancel(self, run_id: str) -> None:
        if self.status(run_id)["status"] not in TERMINAL_STATUSES:
            (self._directory(run_id) / "cancel").touch()

    def collect(self, run_id: str, destination: Path) -> dict[str, Any]:
        record = self.status(run_id)
        metadata = record["artifacts"]
        if not metadata:
            raise ValueError(
                f"No artifacts available ({record['status']}): {record['error']}"
            )
        if destination.exists():
            raise FileExistsError(
                "Collect into a new directory to preserve existing job state"
            )
        archive = self._directory(run_id) / "artifacts.tar.gz"
        if (
            archive.stat().st_size != metadata["size"]
            or sha256(archive) != metadata["sha256"]
        ):
            raise ValueError("Artifact checksum or size mismatch")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            staged = Path(temporary) / "workspace"
            extract_archive(archive, staged)
            staged.rename(destination)
        return record


class FallbackRunner(BacktestRunner):
    """Run on the configured provider; when it refuses capacity, run locally.

    Run ids route back to whichever runner started them, so status, cancel and
    collection work the same after a fallback.
    """

    def __init__(self, primary: BacktestRunner, local: LocalRunner):
        super().__init__(primary.config, owner_pid=primary.owner_pid)
        self.primary, self.local = primary, local

    def submit_archive(self, archive: Path) -> dict[str, Any]:
        try:
            return self.primary.submit_archive(archive)
        except ComputeUnavailable as exc:
            fallback = {"fallback": {"from": self.config.provider, "reason": str(exc)}}
            return self.local.submit_archive(archive, extra=fallback)

    def _runner(self, run_id: str) -> BacktestRunner:
        return self.local if self.local.owns(run_id) else self.primary

    def status(self, run_id: str) -> dict[str, Any]:
        return self._runner(run_id).status(run_id)

    def cancel(self, run_id: str) -> None:
        self._runner(run_id).cancel(run_id)

    def collect(self, run_id: str, destination: Path) -> dict[str, Any]:
        return self._runner(run_id).collect(run_id, destination)

    def close(self) -> None:
        self.primary.close()
        self.local.close()


def create_runner(
    *,
    repo_root: Path | None = None,
    config: RunnerConfig | None = None,
    owner_pid: int | None = None,
) -> BacktestRunner:
    settings = config or load_runner_config(repo_root=repo_root)
    try:
        implementation = RUNNERS[settings.provider]
    except KeyError as exc:
        raise ValueError(f"Unknown backtest runner: {settings.provider}") from exc
    runner = implementation(settings, owner_pid=owner_pid)
    if settings.provider == "local" or settings.fallback != "local":
        return runner
    local = LocalRunner(replace(settings, provider="local"), owner_pid=owner_pid)
    return FallbackRunner(runner, local)


def run_configured_operation(
    op: str, kwargs: dict[str, Any], *, config: RunnerConfig
) -> dict[str, Any]:
    """Bridge existing agent operations to the configured lifecycle contract.

    The run's outputs are applied back to the job, as in-place execution
    would have left them, and a terminated caller cancels its run.
    """
    if op not in OPERATIONS:
        raise ValueError(f"Unsupported portable computation: {op}")
    options = dict(kwargs)
    job_id = options.pop("job_id")
    store = JobStore()
    owner = _owner()
    with (
        _exit_on_termination(),
        create_runner(config=config, owner_pid=os.getpid()) as runner,
    ):
        submitted = runner.submit(store, job_id, op=op, options=options)
        run_id = submitted["id"]
        receipt_dir = _receipt(config, submitted, owner)
        result = _wait_or_cancel(runner, run_id)
        if result.get("artifacts"):
            destination = receipt_dir / "artifacts"
            runner.collect(run_id, destination)
            # Failed runs apply too: in place, their partial ledgers persist.
            result = {
                **result,
                "artifacts_path": str(destination),
                "applied": apply_job_outputs(store, destination),
            }
        atomic_write_json(receipt_dir / "run.json", {**result, "owner": owner})
        _prune_receipts(config.runs_dir, config.retain_runs)
        if result["status"] != "succeeded":
            raise RuntimeError(
                f"{op} {result['status']}: {result.get('error', '')}; run record: {receipt_dir / 'run.json'}"
            )
        return result


def run_phase(
    phase: Callable[..., Any],
    root: Path,
    paths: Sequence[str],
    args: Mapping[str, Any] | None = None,
    *,
    config: RunnerConfig | None = None,
) -> PhaseOutcome:
    """Run a registered pure phase on the configured runner.

    Only the named inputs travel and only the phase's ``outputs/`` returns.
    Nothing is applied to any job; the caller owns the result. Collected
    outputs live in a run receipt that retention eventually prunes.
    """
    name = phase_name(phase)
    resolve_phase(name)
    root = root.resolve()
    settings = config or load_runner_config(repo_root=root)
    if any(
        settings.runs_dir.is_relative_to((root / safe_relative(path)).resolve())
        for path in paths
    ):
        raise ValueError("runs_dir must be outside the phase inputs")
    owner = _owner()
    with (
        _exit_on_termination(),
        create_runner(config=settings, owner_pid=os.getpid()) as runner,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "inputs.tar.gz"
            pack_inputs(
                root,
                paths,
                {"phase": name, "args": dict(args or {})},
                archive,
                expected_sdk_commit=settings.sdk_commit,
            )
            submitted = runner.submit_archive(archive)
        run_id = submitted["id"]
        receipt_dir = _receipt(settings, submitted, owner)
        result = _wait_or_cancel(runner, run_id)
        collected = SpriteWorkspace(receipt_dir / "collected")
        if result.get("artifacts"):
            runner.collect(run_id, collected.root)
        atomic_write_json(receipt_dir / "run.json", {**result, "owner": owner})
        _prune_receipts(settings.runs_dir, settings.retain_runs)
        if result["status"] != "succeeded":
            record = (
                json.loads(collected.phase_error_file.read_text())
                if collected.phase_error_file.exists()
                else {}
            )
            error = str(record.get("error") or result.get("error", ""))
            raise PhaseFailed(
                f"{name} {result['status']}: {error}; run record: {receipt_dir / 'run.json'}",
                run=result,
                error=error,
                error_type=record.get("error_type"),
                stage=record.get("stage"),
                outputs_path=(
                    str(collected.outputs_dir)
                    if collected.outputs_dir.is_dir()
                    else None
                ),
            )
        return {
            "result": json.loads(collected.phase_result_file.read_text()),
            "outputs_path": str(collected.outputs_dir),
            "run": result,
        }


def _owner() -> dict[str, Any]:
    return {"pid": os.getpid(), **process_identity_fields(os.getpid())}


def _receipt(
    config: RunnerConfig, submitted: Mapping[str, Any], owner: Mapping[str, Any]
) -> Path:
    receipt_dir = config.runs_dir / "receipts" / str(uuid.uuid4())
    receipt_dir.mkdir(parents=True, mode=0o700)
    atomic_write_json(receipt_dir / "run.json", {**submitted, "owner": owner})
    return receipt_dir


def _wait_or_cancel(runner: BacktestRunner, run_id: str) -> dict[str, Any]:
    try:
        return runner.wait(run_id)
    except BaseException:
        # A caller that stops waiting must not leave a billed or busy run behind.
        with suppress(Exception):
            runner.cancel(run_id)
        raise


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", ""))[:500]
    except (ValueError, AttributeError):
        return ""


@contextmanager
def _exit_on_termination() -> Iterator[None]:
    """SIGTERM/SIGHUP unwind the caller like Ctrl-C, so it cancels its run.

    A handler the process already had still runs once the run is cancelled:
    a heavy-lane op child must record its own cancellation (see op_runner).
    """
    received: list[int] = []

    def stop(signum: int, frame: FrameType | None) -> None:
        received.append(signum)
        raise SystemExit(128 + signum)

    previous = {
        sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGHUP)
    }
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        handler = previous[signal.Signals(received[0])] if received else None
        if callable(handler):
            handler(received[0], None)


def _kill_orphaned_group(child: Any) -> None:
    # The worker starts its compute child in a new session: pgid == pid.
    if isinstance(child, dict) and recorded_process_alive(child):
        try:
            os.killpg(child["pid"], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # macOS reports EPERM for a group of only unreaped zombies.


def _prune_receipts(runs_dir: Path, keep: int) -> None:
    finished: list[tuple[float, Path]] = []
    for receipt in (runs_dir / "receipts").glob("*/run.json"):
        try:
            record = json.loads(receipt.read_text())
            modified = receipt.stat().st_mtime
        except (OSError, ValueError):
            continue  # Pruned or being written by a concurrent operation.
        if record.get("status") in TERMINAL_STATUSES or not recorded_process_alive(
            record.get("owner") or {}
        ):
            finished.append((modified, receipt.parent))
    _prune(finished, keep)


def _prune(entries: Iterable[tuple[float, Path]], keep: int) -> None:
    for _, directory in sorted(entries, reverse=True)[keep:]:
        shutil.rmtree(directory, ignore_errors=True)
