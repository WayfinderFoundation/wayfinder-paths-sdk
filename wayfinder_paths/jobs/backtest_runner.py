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
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any, Self
from urllib.parse import urlsplit

from wayfinder_paths.jobs.execution.op_process import (
    process_identity_fields,
    recorded_process_alive,
)
from wayfinder_paths.jobs.sprite_bundle import (
    OPERATIONS,
    apply_job_outputs,
    extract_archive,
    pack_job,
    sha256,
)
from wayfinder_paths.jobs.sprite_client import TERMINAL_STATUSES, SpriteBacktestsClient
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.monitor_state import atomic_write_json


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    provider: str
    runs_dir: Path
    configured: bool = False
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
    if provider not in {"local", "sprites"}:
        raise ValueError("backtest_runner.provider must be local or sprites")
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

    @abstractmethod
    def submit(
        self,
        store: JobStore,
        job_id: str,
        *,
        op: str = "backtest_job",
        options: dict[str, Any] | None = None,
        extra_paths: list[str] | None = None,
    ) -> dict[str, Any]: ...

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

    def submit(
        self,
        store: JobStore,
        job_id: str,
        *,
        op: str = "backtest_job",
        options: dict[str, Any] | None = None,
        extra_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            **self.client.submit(
                store,
                job_id,
                op=op,
                options=options,
                extra_paths=[*self.config.extra_paths, *(extra_paths or [])],
                preset=self.config.preset,
                expected_sdk_commit=self.config.sdk_commit,
            ),
            "provider": "sprites",
        }

    def status(self, run_id: str) -> dict[str, Any]:
        return {**self.client.status(run_id), "provider": "sprites"}

    def cancel(self, run_id: str) -> None:
        self.client.cancel(run_id)

    def collect(self, run_id: str, destination: Path) -> dict[str, Any]:
        return {**self.client.collect(run_id, destination), "provider": "sprites"}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class LocalRunner(BacktestRunner):
    def _directory(self, run_id: str) -> Path:
        # Local IDs are UUIDs, never user-supplied filesystem paths.
        return self.config.runs_dir / str(uuid.UUID(run_id))

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
        self._prune_runs()
        run_id = str(uuid.uuid4())
        directory = self._directory(run_id)
        directory.mkdir(parents=True, mode=0o700)
        try:
            pack_job(
                store,
                job_id,
                directory / "workspace.tar.gz",
                op=op,
                options=options,
                extra_paths=[*self.config.extra_paths, *(extra_paths or [])],
                expected_sdk_commit=self.config.sdk_commit,
            )
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


def create_runner(
    *,
    repo_root: Path | None = None,
    config: RunnerConfig | None = None,
    owner_pid: int | None = None,
) -> BacktestRunner:
    settings = config or load_runner_config(repo_root=repo_root)
    providers = {"local": LocalRunner, "sprites": SpritesRunner}
    try:
        implementation = providers[settings.provider]
    except KeyError as exc:
        raise ValueError(f"Unknown backtest runner: {settings.provider}") from exc
    return implementation(settings, owner_pid=owner_pid)


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
    owner = {"pid": os.getpid(), **process_identity_fields(os.getpid())}
    with (
        _exit_on_termination(),
        create_runner(config=config, owner_pid=os.getpid()) as runner,
    ):
        submitted = runner.submit(store, job_id, op=op, options=options)
        run_id = submitted["id"]
        receipt_dir = config.runs_dir / "receipts" / str(uuid.uuid4())
        receipt_dir.mkdir(parents=True, mode=0o700)
        atomic_write_json(receipt_dir / "run.json", {**submitted, "owner": owner})
        try:
            result = runner.wait(run_id)
        except (KeyboardInterrupt, SystemExit):
            runner.cancel(run_id)
            raise
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


@contextmanager
def _exit_on_termination() -> Iterator[None]:
    """SIGTERM/SIGHUP unwind the caller like Ctrl-C, so it cancels its run."""

    def stop(signum: int, frame: FrameType | None) -> None:
        raise SystemExit(128 + signum)

    previous = {
        sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGHUP)
    }
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


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
