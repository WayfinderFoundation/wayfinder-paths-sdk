"""Submit complete SDK workspaces to Django-managed Sprite backtest workers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import httpx

from wayfinder_paths.jobs.sprite_bundle import (
    MAX_ARCHIVE_BYTES,
    OPERATIONS,
    ArchiveMetadata,
    extract_archive,
    pack_job,
    sha256,
)
from wayfinder_paths.jobs.store import JobStore

CHUNK_BYTES = 8 * 1024 * 1024
UPLOAD_ATTEMPTS = 3
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timed_out", "cancelled"})


@dataclass(frozen=True, slots=True)
class SpriteRoutes:
    """Keep owner-authenticated and worker-authenticated routes in one place."""

    app_name: str

    @property
    def instance(self) -> str:
        return f"/api/v1/opencode/instances/{self.app_name}/sprite-backtests/"

    def lease(self, lease_id: str) -> str:
        return f"{self.instance}{lease_id}/"

    def artifacts(self, lease_id: str) -> str:
        return f"{self.lease(lease_id)}artifacts/"

    def workspace(self, lease_id: str) -> str:
        return self._worker(lease_id, "workspace")

    def jobs(self, lease_id: str) -> str:
        return self._worker(lease_id, "jobs")

    @staticmethod
    def _worker(lease_id: str, resource: str) -> str:
        return f"/api/v1/opencode/sprite-backtests/{lease_id}/{resource}/"


class SpriteBacktestsClient:
    """Manage remote jobs; injected HTTP clients remain owned by the caller."""

    def __init__(
        self,
        backend: str,
        app_name: str,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._owns_http = client is None
        self.http = (
            client
            if client is not None
            else httpx.Client(
                base_url=backend.rstrip("/"), timeout=180, follow_redirects=False
            )
        )
        self.owner_headers = {"X-API-Key": api_key}
        self.routes = SpriteRoutes(app_name)
        self.instance_path = self.routes.instance
        self._sleep = sleep if sleep is not None else time.sleep

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def _upload_chunk(
        self, path: str, headers: Mapping[str, str], chunk: bytes
    ) -> httpx.Response:
        for attempt in range(UPLOAD_ATTEMPTS):
            try:
                response = self.http.put(path, content=chunk, headers=headers)
                response.raise_for_status()
                return response
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                client_error = (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code < 500
                )
                if client_error or attempt == UPLOAD_ATTEMPTS - 1:
                    raise
                self._sleep(attempt + 1)
        raise AssertionError("Upload retry loop must return or raise")

    def _upload(
        self,
        path: str,
        headers: Mapping[str, str],
        archive: Path,
        packed: ArchiveMetadata,
    ) -> None:
        response: httpx.Response | None = None
        with archive.open("rb") as stream:
            for part, chunk in enumerate(iter(lambda: stream.read(CHUNK_BYTES), b"")):
                response = self._upload_chunk(
                    path,
                    {
                        **headers,
                        "Content-Type": "application/gzip",
                        "X-Content-SHA256": hashlib.sha256(chunk).hexdigest(),
                        "X-Bundle-SHA256": packed["sha256"],
                        "X-Bundle-Size": str(packed["size"]),
                        "X-Bundle-Part": str(part),
                    },
                    chunk,
                )
        if response is None:
            raise ValueError("Cannot upload an empty workspace archive")
        if response.json() != {"sha256": packed["sha256"], "size": packed["size"]}:
            raise ValueError("Workspace upload was not confirmed")

    def submit(
        self,
        store: JobStore,
        job_id: str,
        *,
        preset: str = "jobs-v1",
        op: str = "backtest_job",
        options: dict[str, Any] | None = None,
        extra_paths: list[str] | None = None,
        expected_sdk_commit: str | None = None,
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "workspace.tar.gz"
            packed = pack_job(
                store,
                job_id,
                archive,
                op=op,
                options=options,
                extra_paths=extra_paths,
                expected_sdk_commit=expected_sdk_commit,
            )
            response = self.http.post(
                self.routes.instance,
                headers=self.owner_headers,
                json={"preset_key": preset},
            )
            response.raise_for_status()
            lease = response.json()
            try:
                if "sdk-workspace-v1" not in lease.get("runtime", {}).get(
                    "capabilities", []
                ):
                    raise ValueError(
                        "Preset needs a checkpoint with sdk-workspace-v1 support"
                    )
                if (
                    expected_sdk_commit
                    and lease["runtime"].get("sdk_commit") != expected_sdk_commit
                ):
                    raise ValueError(
                        "Preset SDK commit does not match requested version"
                    )
                headers = {"Authorization": "Bearer " + lease["auth_token"]}
                self._upload(
                    self.routes.workspace(lease["id"]), headers, archive, packed
                )
                response = self.http.post(
                    self.routes.jobs(lease["id"]),
                    headers=headers,
                    json={"kind": "sdk_job", "workspace_sha256": packed["sha256"]},
                )
                response.raise_for_status()
                return {
                    key: value
                    for key, value in lease.items()
                    if key not in {"auth_token", "auth_header"}
                }
            except BaseException:
                # Owner cancellation survives agent capability expiry, and a
                # terminated submitter must not leave a billed lease running.
                with suppress(httpx.HTTPError):
                    self.cancel(lease["id"])
                raise

    def status(self, lease_id: str) -> dict[str, Any]:
        response = self.http.get(
            self.routes.lease(lease_id), headers=self.owner_headers
        )
        response.raise_for_status()
        return response.json()

    def cancel(self, lease_id: str) -> None:
        response = self.http.delete(
            self.routes.lease(lease_id), headers=self.owner_headers
        )
        response.raise_for_status()

    def wait(self, lease_id: str, *, poll_interval: float = 5.0) -> dict[str, Any]:
        """Poll until the lease is terminal, using the configured sleep function."""
        while True:
            status = self.status(lease_id)
            if status["status"] in TERMINAL_STATUSES:
                return status
            self._sleep(poll_interval)

    def collect(self, lease_id: str, destination: Path) -> dict[str, Any]:
        status = self.status(lease_id)
        metadata = status.get("artifacts")
        if not metadata:
            raise ValueError(
                f"No artifacts available ({status['status']}): {status.get('error', '')}"
            )
        if destination.exists():
            raise FileExistsError(
                "Collect into a new directory to preserve existing job state"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
            archive = Path(directory) / "artifacts.tar.gz"
            size = 0
            with (
                self.http.stream(
                    "GET",
                    self.routes.artifacts(lease_id),
                    headers=self.owner_headers,
                ) as response,
                archive.open("wb") as stream,
            ):
                response.raise_for_status()
                for chunk in response.iter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise ValueError("Artifact download exceeds 512 MiB")
                    stream.write(chunk)
            if size != metadata["size"] or sha256(archive) != metadata["sha256"]:
                raise ValueError("Artifact checksum or size mismatch")
            staged = Path(directory) / "workspace"
            extract_archive(archive, staged)
            staged.rename(destination)
        return status


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--preset", default="jobs-v1")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--job-id")
    parser.add_argument("--op", choices=sorted(OPERATIONS), default="backtest_job")
    parser.add_argument(
        "--options",
        type=Path,
        help="JSON object with the existing SDK operation options",
    )
    parser.add_argument(
        "--extra-path",
        action="append",
        default=[],
        help="Additional repository-relative source/data path",
    )
    parser.add_argument("--sdk-commit")
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--submit-only", action="store_true")
    mode.add_argument(
        "--collect",
        help="Collect an existing remote lease by id using owner authentication",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.collect and not args.job_id:
        parser.error("--job-id is required for submission")
    if not args.submit_only and not args.output:
        parser.error("--output is required for collection")
    api_key = os.environ.get("WAYFINDER_API_KEY")
    if not api_key:
        parser.error("WAYFINDER_API_KEY must be set")
    options: dict[str, Any] = {}
    if args.options:
        try:
            options = json.loads(args.options.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            parser.error(f"Cannot read --options: {exc}")
        if not isinstance(options, dict):
            parser.error("--options must contain a JSON object")
    with SpriteBacktestsClient(args.backend, args.app_name, api_key) as client:
        if args.collect:
            lease_id = args.collect
        else:
            lease = client.submit(
                JobStore(repo_root=args.repo),
                args.job_id,
                preset=args.preset,
                op=args.op,
                options=options,
                extra_paths=args.extra_path,
                expected_sdk_commit=args.sdk_commit,
            )
            lease_id = lease["id"]
            print(json.dumps(lease), flush=True)
            if args.submit_only:
                return
        status = client.wait(lease_id)
        if status.get("artifacts"):
            client.collect(lease_id, args.output)
        print(json.dumps(status, indent=2))
        if status["status"] != "succeeded":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
