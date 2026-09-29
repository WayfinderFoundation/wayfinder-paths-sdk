"""Run SDK workspaces on leased Sprites: Django books the lease, bundles move
directly between this node and the Sprite."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self, TypedDict, cast
from urllib.parse import urlsplit

import httpx
from loguru import logger

from wayfinder_paths.jobs.sprite_bundle import (
    MAX_ARCHIVE_BYTES,
    OPERATIONS,
    ArchiveMetadata,
    extract_archive,
    pack_job,
    sha256,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.monitor_state import atomic_write_json

CHUNK_BYTES = 8 * 1024 * 1024
ATTEMPTS = 5
DOWNLOAD_ATTEMPTS = 3
CALL_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
TRANSFER_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
# Django wipes inside the release request: URL close and Sprite destroy.
RELEASE_TIMEOUT = httpx.Timeout(240.0, connect=10.0)
# Choosing a lease never waits long on another process of this node (booking can take
# minutes), and releasing a stale lease while choosing is bounded too.
LOCK_WAIT_SECONDS = 30
RELEASE_DEADLINE_SECONDS = 300
# Fields every lease document has; anything else (an HTML page, an older backend)
# means the backend cannot serve this node, which then computes locally.
LEASE_FIELDS = frozenset(
    {
        "id",
        "provider",
        "status",
        "worker_url",
        "expires_at",
        "timeout_seconds",
        "transfer_timeout_seconds",
        "setup_timeout_seconds",
        "runtime",
    }
)
# The Sprite proxy answers these while it wakes the Sprite.
RETRY_STATUSES = frozenset({502, 503, 504})
# A booking whose answer was lost is repeated with the same token, which
# Django answers with the same lease; 503 is a refusal, not a lost answer.
BOOKING_RETRY_STATUSES = frozenset({502, 504})
# No subscription or credit (402/403), active/pool limits (409), daily cap
# (429), or disabled/unavailable (503): nothing was started.
CAPACITY_STATUSES = frozenset({402, 403, 409, 429, 503})
# The lease is closed: the worker closed itself (410), or Django closed the URL
# or leased the slot again, which answers 401/403 or a redirect.
CLOSED_STATUSES = frozenset({401, 403, 410})
# The worker cannot answer for the job: unreachable, closed, or the job was
# wiped (404). The backend's anonymized record answers instead.
WORKER_GONE_STATUSES = frozenset({404, *CLOSED_STATUSES, *RETRY_STATUSES})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timed_out", "cancelled"})
# Refusal reasons meaning the backend has offloading switched off (a setting, not a
# shortage). The node computes locally and asks again only after the recheck interval.
OFFLOAD_OFF_REASONS = frozenset({"backtests_disabled", "profile_disabled"})
OFFLOAD_OFF_RECHECK_SECONDS = 600
# In the lease directory, which never enters a bundle; not *.json, which holds leases.
OFFLOAD_OFF_FILE = "offload-off.state"
WORKSPACE_CAPABILITY = "sdk-workspace-v1"
WAIT_GRACE_SECONDS = 120
# A cancel issued at a wait deadline gets this long, whatever the worker does.
CANCEL_BUDGET_SECONDS = 10
PROVISIONING_POLL_SECONDS = 2
PROVISIONING_TIMEOUT_SECONDS = 600
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# Why a lease is booked or a job runs (e.g. "evolution:screen_phase"). Django keeps it in
# the lease audit log, so it is a short label and never user data.
PURPOSE = re.compile(r"[a-z][a-z0-9_.:-]{0,63}")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class LeaseUnavailable(RuntimeError):
    """No usable lease: refused, unreachable, never ready, or closed twice.
    Nothing was started on a Sprite."""


class OffloadOff(LeaseUnavailable):
    """The backend has offloading switched off: compute locally, whatever the
    configured fallback."""


class ArtifactMismatch(ValueError):
    pass


class LeaseRecord(TypedDict):
    id: str
    backend: str
    app_name: str
    preset: str
    # Where the lease's machine runs (the runner profile's provider, e.g. "sprites").
    provider: str
    worker_url: str
    token: str
    expires_at: str
    timeout_seconds: int
    transfer_timeout_seconds: int
    # Worker setup (the SDK runtime install on a fresh Sprite) the first job waits for.
    setup_timeout_seconds: int
    # sha256 of the base archive the worker holds, once its upload completed.
    base: str | None
    # Claimed by a submission until its results are collected or it is
    # cancelled; the worker wipes a job's directory when the next one starts.
    job: str | None


@dataclass(frozen=True, slots=True)
class SpriteRoutes:
    """Owner-authenticated Django lease routes. Without an app name, Django books by the
    API key alone: a Shell's own key under its Shell, any other key a local lease."""

    app_name: str = ""

    @property
    def leases(self) -> str:
        if self.app_name:
            return f"/api/v1/opencode/instances/{self.app_name}/sprite-backtests/"
        return "/api/v1/opencode/sprite-leases/"

    def lease(self, lease_id: str) -> str:
        return f"{self.leases}{checked_id(lease_id)}/"


@dataclass(slots=True)
class _Scope:
    lease: str | None
    job: str | None

    @property
    def label(self) -> str:
        return f"[lease {self.lease or 'none'}, job {self.job or 'none'}]"


class LeaseStore:
    """Node-side lease records, the only place a lease token is kept."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _ready(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        return self.directory

    def _path(self, lease_id: str) -> Path:
        return self._ready() / f"{checked_id(lease_id)}.json"

    @contextmanager
    def locked(self, *, timeout: float | None = None) -> Iterator[None]:
        """Serialize choosing, claiming and settling leases across processes. With a
        timeout, a lock held longer raises LeaseUnavailable instead of waiting."""
        descriptor = os.open(self._ready() / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            flags = fcntl.LOCK_EX if timeout is None else fcntl.LOCK_EX | fcntl.LOCK_NB
            deadline = time.monotonic() + (timeout or 0)
            while True:
                try:
                    fcntl.flock(descriptor, flags)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LeaseUnavailable(
                            "Another process of this node is choosing a lease"
                        ) from None
                    time.sleep(0.2)
            yield
        finally:
            os.close(descriptor)

    def find(self, lease_id: str) -> LeaseRecord | None:
        try:
            return _record(self._path(lease_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def all(self) -> list[LeaseRecord]:
        records = [
            _record(path.read_text(encoding="utf-8"))
            for path in sorted(self._ready().glob("*.json"))
        ]
        return [record for record in records if record is not None]

    def save(self, record: LeaseRecord) -> LeaseRecord:
        # mkstemp creates the file 0600, so the token is never readable by
        # others, not even before a chmod.
        atomic_write_json(self._path(record["id"]), record)
        return record

    def delete(self, lease_id: str) -> None:
        self._path(lease_id).unlink(missing_ok=True)

    def mark_offload_off(self, backend: str, reason: str, until: float) -> None:
        atomic_write_json(
            self._ready() / OFFLOAD_OFF_FILE,
            {"backend": backend, "reason": reason, "until": until},
        )

    def clear_offload_off(self) -> None:
        (self.directory / OFFLOAD_OFF_FILE).unlink(missing_ok=True)


class SpriteBacktestsClient:
    """Book, reuse and release leases; injected HTTP clients stay caller-owned.

    Only the lease token's SHA-256 reaches Django. The token itself stays in
    ``lease_dir`` and authenticates this node to its Sprite. Errors leaving the
    client name their lease and job and never contain the token.
    """

    def __init__(
        self,
        backend: str,
        app_name: str | None,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        lease_dir: Path | None = None,
        sdk_commit: str | None = None,
    ) -> None:
        self._owns_http = client is None
        # The public SDK commit Django installs on a fresh Sprite for sdk_runtime presets
        # (normally this node's own); a submission's expected commit takes precedence.
        self.sdk_commit = sdk_commit
        self.backend = backend.rstrip("/")
        self.app_name = app_name or ""
        self.http = (
            client
            if client is not None
            else httpx.Client(
                base_url=self.backend, timeout=CALL_TIMEOUT, follow_redirects=False
            )
        )
        self.owner_headers = {"X-API-Key": api_key}
        self.routes = SpriteRoutes(self.app_name)
        self.leases = LeaseStore(
            lease_dir
            if lease_dir is not None
            else Path.home() / ".wayfinder" / "sprite-leases"
        )
        self._sleep = sleep if sleep is not None else time.sleep
        self._clock = clock if clock is not None else time.time

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
        purpose: str = "",
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "workspace.tar.gz"
            pack_job(
                store,
                job_id,
                archive,
                op=op,
                options=options,
                extra_paths=extra_paths,
                expected_sdk_commit=expected_sdk_commit,
            )
            return self.submit_archive(
                archive,
                preset=preset,
                expected_sdk_commit=expected_sdk_commit,
                purpose=purpose or f"operation:{op}",
            )

    def submit_archive(
        self,
        archive: Path,
        *,
        base: Path | None = None,
        require_artifacts: bool = True,
        preset: str = "jobs-v1",
        expected_sdk_commit: str | None = None,
        purpose: str = "",
    ) -> dict[str, Any]:
        """Run a prebuilt job or phase archive, over ``base`` when given.

        A live lease of this node is reused; its base is uploaded once and
        each job uploads only its own archive. A lease found closed while
        submitting is replaced once. ``purpose`` labels the booking and the
        job in Django's lease audit log.
        """
        if purpose and not PURPOSE.fullmatch(purpose):
            raise ValueError(f"purpose must match {PURPOSE.pattern}: {purpose!r}")
        job_id = str(uuid.uuid4())
        scope = _Scope(None, job_id)
        with _naming(scope):
            base_sha = sha256(base) if base is not None else None
            for attempt in range(2):
                record, lease = self._acquire(
                    preset, base_sha, job_id, expected_sdk_commit, purpose
                )
                scope.lease = record["id"]
                try:
                    accepted = self._send(
                        record,
                        job_id,
                        archive,
                        base,
                        base_sha,
                        require_artifacts,
                        purpose,
                    )
                except Exception as exc:
                    # Frees the one-lease slot and stops anything half-started.
                    with suppress(httpx.HTTPError), self.leases.locked():
                        self._release(record["id"], reason=f"submission failed: {exc}")
                    if _closed(exc):
                        if not attempt:
                            continue
                        raise LeaseUnavailable(
                            f"Lease {record['id']} closed while submitting, again "
                            "after booking a new lease"
                        ) from exc
                    if isinstance(
                        exc, (httpx.TransportError, httpx.HTTPStatusError)
                    ) and (_worker_gone(exc)):
                        # Nothing ran: the job can still run locally.
                        raise LeaseUnavailable(
                            f"Lease {record['id']} worker unreachable while submitting: {exc}"
                        ) from exc
                    raise
                except BaseException:
                    # Cancelled by the caller: keep the lease for reuse.
                    self._abandon(record, job_id)
                    raise
                return {
                    **_run(
                        record["id"],
                        job_id,
                        accepted.get("status", "running"),
                        {},
                        {},
                        "",
                    ),
                    "expires_at": record["expires_at"],
                    "runtime": lease.get("runtime", {}),
                    "destination": _destination(record),
                }
        raise AssertionError("Submission loop must return or raise")

    def status(self, run_id: str) -> dict[str, Any]:
        lease_id, job_id = split_run_id(run_id)
        with _naming(_Scope(lease_id, job_id)):
            return self._status(lease_id, job_id)

    def wait(
        self,
        run_id: str,
        *,
        poll_interval: float = 5.0,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Poll until the run is terminal or the deadline passes.

        The default deadline is the lease's job timeout plus a transfer window,
        its setup allowance (the first job on a fresh Sprite waits for the runtime
        install) and a grace period, never past the lease's expiry plus the grace.
        At the deadline the job is cancelled and reported ``timed_out``.
        """
        lease_id, job_id = split_run_id(run_id)
        with _naming(_Scope(lease_id, job_id)):
            started = self._clock()
            limits: Mapping[str, Any] = self.leases.find(lease_id) or self._lease(
                lease_id
            )
            budget = (
                timeout
                if timeout is not None
                else limits["timeout_seconds"]
                + limits["transfer_timeout_seconds"]
                # Records written before this field existed had no setup to wait for.
                + limits.get("setup_timeout_seconds", 0)
                + WAIT_GRACE_SECONDS
            )
            deadline = min(
                started + budget, _epoch(limits["expires_at"]) + WAIT_GRACE_SECONDS
            )
            status = _run(lease_id, job_id, "running", {}, {}, "")
            while True:
                # Retries inside one poll stop at the deadline, so an unreachable
                # worker cannot stretch the wait past it.
                try:
                    status = self._status(lease_id, job_id, deadline=deadline)
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    # Offline (a laptop sleep, a network drop) or a backend outage: keep
                    # polling until the deadline instead of abandoning a running job.
                    if (
                        isinstance(exc, httpx.HTTPStatusError)
                        and exc.response.status_code not in RETRY_STATUSES
                    ):
                        raise
                else:
                    if status["status"] in TERMINAL_STATUSES:
                        return status
                if self._clock() >= deadline:
                    self._cancel(
                        lease_id,
                        job_id,
                        running=True,
                        deadline=self._clock() + CANCEL_BUDGET_SECONDS,
                    )
                    waited = self._clock() - started
                    return {
                        **status,
                        "status": "timed_out",
                        "artifacts": {},
                        "error": (
                            f"The node stopped waiting for job {job_id} on lease "
                            f"{lease_id} after {waited:.0f} s and cancelled it"
                        ),
                    }
                self._sleep(min(poll_interval, max(0.0, deadline - self._clock())))

    def cancel(self, run_id: str) -> None:
        """Cancel the job and keep its lease for the next job."""
        lease_id, job_id = split_run_id(run_id)
        with _naming(_Scope(lease_id, job_id)):
            self._cancel(lease_id, job_id)

    def collect(self, run_id: str, destination: Path) -> dict[str, Any]:
        lease_id, job_id = split_run_id(run_id)
        with _naming(_Scope(lease_id, job_id)):
            run = self._status(lease_id, job_id)
            metadata = run["artifacts"]
            if not metadata:
                raise ValueError(
                    f"No artifacts available ({run['status']}): {run['error']}"
                )
            if destination.exists():
                raise FileExistsError(
                    "Collect into a new directory to preserve existing job state"
                )
            record = self.leases.find(lease_id)
            if record is None:
                raise ValueError(
                    f"No record of lease {lease_id} in {self.leases.directory}; "
                    "only the node that booked a lease can download its artifacts"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
                archive = Path(directory) / "artifacts.tar.gz"
                self._retrying(
                    lambda: self._download(record, job_id, archive, metadata),
                    attempts=DOWNLOAD_ATTEMPTS,
                    also=(ArtifactMismatch,),
                )
                staged = Path(directory) / "workspace"
                extract_archive(archive, staged)
                staged.rename(destination)
            self._settle(lease_id, job_id)
            return run

    def release(self, lease_id: str) -> dict[str, Any]:
        """Close a lease now; Django wipes its Sprite. Idempotent."""
        with _naming(_Scope(lease_id, None)), self.leases.locked():
            return self._release(lease_id, reason="released by its owner")

    def release_idle(self, *, reason: str) -> list[str]:
        """Close this node's leases no submission is using, for example when a
        campaign that kept one warm between its phases ends."""
        released = []
        with self.leases.locked(timeout=LOCK_WAIT_SECONDS):
            for record in self._own_records():
                if record["job"] is not None:
                    continue
                with suppress(httpx.HTTPError):
                    self._release(
                        record["id"],
                        reason=reason,
                        deadline=self._clock() + RELEASE_DEADLINE_SECONDS,
                    )
                    released.append(record["id"])
        return released

    def _acquire(
        self,
        preset: str,
        base_sha: str | None,
        job_id: str,
        expected_sdk_commit: str | None,
        purpose: str,
    ) -> tuple[LeaseRecord, dict[str, Any]]:
        commit = expected_sdk_commit or self.sdk_commit
        off = offload_off_reason(self.leases.directory, self.backend, self._clock())
        if off is not None:
            raise OffloadOff(off)
        with self.leases.locked(timeout=LOCK_WAIT_SECONDS):
            draining = False
            try:
                for record in self._own_records():
                    try:
                        lease = _complete(self._lease(record["id"]))
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 404:
                            raise
                        self.leases.delete(record["id"])
                        continue
                    if lease["status"] == "closed":
                        self.leases.delete(record["id"])
                        continue
                    if record["job"] is not None or lease["status"] != "ready":
                        continue  # Another submission of this node still needs it.
                    draining = draining or bool(lease.get("draining"))
                    problem = self._reuse_problem(
                        lease, record, preset, base_sha, commit
                    )
                    if problem is None:
                        claimed = self.leases.save(
                            {
                                **record,
                                "job": job_id,
                                "expires_at": lease["expires_at"],
                                "timeout_seconds": lease["timeout_seconds"],
                                "transfer_timeout_seconds": lease[
                                    "transfer_timeout_seconds"
                                ],
                                "setup_timeout_seconds": lease["setup_timeout_seconds"],
                            }
                        )
                        logger.info(
                            "Reusing {} lease {} at {} for {}",
                            claimed["provider"],
                            claimed["id"],
                            urlsplit(claimed["worker_url"]).hostname,
                            purpose or "an unlabelled job",
                        )
                        return claimed, lease
                    # One open lease per owner: free the slot for a new booking.
                    self._release(
                        record["id"],
                        reason=f"cannot be reused: {problem}",
                        deadline=self._clock() + RELEASE_DEADLINE_SECONDS,
                    )
                if draining:
                    # Bookings are off while the backend drains its leases.
                    raise self._offload_off("backtests_disabled")
                return self._book(preset, job_id, commit, purpose)
            except httpx.TransportError as exc:
                raise LeaseUnavailable(f"Sprites backend unreachable: {exc}") from exc
            except httpx.HTTPStatusError as exc:
                reason = _refusal_reason(exc.response)
                if reason in OFFLOAD_OFF_REASONS:
                    raise self._offload_off(reason) from exc
                code = exc.response.status_code
                # Refusals, and a backend or gateway failing after the retries (5xx,
                # redirects), are unavailable; other 4xx are configuration errors.
                if (
                    code not in CAPACITY_STATUSES
                    and code < 500
                    and not 300 <= code < 400
                ):
                    raise
                outcome = (
                    "refused a worker" if code in CAPACITY_STATUSES else "unavailable"
                )
                raise LeaseUnavailable(
                    f"Sprites {outcome} (HTTP {code}): {_detail(exc.response)}"
                ) from exc

    def _reuse_problem(
        self,
        lease: Mapping[str, Any],
        record: LeaseRecord,
        preset: str,
        base_sha: str | None,
        commit: str | None,
    ) -> str | None:
        """Why a lease cannot run this job, or None. A lease runs this node's jobs only
        while it runs this node's SDK commit (a node whose commit is unknown never
        reuses an SDK lease) and its setup worked."""
        remaining = _epoch(lease["expires_at"]) - self._clock()
        runtime = lease.get("runtime") or {}
        if record["preset"] != preset:
            return f"booked for preset {record['preset']}"
        if base_sha is not None and record["base"] not in {None, base_sha}:
            return "holds another base"
        if remaining < lease["timeout_seconds"] + 2 * lease["transfer_timeout_seconds"]:
            return f"expires in {max(remaining, 0):.0f} s"
        problem = _runtime_problem(lease, commit)
        if problem is not None:
            return problem
        if commit is None and runtime.get("sdk_commit"):
            return "this node's SDK commit is unknown"
        if (lease.get("setup") or {}).get("state") == "failed":
            return "its SDK install failed"
        if lease.get("draining"):
            return "the backend is draining leases: offloading is switched off"
        return None

    def _offload_off(self, reason: str) -> OffloadOff:
        """Remember that offloading is off, so this node computes locally without
        asking again until the recheck interval passes."""
        self.leases.mark_offload_off(
            self.backend, reason, self._clock() + OFFLOAD_OFF_RECHECK_SECONDS
        )
        logger.warning(
            "Offloading is switched off on {} ({}); computing locally, asking again "
            "in {} s",
            self.backend,
            reason,
            OFFLOAD_OFF_RECHECK_SECONDS,
        )
        return OffloadOff(f"Offloading is switched off on {self.backend} ({reason})")

    def _book(
        self, preset: str, job_id: str, commit: str | None, purpose: str
    ) -> tuple[LeaseRecord, dict[str, Any]]:
        token, lease = self._request_booking(preset, commit, purpose)
        if lease["status"] == "closed":
            # The retried booking found this token's lease already closed.
            token, lease = self._request_booking(preset, commit, purpose)
        lease_id = checked_id(lease["id"])
        logger.info(
            "Booked {} lease {} (preset {}, SDK {}) for {}; waiting for it to start",
            lease["provider"],
            lease_id,
            preset,
            commit or "unpinned",
            purpose or "an unlabelled job",
        )
        try:
            deadline = self._clock() + PROVISIONING_TIMEOUT_SECONDS
            while lease["status"] == "provisioning" and self._clock() < deadline:
                self._sleep(PROVISIONING_POLL_SECONDS)
                lease = self._lease(lease_id)
            if lease["status"] != "ready":
                reason = lease.get("closed_reason") or lease.get("error") or "no reason"
                raise LeaseUnavailable(
                    f"Lease {lease_id} is {lease['status']} instead of ready ({reason})"
                )
            record = self.leases.save(
                {
                    "id": lease_id,
                    "backend": self.backend,
                    "app_name": self.app_name,
                    "preset": preset,
                    "provider": lease["provider"],
                    "worker_url": lease["worker_url"],
                    "token": token,
                    "expires_at": lease["expires_at"],
                    "timeout_seconds": lease["timeout_seconds"],
                    "transfer_timeout_seconds": lease["transfer_timeout_seconds"],
                    "setup_timeout_seconds": lease["setup_timeout_seconds"],
                    "base": None,
                    "job": job_id,
                }
            )
            problem = _worker_url_problem(lease["worker_url"]) or _runtime_problem(
                lease, commit
            )
            if problem:
                raise ValueError(problem)
        except BaseException as exc:
            with suppress(httpx.HTTPError):
                self._release(lease_id, reason=f"booking failed: {exc!r}")
            raise
        self.leases.clear_offload_off()
        logger.info(
            "{} lease {} is ready at {}",
            record["provider"],
            lease_id,
            urlsplit(record["worker_url"]).hostname,
        )
        return record, lease

    def _request_booking(
        self, preset: str, sdk_commit: str | None, purpose: str
    ) -> tuple[str, dict[str, Any]]:
        token = secrets.token_urlsafe(32)
        body = {"preset_key": preset, "token_sha256": token_sha256(token)}
        if sdk_commit is not None:
            body["sdk_commit"] = sdk_commit
        if purpose:
            body["purpose"] = purpose
        response = self._retrying(
            lambda: self._backend("POST", self.routes.leases, json=body),
            statuses=BOOKING_RETRY_STATUSES,
        )
        return token, _complete(_lease_document(response))

    def _backend(
        self,
        method: str,
        path: str,
        *,
        timeout: httpx.Timeout = CALL_TIMEOUT,
        deadline: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        response = self.http.request(
            method,
            path,
            headers=self.owner_headers,
            timeout=self._bounded(timeout, deadline),
            **kwargs,
        )
        response.raise_for_status()
        return response

    def _bounded(self, timeout: httpx.Timeout, deadline: float | None) -> httpx.Timeout:
        """A call made under a deadline never outlasts it."""
        if deadline is None:
            return timeout
        remaining = max(1.0, deadline - self._clock())
        return httpx.Timeout(
            min(timeout.read or remaining, remaining),
            connect=min(timeout.connect or remaining, remaining),
        )

    def _lease(self, lease_id: str, *, deadline: float | None = None) -> dict[str, Any]:
        path = self.routes.lease(lease_id)
        return _lease_document(
            self._retrying(
                lambda: self._backend("GET", path, deadline=deadline), deadline=deadline
            )
        )

    def _release(
        self, lease_id: str, *, reason: str, deadline: float | None = None
    ) -> dict[str, Any]:
        logger.info("Releasing lease {}: {}", lease_id, reason)
        path = self.routes.lease(lease_id)
        try:
            lease = self._retrying(
                lambda: self._backend(
                    "DELETE", path, timeout=RELEASE_TIMEOUT, deadline=deadline
                ),
                deadline=deadline,
            ).json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            lease = {}
        self.leases.delete(lease_id)
        return lease

    def _own_records(self) -> list[LeaseRecord]:
        return [
            record
            for record in self.leases.all()
            if record["backend"] == self.backend and record["app_name"] == self.app_name
        ]

    def _send(
        self,
        record: LeaseRecord,
        job_id: str,
        archive: Path,
        base: Path | None,
        base_sha: str | None,
        require_artifacts: bool,
        purpose: str,
    ) -> dict[str, Any]:
        if base is not None and record["base"] != base_sha:
            uploaded = self._upload(record, "base", base)
            record = self._record_base(record["id"], uploaded["sha256"])
        workspace = self._upload(record, "workspace", archive)
        payload: dict[str, Any] = {
            "job_id": job_id,
            "kind": "sdk_job",
            "workspace_sha256": workspace["sha256"],
            "base_sha256": base_sha,
            "require_artifacts": require_artifacts,
        }
        if purpose:
            payload["purpose"] = purpose
        return self._worker(record, "POST", "/jobs", payload=payload).json()

    def _abandon(self, record: LeaseRecord, job_id: str) -> None:
        with suppress(httpx.HTTPError):
            self.http.request(
                "DELETE",
                _worker_url(record, f"/jobs/{job_id}"),
                headers=_bearer(record),
                timeout=CALL_TIMEOUT,
            )
        self._settle(record["id"], job_id)

    def _record_base(self, lease_id: str, base_sha: str) -> LeaseRecord:
        with self.leases.locked():
            record = self.leases.find(lease_id)
            if record is None:
                raise FileNotFoundError(f"Lease record {lease_id} disappeared")
            return self.leases.save({**record, "base": base_sha})

    def _settle(self, lease_id: str, job_id: str) -> None:
        with self.leases.locked():
            record = self.leases.find(lease_id)
            if record is not None and record["job"] == job_id:
                self.leases.save({**record, "job": None})

    def _status(
        self, lease_id: str, job_id: str, *, deadline: float | None = None
    ) -> dict[str, Any]:
        record = self.leases.find(lease_id)
        if record is None:
            return self._recorded(lease_id, job_id, deadline=deadline)
        try:
            job = self._worker(
                record, "GET", f"/jobs/{job_id}", deadline=deadline
            ).json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if not _worker_gone(exc):
                raise
            return self._recorded(lease_id, job_id, deadline=deadline)
        result = job.get("result") or {}
        run = _run(
            lease_id,
            job_id,
            job["status"],
            result,
            result.get("artifacts") or {},
            job.get("error") or "",
        )
        if run["status"] in TERMINAL_STATUSES and not run["artifacts"]:
            self._settle(lease_id, job_id)
        return run

    def _recorded(
        self, lease_id: str, job_id: str, *, deadline: float | None = None
    ) -> dict[str, Any]:
        """The backend's anonymized record, when the worker cannot answer."""
        lease = self._lease(lease_id, deadline=deadline)
        job: dict[str, Any] = next(
            (entry for entry in lease.get("jobs", []) if entry["job_id"] == job_id),
            {},
        )
        status, error = job.get("status", "running"), job.get("error") or ""
        recorded = job.get("artifacts") or {}
        closed = lease["status"] == "closed"
        # A closed lease's Sprite is wiped, and with it any uncollected artifacts.
        if closed and (status not in TERMINAL_STATUSES or recorded):
            reason = lease.get("closed_reason") or "unknown"
            error = (
                f"Lease closed ({reason}) before the job finished"
                if status not in TERMINAL_STATUSES
                else f"Lease closed ({reason}) before the artifacts of this "
                f"{status} job were collected"
            )
            status = "failed"
        if closed:
            with self.leases.locked():
                self.leases.delete(lease_id)
        elif status in TERMINAL_STATUSES and not recorded:
            self._settle(lease_id, job_id)
        return {
            **_run(
                lease_id,
                job_id,
                status,
                job.get("result") or {},
                {} if closed else recorded,
                error,
            ),
            "source": "backend",
        }

    def _cancel(
        self,
        lease_id: str,
        job_id: str,
        *,
        running: bool = False,
        deadline: float | None = None,
    ) -> None:
        if (
            not running
            and self._status(lease_id, job_id, deadline=deadline)["status"]
            in TERMINAL_STATUSES
        ):
            return
        record = self.leases.find(lease_id)
        if record is not None:
            try:
                self._worker(record, "DELETE", f"/jobs/{job_id}", deadline=deadline)
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if not _worker_gone(exc):
                    raise
            else:
                self._settle(lease_id, job_id)
                return
        # Only closing the lease stops a job on a worker that cannot be reached.
        # Under a deadline, Django finishes the wipe even if this call times out,
        # and reconciliation retries it.
        with self.leases.locked(), suppress(httpx.TransportError):
            self._release(
                lease_id,
                reason=f"its worker is unreachable to cancel job {job_id}",
                deadline=deadline,
            )

    def _retrying[T](
        self,
        call: Callable[[], T],
        *,
        attempts: int = ATTEMPTS,
        statuses: frozenset[int] = RETRY_STATUSES,
        also: tuple[type[Exception], ...] = (),
        deadline: float | None = None,
    ) -> T:
        retried: tuple[type[Exception], ...] = (
            httpx.TransportError,
            httpx.HTTPStatusError,
            *also,
        )
        for attempt in range(attempts):
            try:
                return call()
            except retried as exc:
                transient = (
                    not isinstance(exc, httpx.HTTPStatusError)
                    or exc.response.status_code in statuses
                )
                pause = min(2**attempt, 30)
                if (
                    not transient
                    or attempt == attempts - 1
                    or (deadline is not None and self._clock() + pause >= deadline)
                ):
                    raise
                self._sleep(pause)
        raise AssertionError("Retry loop must return or raise")

    def _worker(
        self,
        record: LeaseRecord,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        payload: Any = None,
        timeout: httpx.Timeout = CALL_TIMEOUT,
        deadline: float | None = None,
    ) -> httpx.Response:
        def send() -> httpx.Response:
            response = self.http.request(
                method,
                _worker_url(record, path),
                headers={**(headers or {}), **_bearer(record)},
                content=content,
                json=payload,
                timeout=self._bounded(timeout, deadline),
            )
            response.raise_for_status()
            return response

        return self._retrying(send, deadline=deadline)

    def _upload(
        self, record: LeaseRecord, resource: str, archive: Path
    ) -> ArchiveMetadata:
        packed: ArchiveMetadata = {
            "sha256": sha256(archive),
            "size": archive.stat().st_size,
        }
        if not packed["size"]:
            raise ValueError(f"Cannot upload an empty {resource} archive")
        try:
            return self._upload_chunks(record, resource, archive, packed)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:
                raise
        # The worker lost track of this upload, or already holds this base:
        # re-read its state and start the upload over, once.
        state = self._worker(record, "GET", "/status").json()
        if (
            resource == "base"
            and (state.get("base") or {}).get("sha256") == (packed["sha256"])
        ):
            return packed
        return self._upload_chunks(record, resource, archive, packed)

    def _upload_chunks(
        self,
        record: LeaseRecord,
        resource: str,
        archive: Path,
        packed: ArchiveMetadata,
    ) -> ArchiveMetadata:
        last = (packed["size"] - 1) // CHUNK_BYTES
        with archive.open("rb") as stream:
            for part, chunk in enumerate(iter(lambda: stream.read(CHUNK_BYTES), b"")):
                # A chunk whose answer was lost is sent again; repeats are
                # idempotent on the worker.
                answer = self._worker(
                    record,
                    "PUT",
                    f"/{resource}",
                    headers={
                        "Content-Type": "application/gzip",
                        "X-Content-SHA256": hashlib.sha256(chunk).hexdigest(),
                        "X-Bundle-SHA256": packed["sha256"],
                        "X-Bundle-Size": str(packed["size"]),
                        "X-Bundle-Part": str(part),
                    },
                    content=chunk,
                    timeout=TRANSFER_TIMEOUT,
                ).json()
                expected: dict[str, Any] = (
                    dict(packed) if part == last else {"part": part, "accepted": True}
                )
                if {key: answer.get(key) for key in expected} != expected:
                    raise ValueError(
                        f"The Sprite did not confirm {resource} part {part}"
                    )
        return packed

    def _download(
        self,
        record: LeaseRecord,
        job_id: str,
        archive: Path,
        metadata: Mapping[str, Any],
    ) -> None:
        size, digest = 0, hashlib.sha256()
        with (
            self.http.stream(
                "GET",
                _worker_url(record, f"/jobs/{job_id}/artifacts"),
                headers=_bearer(record),
                timeout=TRANSFER_TIMEOUT,
            ) as response,
            archive.open("wb") as stream,
        ):
            response.raise_for_status()
            declared = response.headers.get("X-Content-SHA256")
            for chunk in response.iter_bytes(1024 * 1024):
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise ValueError("Artifact download exceeds 512 MiB")
                digest.update(chunk)
                stream.write(chunk)
        if (size, digest.hexdigest(), declared) != (
            metadata["size"],
            metadata["sha256"],
            metadata["sha256"],
        ):
            raise ArtifactMismatch("Artifact checksum or size mismatch")


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def checked_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"Invalid Sprite lease or job id: {value!r}")
    return value


def split_run_id(run_id: str) -> tuple[str, str]:
    lease_id, separator, job_id = run_id.partition(":")
    if not separator:
        raise ValueError(f"Sprite run ids are '<lease id>:<job id>': {run_id!r}")
    return checked_id(lease_id), checked_id(job_id)


@contextmanager
def _naming(scope: _Scope) -> Iterator[None]:
    """Name the lease and job in every error leaving the client. Messages
    carry URLs and ids only; the token lives in headers, which httpx masks."""
    try:
        yield
    except (httpx.HTTPError, ValueError, OSError, LeaseUnavailable) as exc:
        if scope.label in str(exc):
            raise
        raise _labelled(exc, f"{exc} {scope.label}") from exc


def _labelled(exc: Exception, message: str) -> Exception:
    if isinstance(exc, httpx.HTTPStatusError):
        return httpx.HTTPStatusError(
            message, request=exc.request, response=exc.response
        )
    if isinstance(exc, httpx.RequestError):
        return type(exc)(message, request=exc.request)
    if isinstance(exc, ValueError):
        return ValueError(message)
    return type(exc)(message)


def _run(
    lease_id: str,
    job_id: str,
    status: str,
    result: Mapping[str, Any],
    artifacts: Mapping[str, Any],
    error: str,
) -> dict[str, Any]:
    return {
        "id": f"{lease_id}:{job_id}",
        "lease_id": lease_id,
        "job_id": job_id,
        "status": status,
        "result": dict(result),
        "artifacts": dict(artifacts),
        "error": error,
    }


def _destination(record: LeaseRecord) -> dict[str, Any]:
    """Where a submission runs, for run receipts and job journals."""
    return {
        "provider": record["provider"],
        "lease_id": record["id"],
        "preset": record["preset"],
        "worker_host": urlsplit(record["worker_url"]).hostname,
        "backend": record["backend"],
    }


def _worker_url(record: LeaseRecord, path: str) -> str:
    return record["worker_url"].rstrip("/") + path


def _bearer(record: LeaseRecord) -> dict[str, str]:
    return {"Authorization": f"Bearer {record['token']}"}


def _worker_gone(exc: httpx.TransportError | httpx.HTTPStatusError) -> bool:
    return isinstance(exc, httpx.TransportError) or (
        exc.response.status_code in WORKER_GONE_STATUSES or exc.response.is_redirect
    )


def _closed(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code in CLOSED_STATUSES or exc.response.is_redirect
    )


def _epoch(timestamp: str) -> float:
    return datetime.fromisoformat(timestamp).timestamp()


def _record(text: str) -> LeaseRecord | None:
    """A lease record, or None for a corrupt file (which must not break every submission)."""
    try:
        record = json.loads(text)
    except ValueError:
        return None
    return cast(LeaseRecord, record) if isinstance(record, dict) else None


def _lease_document(response: httpx.Response) -> dict[str, Any]:
    try:
        lease = response.json()
    except ValueError as exc:
        raise LeaseUnavailable(
            "Sprites backend answered without a lease document"
        ) from exc
    if not isinstance(lease, dict):
        raise LeaseUnavailable("Sprites backend answered an unexpected lease document")
    return lease


def _complete(lease: dict[str, Any]) -> dict[str, Any]:
    if not LEASE_FIELDS <= lease.keys():
        raise LeaseUnavailable("Sprites backend answered an incomplete lease document")
    return lease


def offload_off_reason(lease_dir: Path, backend: str, now: float) -> str | None:
    """Why ``backend`` has offloading switched off, while this node's record of that
    is fresh; None when offloading may be on."""
    try:
        state = json.loads((lease_dir / OFFLOAD_OFF_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(state, dict)
        or state.get("backend") != backend.rstrip("/")
        or not isinstance(state.get("until"), (int, float))
        or state["until"] <= now
    ):
        return None
    return f"Offloading is switched off on {state['backend']} ({state.get('reason')})"


def _refusal_reason(response: httpx.Response) -> str | None:
    try:
        reason = response.json().get("reason")
    except (ValueError, AttributeError):
        return None
    return reason if isinstance(reason, str) else None


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", ""))[:500]
    except (ValueError, AttributeError):
        return ""


def _runtime_problem(
    lease: Mapping[str, Any], expected_sdk_commit: str | None
) -> str | None:
    runtime = lease.get("runtime") or {}
    if WORKSPACE_CAPABILITY not in runtime.get("capabilities", []):
        return "Preset needs a checkpoint with sdk-workspace-v1 support"
    if expected_sdk_commit and runtime.get("sdk_commit") != expected_sdk_commit:
        return "Preset SDK commit does not match requested version"
    return None


def _worker_url_problem(url: str) -> str | None:
    # The lease token is a bearer credential: only TLS, or loopback in development.
    parts = urlsplit(url)
    secure = parts.scheme == "https" or (
        parts.scheme == "http" and parts.hostname in _LOOPBACK
    )
    if not secure or not parts.hostname or parts.username or parts.password:
        return f"Refusing to send the lease token to worker URL {url!r}"
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True)
    parser.add_argument(
        "--app-name",
        help="Book under this Shell (default: decided by the API key, as on a Shell)",
    )
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
    parser.add_argument(
        "--lease-dir",
        type=Path,
        help="Node-side lease records (default ~/.wayfinder/sprite-leases)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--submit-only", action="store_true")
    mode.add_argument(
        "--collect",
        metavar="RUN_ID",
        help="Wait for and collect a run ('<lease id>:<job id>') of a stored lease",
    )
    mode.add_argument(
        "--release", metavar="LEASE_ID", help="Close a lease and wipe its Sprite now"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not (args.collect or args.release or args.job_id):
        parser.error("--job-id is required for submission")
    if not (args.submit_only or args.release or args.output):
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
    with SpriteBacktestsClient(
        args.backend, args.app_name, api_key, lease_dir=args.lease_dir
    ) as client:
        if args.release:
            print(json.dumps(client.release(args.release), indent=2))
            return
        if args.collect:
            run_id = args.collect
        else:
            submitted = client.submit(
                JobStore(repo_root=args.repo),
                args.job_id,
                preset=args.preset,
                op=args.op,
                options=options,
                extra_paths=args.extra_path,
                expected_sdk_commit=args.sdk_commit,
            )
            run_id = submitted["id"]
            print(json.dumps(submitted), flush=True)
            if args.submit_only:
                return
        status = client.wait(run_id)
        if status.get("artifacts"):
            client.collect(run_id, args.output)
        print(json.dumps(status, indent=2))
        if status["status"] != "succeeded":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
