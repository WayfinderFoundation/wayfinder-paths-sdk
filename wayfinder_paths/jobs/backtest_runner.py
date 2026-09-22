"""Configured compute backends with one workspace and run lifecycle contract."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from wayfinder_paths.jobs.execution.op_process import recorded_process_alive
from wayfinder_paths.jobs.sprite_bundle import (
    OPERATIONS,
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
    timeout_seconds: int = 900
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
        "WAYFINDER_BACKTEST_TIMEOUT_SECONDS", section.get("timeout_seconds", 900)
    )
    if isinstance(timeout, bool) or not isinstance(timeout, (int, str)):
        raise ValueError("timeout_seconds must be an integer")
    try:
        timeout = int(timeout)
    except ValueError as exc:
        raise ValueError("timeout_seconds must be an integer") from exc
    if not 1 <= timeout <= 21600:
        raise ValueError("timeout_seconds must be between 1 and 21600")
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
        extra_paths=tuple(extra),
        sdk_commit=commit,
        backend=backend,
        app_name=app,
        preset=preset,
        api_key=key,
    )


class BacktestRunner(ABC):
    """One operation per run; collection always targets a fresh directory."""

    def __init__(self, config: RunnerConfig):
        self.config = config

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
        self, config: RunnerConfig, *, client: SpriteBacktestsClient | None = None
    ):
        super().__init__(config)
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
            record = {
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
            # Reap children while this client remains alive; detached processes
            # continue normally after a submitting CLI or MCP process exits.
            import threading

            threading.Thread(target=proc.wait, daemon=True).start()
            return record
        except Exception:
            import shutil

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
    *, repo_root: Path | None = None, config: RunnerConfig | None = None
) -> BacktestRunner:
    settings = config or load_runner_config(repo_root=repo_root)
    providers = {"local": LocalRunner, "sprites": SpritesRunner}
    try:
        implementation = providers[settings.provider]
    except KeyError as exc:
        raise ValueError(f"Unknown backtest runner: {settings.provider}") from exc
    return implementation(settings)


def run_configured_operation(
    op: str, kwargs: dict[str, Any], *, config: RunnerConfig
) -> dict[str, Any]:
    """Bridge existing agent operations to the configured lifecycle contract."""
    if op not in OPERATIONS:
        raise ValueError(f"Unsupported portable computation: {op}")
    options = dict(kwargs)
    job_id = options.pop("job_id")
    with create_runner(config=config) as runner:
        submitted = runner.submit(JobStore(), job_id, op=op, options=options)
        run_id = submitted["id"]
        receipt_dir = config.runs_dir / "receipts" / str(uuid.uuid4())
        receipt_dir.mkdir(parents=True, mode=0o700)
        atomic_write_json(receipt_dir / "run.json", submitted)
        try:
            result = runner.wait(run_id)
            if result.get("artifacts"):
                destination = receipt_dir / "artifacts"
                runner.collect(run_id, destination)
                result = {**result, "artifacts_path": str(destination)}
            atomic_write_json(receipt_dir / "run.json", result)
            if result["status"] != "succeeded":
                raise RuntimeError(
                    f"{op} {result['status']}: {result.get('error', '')}; run record: {receipt_dir / 'run.json'}"
                )
            return result
        except (KeyboardInterrupt, SystemExit):
            runner.cancel(run_id)
            raise
