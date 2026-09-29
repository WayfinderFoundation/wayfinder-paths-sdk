"""The Sprite lease protocol in memory: Django's owner lease endpoints and the
Sprite worker's HTTP API, running the real portable runtime for every job."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from wayfinder_paths.jobs.sprite_bundle import sha256
from wayfinder_paths.jobs.sprite_client import SpriteBacktestsClient

BACKEND = "https://backend.example"
API_KEY = "owner-key"
# The owner routes: booked by API key alone, or under a named Shell.
LEASES = "/api/v1/opencode/sprite-leases/"
SHELL_LEASES = "/api/v1/opencode/instances/shell/sprite-backtests/"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Upload:
    sha256: str
    size: int
    parts: list[bytes] = field(default_factory=list)


@dataclass
class Worker:
    lease_id: str
    token_sha256: str
    directory: Path
    # The worker closed itself (idle timeout): it answers 410 until wiped.
    closed: bool = False
    # Django closed the lease: the URL is private (or leased again), so the
    # node's token is refused with 401.
    gone: bool = False
    base: dict[str, Any] | None = None
    staged: dict[str, Any] | None = None
    uploads: dict[str, Upload] = field(default_factory=dict)
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    bodies: dict[str, dict[str, Any]] = field(default_factory=dict)

    def running(self) -> bool:
        return any(job["status"] == "running" for job in self.jobs.values())


class FakeSprites:
    def __init__(
        self,
        root: Path,
        *,
        refuse: int | None = None,
        capabilities: tuple[str, ...] = ("sdk-workspace-v1",),
        lease_seconds: int = 3600,
        timeout_seconds: int = 900,
        transfer_timeout_seconds: int = 600,
        setup_timeout_seconds: int = 0,
    ) -> None:
        self.root = root
        self.refuse = refuse
        self.capabilities = capabilities
        self.lease_seconds = lease_seconds
        self.timeout_seconds = timeout_seconds
        self.transfer_timeout_seconds = transfer_timeout_seconds
        self.setup_timeout_seconds = setup_timeout_seconds
        self.requests: list[httpx.Request] = []
        self.leases: dict[str, dict[str, Any]] = {}
        self.workers: dict[str, Worker] = {}
        self.base_uploads: list[str] = []
        # Failure injection, each consumed as it fires.
        self.worker_down = False
        self.corrupt_artifacts = 0
        self.lost_bookings = 0
        # Polls a new lease stays provisioning; None never becomes ready.
        self.provisioning_polls: int | None = 0
        self.closed_on_booking = False
        self.lost_chunk_answers: set[tuple[str, int]] = set()
        self.conflicts: set[str] = set()
        self.hold_jobs = False
        self._provisioning: dict[str, int | None] = {}
        self._hashes: dict[str, str] = {}
        self._hosts: dict[str, str] = {}

    def client(
        self,
        *,
        lease_dir: Path | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        sdk_commit: str | None = None,
        app_name: str | None = None,
    ) -> SpriteBacktestsClient:
        http = httpx.Client(
            transport=httpx.MockTransport(self.handle), base_url=BACKEND
        )
        return SpriteBacktestsClient(
            BACKEND,
            app_name,
            API_KEY,
            client=http,
            sleep=sleep if sleep is not None else lambda seconds: None,
            clock=clock,
            lease_dir=lease_dir if lease_dir is not None else self.root / "leases",
            sdk_commit=sdk_commit,
        )

    def idle_out(self, lease_id: str) -> None:
        """The worker closed at its idle deadline; Django has not heard yet."""
        self.workers[self._hosts[lease_id]].closed = True

    def backend_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "backend.example"]

    def worker_requests(self, path: str | None = None) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.url.host != "backend.example" and path in {None, r.url.path}
        ]

    def bookings(self) -> list[httpx.Request]:
        return [
            r
            for r in self.backend_requests()
            if r.method == "POST" and r.url.path in {LEASES, SHELL_LEASES}
        ]

    def close(self, lease_id: str, reason: str) -> None:
        """Django's close and wipe: running jobs end, the URL is withdrawn."""
        lease = self.leases[lease_id]
        if lease["status"] == "closed":
            return
        for job in lease["jobs"]:
            if job["status"] == "running":
                job.update(
                    status="cancelled" if reason == "released" else "timed_out",
                    error=f"Lease closed: {reason}",
                    finished_at=_now(),
                )
        lease.update(
            status="closed", closed_reason=reason, worker_url="", wiped_at=_now()
        )
        worker = self.workers[self._hosts[lease_id]]
        worker.gone = True
        shutil.rmtree(worker.directory, ignore_errors=True)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "backend.example":
            return self._django(request)
        if self.worker_down:
            raise httpx.ConnectError("Sprite unreachable", request=request)
        return self._worker(request)

    def _django(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == API_KEY
        assert "Authorization" not in request.headers
        if request.method == "POST" and request.url.path in {LEASES, SHELL_LEASES}:
            if self.refuse is not None:
                return httpx.Response(
                    self.refuse, json={"detail": "worker limit reached"}
                )
            body = json.loads(request.content)
            assert set(body) <= {"preset_key", "token_sha256", "sdk_commit", "purpose"}
            assert re.fullmatch(r"[0-9a-f]{64}", body["token_sha256"])
            assert re.fullmatch(r"[0-9a-f]{40}", body.get("sdk_commit", "0" * 40))
            known = self._hashes.get(body["token_sha256"])
            if known is not None and self.leases[known]["wiped_at"] is None:
                # Idempotent booking: the same token gets its unwiped lease.
                return httpx.Response(200, json=self.leases[known])
            if any(lease["wiped_at"] is None for lease in self.leases.values()):
                return httpx.Response(409, json={"detail": "active lease limit"})
            lease = self._book(body)
            if self.lost_bookings or lease["status"] == "provisioning":
                # Django answers only once provisioning finishes; a client that
                # gave up first retries and sees the lease still provisioning.
                self.lost_bookings = max(0, self.lost_bookings - 1)
                raise httpx.ReadTimeout("booking answer lost", request=request)
            return httpx.Response(201, json=lease)
        root = LEASES if request.url.path.startswith(LEASES) else SHELL_LEASES
        lease_id = request.url.path.removeprefix(root).strip("/")
        if lease_id not in self.leases:
            return httpx.Response(404, json={"detail": "Not found."})
        if request.method == "DELETE":
            self.close(lease_id, "released")
        elif self.leases[lease_id]["status"] == "provisioning":
            remaining = self._provisioning[lease_id]
            if remaining is not None:
                self._provisioning[lease_id] = remaining - 1
                if remaining <= 1:
                    self.leases[lease_id]["status"] = "ready"
        return httpx.Response(200, json=self.leases[lease_id])

    def _book(self, body: dict[str, Any]) -> dict[str, Any]:
        lease_id = str(uuid.uuid4())
        host = f"sprite-{len(self.leases)}.sprites.example"
        now = datetime.now(UTC)
        self._hashes[body["token_sha256"]] = lease_id
        self._hosts[lease_id] = host
        self._provisioning[lease_id] = self.provisioning_polls
        self.leases[lease_id] = {
            "id": lease_id,
            "provider": "sprites",
            "sprite_name": host.split(".")[0],
            "preset_key": body["preset_key"],
            "purpose": body.get("purpose", ""),
            "status": "ready" if self.provisioning_polls == 0 else "provisioning",
            "closed_reason": "",
            "error": "",
            "worker_url": f"https://{host}",
            "created": now.isoformat(),
            "expires_at": (now + timedelta(seconds=self.lease_seconds)).isoformat(),
            "idle_timeout_seconds": 600,
            "timeout_seconds": self.timeout_seconds,
            "transfer_timeout_seconds": self.transfer_timeout_seconds,
            "setup_timeout_seconds": self.setup_timeout_seconds,
            "last_activity_at": now.isoformat(),
            "wiped_at": None,
            "wipe_pending": False,
            # Like Django's sdk_runtime presets: the Sprite runs the requested commit.
            "runtime": {
                "capabilities": list(self.capabilities),
                "sdk_commit": body.get("sdk_commit"),
            },
            "jobs": [],
        }
        self.workers[host] = Worker(
            lease_id,
            body["token_sha256"],
            self.root / "sprites" / host,
            closed=self.closed_on_booking,
        )
        return self.leases[lease_id]

    def _worker(self, request: httpx.Request) -> httpx.Response:
        worker = self.workers[request.url.host]
        assert "X-API-Key" not in request.headers
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if (
            worker.gone
            or hashlib.sha256(token.encode()).hexdigest() != worker.token_sha256
        ):
            return httpx.Response(401, json={"detail": "Invalid lease token"})
        if worker.closed:
            return httpx.Response(410, json={"detail": "Lease closed"})
        path, method = request.url.path, request.method
        if method == "PUT" and path in {"/base", "/workspace"}:
            return self._chunk(worker, path[1:], request)
        if method == "POST" and path == "/jobs":
            return self._run(worker, json.loads(request.content))
        if method == "GET" and path == "/status":
            lease = self.leases[worker.lease_id]
            current = next(iter(worker.jobs.values()), None)
            return httpx.Response(
                200,
                json={
                    "lease_status": "running" if worker.running() else "ready",
                    "job": {key: current[key] for key in ("job_id", "status")}
                    if current
                    else None,
                    "base": {
                        key: value
                        for key, value in (worker.base or {}).items()
                        if key in {"sha256", "size"}
                    },
                    "idle_deadline": datetime.now(UTC).timestamp()
                    + lease["idle_timeout_seconds"],
                    "expires_at": datetime.fromisoformat(
                        lease["expires_at"]
                    ).timestamp(),
                },
            )
        parts = path.split("/")
        job = worker.jobs.get(parts[2]) if parts[1:2] == ["jobs"] else None
        if job is None:
            return httpx.Response(404, json={"detail": "Unknown job"})
        if method == "GET" and len(parts) == 3:
            return httpx.Response(
                200,
                json={key: job[key] for key in ("job_id", "status", "result", "error")},
            )
        if method == "GET" and parts[3:] == ["artifacts"]:
            archive = job["directory"] / "artifacts.tar.gz"
            if not archive.exists():
                return httpx.Response(404, json={"detail": "No artifacts"})
            data = archive.read_bytes()
            if self.corrupt_artifacts:
                self.corrupt_artifacts -= 1
                data = data[:-1] + bytes([data[-1] ^ 1])
            return httpx.Response(
                200,
                content=data,
                headers={
                    "Content-Type": "application/gzip",
                    "X-Content-SHA256": sha256(archive),
                },
            )
        if method == "DELETE":
            if job["status"] == "running":
                self._finish(worker, job["job_id"], "cancelled", "Cancelled by node")
            return httpx.Response(
                202, json={"job_id": job["job_id"], "status": job["status"]}
            )
        return httpx.Response(405, json={"detail": "Method not allowed"})

    def _chunk(
        self, worker: Worker, kind: str, request: httpx.Request
    ) -> httpx.Response:
        headers = request.headers
        assert headers["Content-Type"] == "application/gzip"
        assert (
            hashlib.sha256(request.content).hexdigest() == headers["X-Content-SHA256"]
        )
        digest, size = headers["X-Bundle-SHA256"], int(headers["X-Bundle-Size"])
        part = int(headers["X-Bundle-Part"])
        if worker.running():
            return httpx.Response(409, json={"detail": "A job is running"})
        if kind in self.conflicts:
            # The worker lost this upload's earlier parts (e.g. it restarted).
            self.conflicts.discard(kind)
            worker.uploads.pop(kind, None)
            return httpx.Response(409, json={"detail": "Upload out of sequence"})
        if kind == "base" and worker.base is not None:
            if worker.base["sha256"] != digest:
                return httpx.Response(409, json={"detail": "Lease base is immutable"})
            parts = worker.base["parts"]
            if part >= len(parts) or parts[part] != headers["X-Content-SHA256"]:
                return httpx.Response(409, json={"detail": "Chunk differs"})
            if part < len(parts) - 1:
                return httpx.Response(200, json={"part": part, "accepted": True})
            return httpx.Response(200, json={"sha256": digest, "size": size})
        upload = worker.uploads.get(kind)
        if upload is None or upload.sha256 != digest:
            upload = worker.uploads[kind] = Upload(digest, size)
        if part == len(upload.parts):
            upload.parts.append(request.content)
        elif part > len(upload.parts):
            return httpx.Response(409, json={"detail": "Parts are sequential"})
        elif upload.parts[part] != request.content:
            return httpx.Response(409, json={"detail": "Chunk differs"})
        data = b"".join(upload.parts)
        if (kind, part) in self.lost_chunk_answers:
            self.lost_chunk_answers.discard((kind, part))
            raise httpx.ReadTimeout("chunk answer lost", request=request)
        if len(data) < size:
            return httpx.Response(200, json={"part": part, "accepted": True})
        assert hashlib.sha256(data).hexdigest() == digest and len(data) == size
        archive = worker.directory / f"{kind}-{digest}.tar.gz"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(data)
        del worker.uploads[kind]
        stored = {
            "sha256": digest,
            "size": size,
            "path": archive,
            "parts": [hashlib.sha256(chunk).hexdigest() for chunk in upload.parts],
        }
        if kind == "base":
            worker.base = stored
            self.base_uploads.append(worker.lease_id)
        else:
            worker.staged = stored
        return httpx.Response(200, json={"sha256": digest, "size": size})

    def _run(self, worker: Worker, body: dict[str, Any]) -> httpx.Response:
        assert set(body) - {"purpose"} == {
            "job_id",
            "kind",
            "workspace_sha256",
            "base_sha256",
            "require_artifacts",
        }
        assert body["kind"] == "sdk_job"
        uuid.UUID(body["job_id"], version=4)
        if body["job_id"] in worker.bodies:
            if worker.bodies[body["job_id"]] != body:
                return httpx.Response(409, json={"detail": "job_id already used"})
            # A repeat of an accepted job (its answer was lost) is idempotent.
            job = worker.jobs.get(body["job_id"])
            status = job["status"] if job else "running"
            return httpx.Response(
                202, json={"job_id": body["job_id"], "status": status}
            )
        if worker.running():
            return httpx.Response(409, json={"detail": "A job is running"})
        staged = worker.staged
        if staged is None or staged["sha256"] != body["workspace_sha256"]:
            return httpx.Response(409, json={"detail": "Workspace not staged"})
        base = None
        if body["base_sha256"] is not None:
            if worker.base is None or worker.base["sha256"] != body["base_sha256"]:
                return httpx.Response(409, json={"detail": "Base not uploaded"})
            base = worker.base["path"]
        # The next job wipes the previous job's directory; the base stays.
        for previous in worker.jobs.values():
            shutil.rmtree(previous["directory"], ignore_errors=True)
        worker.jobs.clear()
        worker.bodies[body["job_id"]] = body
        lease = self.leases[worker.lease_id]
        lease["status"] = "running"
        started = _now()
        directory = worker.directory / "jobs" / body["job_id"]
        directory.mkdir(parents=True)
        lease["jobs"].insert(
            0,
            {
                "job_id": body["job_id"],
                "status": "running",
                "started_at": started,
                "finished_at": None,
                "result": {},
                "error": "",
                "artifacts": {},
            },
        )
        if self.hold_jobs:
            worker.staged = None
            worker.jobs[body["job_id"]] = {
                "job_id": body["job_id"],
                "status": "running",
                "error": "",
                "directory": directory,
                "result": {},
            }
            return httpx.Response(
                202, json={"job_id": body["job_id"], "status": "running"}
            )
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "wayfinder_paths.jobs.sprite_runtime",
                "--bundle",
                str(staged["path"]),
                *(["--base", str(base)] if base is not None else []),
                "--root",
                str(directory / "repo"),
                "--output",
                str(directory / "result.json"),
                "--artifacts",
                str(directory / "artifacts.tar.gz"),
            ],
            capture_output=True,
            timeout=300,
        )
        worker.staged = None
        output_file, archive = directory / "result.json", directory / "artifacts.tar.gz"
        output = json.loads(output_file.read_text()) if output_file.exists() else {}
        artifacts = (
            {"sha256": sha256(archive), "size": archive.stat().st_size}
            if archive.exists()
            else {}
        )
        succeeded = proc.returncode == 0 and (
            bool(artifacts) or not body["require_artifacts"]
        )
        worker.jobs[body["job_id"]] = {
            "job_id": body["job_id"],
            "directory": directory,
            "result": {
                "output": output,
                "exit_code": proc.returncode,
                "timed_out": False,
                "stdout": proc.stdout.decode(errors="replace")[-32768:],
                "stderr": proc.stderr.decode(errors="replace")[-32768:],
                "logs_truncated": False,
                "artifacts": artifacts,
            },
        }
        self._finish(
            worker,
            body["job_id"],
            "succeeded" if succeeded else "failed",
            "" if succeeded else "Backtest computation failed",
        )
        return httpx.Response(202, json={"job_id": body["job_id"], "status": "running"})

    def _finish(self, worker: Worker, job_id: str, status: str, error: str) -> None:
        """The worker's job_finished report: Django keeps an anonymized copy."""
        job = worker.jobs[job_id]
        job.update(status=status, error=error)
        result = job["result"]
        anonymized = json.dumps(
            {
                key: result[key]
                for key in ("output", "exit_code", "timed_out")
                if key in result
            }
        ).replace(str(job["directory"] / "repo"), "<workspace>")
        lease = self.leases[worker.lease_id]
        entry = next(row for row in lease["jobs"] if row["job_id"] == job_id)
        entry.update(
            status=status,
            finished_at=_now(),
            result=json.loads(anonymized),
            error=error,
            artifacts=result.get("artifacts") or {},
        )
        lease.update(status="ready", last_activity_at=_now())
