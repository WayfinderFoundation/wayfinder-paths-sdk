from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from wayfinder_paths.jobs import sprite_client
from wayfinder_paths.jobs.compute_phase import phase_name
from wayfinder_paths.jobs.sprite_bundle import pack_base, pack_inputs
from wayfinder_paths.jobs.sprite_client import (
    ATTEMPTS,
    CANCEL_BUDGET_SECONDS,
    DOWNLOAD_ATTEMPTS,
    PROVISIONING_POLL_SECONDS,
    PROVISIONING_TIMEOUT_SECONDS,
    LeaseRecord,
    LeaseUnavailable,
    SpriteBacktestsClient,
    main,
    split_run_id,
)
from wayfinder_paths.tests import compute_phase_fixtures as phases
from wayfinder_paths.tests.sprite_lease_fake import BACKEND, INSTANCE, FakeSprites
from wayfinder_paths.tests.test_jobs_preflight import _make_job


def _phase_repo(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "data").mkdir(parents=True)
    (root / "data/prices.txt").write_text("1 2 3")
    (root / "data/large-dataset.bin").write_bytes(b"d" * 100000)
    (root / "candidate").mkdir()
    (root / "candidate/params.json").write_text('{"scale": 2}')
    return root


def _pack(root: Path, directory: Path, *, scale: int) -> tuple[Path, Path]:
    base = pack_base(root, ["data"], directory / "base.tar.gz")
    pack_inputs(
        root,
        ["candidate"],
        {
            "phase": phase_name(phases.score_prices),
            "args": {"prices": "data/prices.txt", "scale": scale},
        },
        directory / "inputs.tar.gz",
        base=base,
    )
    return directory / "inputs.tar.gz", directory / "base.tar.gz"


def _run_to_completion(
    client: SpriteBacktestsClient, archive: Path, base: Path, destination: Path
) -> dict[str, Any]:
    run = client.submit_archive(archive, base=base)
    result = client.wait(run["id"])
    assert result["status"] == "succeeded", result
    client.collect(run["id"], destination)
    return result


def _total(destination: Path) -> float:
    return json.loads((destination / "outputs/phase-result.json").read_text())["total"]


class FakeClock:
    """Wall time that only the client's injected sleep advances."""

    def __init__(self) -> None:
        self.now = time.time()
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def test_booking_sends_only_the_token_hash_and_keeps_the_token_private(
    tmp_path: Path,
) -> None:
    fake = FakeSprites(tmp_path)
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    # The token file is private from creation, not after a later chmod.
    umask = os.umask(0)
    try:
        client = fake.client()
        run = client.submit_archive(archive, base=base)
    finally:
        os.umask(umask)
    lease_dir = tmp_path / "leases"
    (record_file,) = lease_dir.glob("*.json")
    record = json.loads(record_file.read_text())
    assert stat.S_IMODE(lease_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(record_file.stat().st_mode) == 0o600
    assert stat.S_IMODE((lease_dir / ".lock").stat().st_mode) == 0o600
    token = record["token"]
    lease = fake.leases[record["id"]]
    assert record == {
        "id": lease["id"],
        "backend": BACKEND,
        "app_name": "shell",
        "preset": "jobs-v1",
        "worker_url": lease["worker_url"],
        "token": token,
        "expires_at": lease["expires_at"],
        "timeout_seconds": 900,
        "transfer_timeout_seconds": 600,
        "setup_timeout_seconds": 0,
        "base": hashlib.sha256(base.read_bytes()).hexdigest(),
        "job": run["job_id"],
    }
    (booking,) = fake.bookings()
    assert json.loads(booking.content) == {
        "preset_key": "jobs-v1",
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
    }
    client.wait(run["id"])
    client.collect(run["id"], tmp_path / "collected")
    client.release(record["id"])
    for request in fake.backend_requests():
        assert request.headers["X-API-Key"] == "owner-key"
        assert token not in str(request.url)
        assert token.encode() not in request.content
        assert all(token not in value for value in request.headers.values())
    workers = fake.worker_requests()
    assert workers and all(
        request.headers["Authorization"] == f"Bearer {token}"
        and "X-API-Key" not in request.headers
        for request in workers
    )
    assert token not in json.dumps(run)
    assert run["id"] == f"{lease['id']}:{run['job_id']}"
    uuid.UUID(run["job_id"], version=4)
    (job,) = [r for r in fake.worker_requests("/jobs") if r.method == "POST"]
    assert json.loads(job.content) == {
        "job_id": run["job_id"],
        "kind": "sdk_job",
        "workspace_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "base_sha256": record["base"],
        "require_artifacts": True,
    }
    client.http.close()


def test_sequential_jobs_reuse_the_lease_and_upload_the_base_once(
    tmp_path: Path,
) -> None:
    fake = FakeSprites(tmp_path)
    root = _phase_repo(tmp_path)
    client = fake.client()
    archives = []
    for scale in (2, 3):
        archive, base = _pack(root, tmp_path / f"packed-{scale}", scale=scale)
        archives.append(base.read_bytes())
        _run_to_completion(client, archive, base, tmp_path / f"collected-{scale}")
    # Packing unchanged inputs again is byte-identical, so the lease's base fits.
    assert archives[0] == archives[1]
    assert _total(tmp_path / "collected-2") == 12.0
    assert _total(tmp_path / "collected-3") == 18.0
    assert len(fake.bookings()) == 1
    assert len(fake.base_uploads) == 1
    assert len(fake.worker_requests("/workspace")) == 2
    # Only a phase's outputs come back; the base stays on the Sprite.
    assert not (tmp_path / "collected-3/data").exists()
    (record,) = client.leases.all()
    assert record["job"] is None
    client.http.close()


def test_a_job_needing_another_base_releases_the_lease_and_books_a_new_one(
    tmp_path: Path,
) -> None:
    fake = FakeSprites(tmp_path)
    root = _phase_repo(tmp_path)
    client = fake.client()
    archive, base = _pack(root, tmp_path / "first", scale=1)
    first = client.submit_archive(archive, base=base)
    client.wait(first["id"])
    client.collect(first["id"], tmp_path / "collected-first")
    (root / "data/prices.txt").write_text("4 5 6")
    archive, base = _pack(root, tmp_path / "second", scale=1)
    second = client.submit_archive(archive, base=base)
    assert second["lease_id"] != first["lease_id"]
    assert fake.leases[first["lease_id"]]["closed_reason"] == "released"
    assert [(r.method, r.url.path) for r in fake.backend_requests()] == [
        ("POST", INSTANCE),
        ("GET", f"{INSTANCE}{first['lease_id']}/"),
        ("DELETE", f"{INSTANCE}{first['lease_id']}/"),
        ("POST", INSTANCE),
    ]
    assert [record["id"] for record in client.leases.all()] == [second["lease_id"]]
    assert client.wait(second["id"])["status"] == "succeeded"
    client.collect(second["id"], tmp_path / "collected-second")
    assert _total(tmp_path / "collected-second") == 15.0
    assert fake.base_uploads == [first["lease_id"], second["lease_id"]]
    client.http.close()


def test_a_lease_holding_uncollected_results_is_not_reused_or_released(
    tmp_path: Path,
) -> None:
    fake = FakeSprites(tmp_path)
    root = _phase_repo(tmp_path)
    client = fake.client()
    archive, base = _pack(root, tmp_path / "packed", scale=2)
    first = client.submit_archive(archive, base=base)
    assert client.wait(first["id"])["artifacts"]
    # The next job would wipe these artifacts: book another lease instead,
    # which the one-open-lease limit refuses.
    with pytest.raises(LeaseUnavailable, match="HTTP 409"):
        client.submit_archive(archive, base=base)
    assert fake.leases[first["lease_id"]]["status"] == "ready"
    client.collect(first["id"], tmp_path / "collected")
    second = client.submit_archive(archive, base=base)
    assert second["lease_id"] == first["lease_id"]
    assert client.wait(second["id"])["status"] == "succeeded"
    client.http.close()


def test_a_lease_without_lifetime_for_another_job_is_replaced(tmp_path: Path) -> None:
    # A 900 s job and two 600 s transfer windows do not fit in what is left.
    fake = FakeSprites(tmp_path, lease_seconds=2000, timeout_seconds=900)
    root = _phase_repo(tmp_path)
    client = fake.client()
    archive, base = _pack(root, tmp_path / "packed", scale=2)
    first = _run_to_completion(client, archive, base, tmp_path / "first")
    second = _run_to_completion(client, archive, base, tmp_path / "second")
    assert first["lease_id"] != second["lease_id"]
    assert fake.leases[first["lease_id"]]["closed_reason"] == "released"
    client.http.close()


def test_run_ids_route_to_their_stored_lease_from_the_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert split_run_id("lease-1:job_2") == ("lease-1", "job_2")
    for invalid in ("lease", "../lease:job", "lease:job/artifacts", ":job"):
        with pytest.raises(ValueError):
            split_run_id(invalid)
    fake = FakeSprites(tmp_path)
    lease_dir = tmp_path / "node-leases"
    client = fake.client(lease_dir=lease_dir)
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    monkeypatch.setenv("WAYFINDER_API_KEY", "owner-key")
    monkeypatch.setattr(
        sprite_client,
        "SpriteBacktestsClient",
        lambda backend, app_name, api_key, *, lease_dir: fake.client(
            lease_dir=lease_dir
        ),
    )
    common = [
        "--backend",
        BACKEND,
        "--app-name",
        "shell",
        "--lease-dir",
        str(lease_dir),
    ]
    main([*common, "--collect", run["id"], "--output", str(tmp_path / "out")])
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"
    assert _total(tmp_path / "out") == 12.0
    main([*common, "--release", run["lease_id"]])
    assert json.loads(capsys.readouterr().out)["closed_reason"] == "released"
    assert not list(lease_dir.glob("*.json"))
    client.http.close()


def test_an_unreachable_worker_falls_back_to_the_backend_record(
    tmp_path: Path,
) -> None:
    fake = FakeSprites(tmp_path)
    delays: list[float] = []
    client = fake.client(sleep=delays.append)
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    fake.worker_down = True
    recorded = client.wait(run["id"])
    assert delays == [1, 2, 4, 8]  # Bounded exponential backoff.
    assert recorded["status"] == "succeeded" and recorded["source"] == "backend"
    # Django keeps only the anonymized summary, never logs.
    assert set(recorded["result"]) == {"output", "exit_code", "timed_out"}
    assert recorded["result"]["output"]["summary"]["total"] == 12.0
    fake.close(run["lease_id"], "idle_timeout")
    closed = client.status(run["id"])
    assert closed["status"] == "failed" and closed["artifacts"] == {}
    assert closed["error"] == (
        "Lease closed (idle_timeout) before the artifacts of this succeeded job "
        "were collected"
    )
    assert not client.leases.all()
    with pytest.raises(ValueError, match="No artifacts"):
        client.collect(run["id"], tmp_path / "collected")
    client.http.close()


def test_a_lease_django_closed_mid_submission_is_rebooked_once(tmp_path: Path) -> None:
    fake = FakeSprites(tmp_path)
    client = fake.client()
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    first = client.submit_archive(archive, base=base)
    assert client.wait(first["id"])["status"] == "succeeded"
    client.collect(first["id"], tmp_path / "first")
    original = fake._chunk

    def closed_by_django(
        worker: Any, kind: str, request: httpx.Request
    ) -> httpx.Response:
        # Django closed the lease after the node chose to reuse it: the URL is
        # private, so the worker's proxy refuses the token.
        fake._chunk = original
        fake.close(first["lease_id"], "expired")
        return httpx.Response(401, json={"detail": "Invalid lease token"})

    fake._chunk = closed_by_django
    second = client.submit_archive(archive, base=base)
    assert second["lease_id"] != first["lease_id"]
    assert client.wait(second["id"])["status"] == "succeeded"
    assert len(fake.bookings()) == 2 and len(fake.base_uploads) == 2
    client.http.close()


def test_a_running_job_on_an_unreachable_closed_lease_ends_with_its_reason(
    tmp_path: Path,
) -> None:
    fake = FakeSprites(tmp_path)
    fake.hold_jobs = True
    client = fake.client()
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    assert client.status(run["id"])["status"] == "running"
    fake.worker_down = True
    assert client.status(run["id"])["status"] == "running"  # Django: still open.
    fake.close(run["lease_id"], "expired")
    fake.worker_down = False  # The URL is private now: the worker refuses the token.
    result = client.wait(run["id"])
    # Django ends a running job with its lease and says why.
    assert result["status"] == "timed_out" and result["source"] == "backend"
    assert result["error"] == "Lease closed: expired"
    client.http.close()


def test_artifact_downloads_retry_from_the_start_then_fail(tmp_path: Path) -> None:
    fake = FakeSprites(tmp_path)
    client = fake.client()
    root = _phase_repo(tmp_path)
    archive, base = _pack(root, tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    client.wait(run["id"])
    fake.corrupt_artifacts = 1
    client.collect(run["id"], tmp_path / "recovered")
    assert _total(tmp_path / "recovered") == 12.0
    path = f"/jobs/{run['job_id']}/artifacts"
    assert len(fake.worker_requests(path)) == 2
    run = client.submit_archive(archive, base=base)
    client.wait(run["id"])
    fake.corrupt_artifacts = DOWNLOAD_ATTEMPTS
    with pytest.raises(ValueError, match="checksum or size mismatch"):
        client.collect(run["id"], tmp_path / "collected")
    path = f"/jobs/{run['job_id']}/artifacts"
    assert len(fake.worker_requests(path)) == DOWNLOAD_ATTEMPTS
    assert not (tmp_path / "collected").exists()
    assert client.leases.all()[0]["job"] == run["job_id"]  # Not collected.
    client.http.close()


def test_a_lost_booking_answer_is_retried_with_the_same_token(tmp_path: Path) -> None:
    fake = FakeSprites(tmp_path)
    fake.lost_bookings = 1
    client = fake.client()
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    first, retry = fake.bookings()
    assert first.content == retry.content
    assert list(fake.leases) == [run["lease_id"]]
    assert client.wait(run["id"])["status"] == "succeeded"
    client.http.close()


def test_a_provisioning_lease_is_polled_until_ready(tmp_path: Path) -> None:
    fake = FakeSprites(tmp_path)
    fake.provisioning_polls = 2
    clock = FakeClock()
    client = fake.client(sleep=clock.sleep, clock=clock)
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    # Django answers a booking once provisioning finishes, so the first answer
    # times out; the retry with the same token finds it still provisioning.
    assert clock.slept == [1] + [PROVISIONING_POLL_SECONDS] * 2
    assert client.wait(run["id"])["status"] == "succeeded"
    client.http.close()


def test_a_lease_that_never_becomes_ready_is_released(tmp_path: Path) -> None:
    fake = FakeSprites(tmp_path)
    fake.provisioning_polls = None
    clock = FakeClock()
    client = fake.client(sleep=clock.sleep, clock=clock)
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    with pytest.raises(LeaseUnavailable, match="provisioning instead of ready"):
        client.submit_archive(archive, base=base)
    assert sum(clock.slept) == 1 + PROVISIONING_TIMEOUT_SECONDS
    ((_, lease),) = fake.leases.items()
    assert lease["closed_reason"] == "released"
    assert not fake.worker_requests() and not client.leases.all()
    client.http.close()


def test_a_lease_that_closed_under_a_submission_is_rebooked_once(
    tmp_path: Path,
) -> None:
    fake = FakeSprites(tmp_path)
    root = _phase_repo(tmp_path)
    client = fake.client()
    archive, base = _pack(root, tmp_path / "packed", scale=2)
    first = _run_to_completion(client, archive, base, tmp_path / "first")
    fake.idle_out(first["lease_id"])  # Django still reports it ready.
    second = _run_to_completion(client, archive, base, tmp_path / "second")
    assert second["lease_id"] != first["lease_id"]
    assert fake.leases[first["lease_id"]]["closed_reason"] == "released"
    # The new lease received the base again.
    assert fake.base_uploads == [first["lease_id"], second["lease_id"]]
    assert _total(tmp_path / "second") == 12.0
    client.http.close()


def test_a_second_closed_lease_makes_compute_unavailable(tmp_path: Path) -> None:
    fake = FakeSprites(tmp_path)
    fake.closed_on_booking = True
    client = fake.client()
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    with pytest.raises(LeaseUnavailable, match="again after booking a new lease"):
        client.submit_archive(archive, base=base)
    assert len(fake.bookings()) == 2
    assert all(lease["closed_reason"] == "released" for lease in fake.leases.values())
    assert not client.leases.all()
    client.http.close()


def test_a_chunk_whose_answer_was_lost_is_sent_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sprite_client, "CHUNK_BYTES", 1024)
    fake = FakeSprites(tmp_path)
    fake.lost_chunk_answers = {("base", 0), ("workspace", 0)}
    client = fake.client()
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    parts = [int(r.headers["X-Bundle-Part"]) for r in fake.worker_requests("/base")]
    assert parts[:2] == [0, 0] and parts[2:] == list(range(1, len(parts) - 1))
    assert client.wait(run["id"])["status"] == "succeeded"
    client.http.close()


def test_an_upload_conflict_rereads_status_and_restarts_once(tmp_path: Path) -> None:
    fake = FakeSprites(tmp_path)
    fake.conflicts = {"base", "workspace"}
    client = fake.client()
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    assert len(fake.worker_requests("/status")) == 2
    assert client.wait(run["id"])["status"] == "succeeded"
    client.http.close()


def test_wait_stops_at_its_deadline_cancels_and_keeps_the_lease(
    tmp_path: Path,
) -> None:
    fake = FakeSprites(tmp_path)
    fake.hold_jobs = True
    clock = FakeClock()
    client = fake.client(sleep=clock.sleep, clock=clock)
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    started = clock()
    result = client.wait(run["id"], poll_interval=10, timeout=60)
    assert clock() - started == 60
    assert result["status"] == "timed_out" and result["artifacts"] == {}
    assert result["error"] == (
        f"The node stopped waiting for job {run['job_id']} on lease "
        f"{run['lease_id']} after 60 s and cancelled it"
    )
    methods = [r.method for r in fake.worker_requests(f"/jobs/{run['job_id']}")]
    assert methods[-1] == "DELETE"
    assert client.status(run["id"])["status"] == "cancelled"
    # The lease is kept for the next job, which runs on it.
    fake.hold_jobs = False
    second = client.submit_archive(archive, base=base)
    assert second["lease_id"] == run["lease_id"]
    assert client.wait(second["id"])["status"] == "succeeded"
    client.http.close()


def test_an_unreachable_worker_cannot_stretch_the_wait_past_its_deadline(
    tmp_path: Path,
) -> None:
    fake = FakeSprites(tmp_path)
    fake.hold_jobs = True
    clock = FakeClock()
    client = fake.client(sleep=clock.sleep, clock=clock)
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    fake.worker_down = True
    started = clock()
    result = client.wait(run["id"], poll_interval=1, timeout=6)
    # Retry backoff stops at the deadline; the cancel gets its own short budget.
    assert clock() - started <= 6 + CANCEL_BUDGET_SECONDS
    assert result["status"] == "timed_out"
    assert "stopped waiting" in result["error"]
    client.http.close()


def test_the_default_wait_ends_by_the_lease_expiry(tmp_path: Path) -> None:
    # The job timeout, a transfer window and 120 s of grace bound the wait,
    # never past the lease's expiry plus the grace.
    fake = FakeSprites(tmp_path, lease_seconds=1000, timeout_seconds=900)
    fake.hold_jobs = True
    clock = FakeClock()
    client = fake.client(sleep=clock.sleep, clock=clock)
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    started = clock()
    assert client.wait(run["id"], poll_interval=5)["status"] == "timed_out"
    expires = datetime.fromisoformat(fake.leases[run["lease_id"]]["expires_at"])
    assert min(900 + 600 + 120, expires.timestamp() + 120 - started) < 1200
    assert clock() - started == pytest.approx(
        expires.timestamp() + 120 - started, abs=5
    )
    client.http.close()


def test_booking_asks_django_to_install_the_nodes_sdk_commit(tmp_path: Path) -> None:
    fake = FakeSprites(tmp_path)
    client = fake.client(sdk_commit="a" * 40)
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    assert client.wait(run["id"])["status"] == "succeeded"
    (booking,) = fake.bookings()
    assert json.loads(booking.content)["sdk_commit"] == "a" * 40
    assert fake.leases[run["lease_id"]]["runtime"]["sdk_commit"] == "a" * 40
    # A submission pinned to another commit replaces the lease (one per owner) with a
    # lease running that commit, once the first run's results are collected.
    client.collect(run["id"], tmp_path / "collected")
    pinned = client.submit_archive(archive, base=base, expected_sdk_commit="b" * 40)
    assert pinned["lease_id"] != run["lease_id"]
    assert json.loads(fake.bookings()[-1].content)["sdk_commit"] == "b" * 40
    assert client.wait(pinned["id"])["status"] == "succeeded"
    client.http.close()


def test_the_default_wait_includes_the_setup_allowance(tmp_path: Path) -> None:
    # A fresh Sprite installs its runtime before the first job runs.
    fake = FakeSprites(
        tmp_path,
        lease_seconds=7200,
        timeout_seconds=60,
        transfer_timeout_seconds=30,
        setup_timeout_seconds=600,
    )
    fake.hold_jobs = True
    clock = FakeClock()
    client = fake.client(sleep=clock.sleep, clock=clock)
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    started = clock()
    assert client.wait(run["id"], poll_interval=5)["status"] == "timed_out"
    assert clock() - started == pytest.approx(60 + 30 + 600 + 120, abs=5)
    client.http.close()


def test_cancellation_keeps_the_lease_for_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSprites(tmp_path)
    fake.hold_jobs = True
    client = fake.client()
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    client.cancel(run["id"])
    assert client.status(run["id"])["status"] == "cancelled"
    assert fake.leases[run["lease_id"]]["status"] == "ready"
    (record,) = client.leases.all()
    assert record["job"] is None
    # Ctrl-C while submitting keeps the lease too.
    original = client._upload

    def interrupted(record: LeaseRecord, resource: str, archive: Path) -> Any:
        if resource == "workspace":
            raise KeyboardInterrupt
        return original(record, resource, archive)

    monkeypatch.setattr(client, "_upload", interrupted)
    with pytest.raises(KeyboardInterrupt):
        client.submit_archive(archive, base=base)
    assert fake.leases[run["lease_id"]]["status"] == "ready"
    assert client.leases.all()[0]["job"] is None
    monkeypatch.undo()
    fake.hold_jobs = False
    second = client.submit_archive(archive, base=base)
    assert second["lease_id"] == run["lease_id"]
    assert client.wait(second["id"])["status"] == "succeeded"
    client.http.close()


def _failures(
    tmp_path: Path,
) -> tuple[str, str, list[tuple[BaseException, tuple[str, ...]]]]:
    fake = FakeSprites(tmp_path)
    client = fake.client()
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    client.wait(run["id"])
    (record,) = client.leases.all()
    token = record["token"]
    caught: list[tuple[BaseException, tuple[str, ...]]] = []

    def expect(call: Callable[[], Any], *names: str) -> None:
        with pytest.raises(Exception) as raised:
            call()
        caught.append((raised.value, names))

    ids = (run["lease_id"], run["job_id"])
    fake.corrupt_artifacts = DOWNLOAD_ATTEMPTS
    expect(lambda: client.collect(run["id"], tmp_path / "bad"), *ids)
    client.leases.save({**record, "token": "forged"})
    expect(lambda: client.collect(run["id"], tmp_path / "forged"), *ids)
    client.leases.save(record)
    expect(lambda: client.collect(run["id"], tmp_path / "packed"), *ids)
    client.collect(run["id"], tmp_path / "collected")
    fake.worker_down = True  # The next submission reuses the lease, then fails.
    expect(lambda: client.submit_archive(archive, base=base), run["lease_id"])
    fake.worker_down = False
    fake.refuse = 429
    expect(lambda: client.submit_archive(archive, base=base), "lease none")
    client.http.close()
    return token, run["lease_id"], caught


def test_errors_name_their_lease_and_job_and_never_the_token(tmp_path: Path) -> None:
    token, lease_id, caught = _failures(tmp_path)
    assert len(caught) == 5
    for error, names in caught:
        message = str(error)
        assert all(name in message for name in names), message
        assert "job " in message and "lease " in message, message
        text = "".join(traceback.format_exception(error))
        assert token not in text
        chained: BaseException | None = error
        while chained is not None:
            assert token not in str(chained)
            chained = chained.__cause__ or chained.__context__


def _record(worker_url: str = "https://sprite.example") -> LeaseRecord:
    return {
        "id": "lease",
        "backend": BACKEND,
        "app_name": "shell",
        "preset": "jobs-v1",
        "worker_url": worker_url,
        "token": "node-token",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "timeout_seconds": 900,
        "transfer_timeout_seconds": 600,
        "setup_timeout_seconds": 0,
        "base": None,
        "job": "job",
    }


def test_worker_uploads_retry_unreachable_and_gateway_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sprite_client, "CHUNK_BYTES", 4)
    archive = tmp_path / "workspace.tar.gz"
    archive.write_bytes(b"1234567890")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    calls: list[int] = []
    delays: list[float] = []

    def api(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/workspace"
        part = int(request.headers["X-Bundle-Part"])
        calls.append(part)
        if calls == [0, 1]:
            raise httpx.ConnectError("waking", request=request)
        if calls == [0, 1, 1]:
            return httpx.Response(503)
        assert request.content == archive.read_bytes()[part * 4 : (part + 1) * 4]
        body: dict[str, Any] = (
            {"sha256": digest, "size": 10}
            if part == 2
            else {"part": part, "accepted": True}
        )
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(api)) as http:
        client = SpriteBacktestsClient(
            BACKEND,
            "shell",
            "key",
            client=http,
            sleep=delays.append,
            lease_dir=tmp_path / "leases",
        )
        assert client._upload(_record(), "workspace", archive) == {
            "sha256": digest,
            "size": 10,
        }
    assert calls == [0, 1, 1, 1, 2]
    assert delays == [1, 2]


@pytest.mark.parametrize("status_code,attempts", [(400, 1), (401, 1), (504, ATTEMPTS)])
def test_worker_errors_retry_only_gateway_statuses(
    tmp_path: Path, status_code: int, attempts: int
) -> None:
    archive = tmp_path / "workspace.tar.gz"
    archive.write_bytes(b"data")
    calls: list[httpx.Request] = []
    delays: list[float] = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status_code)

    with httpx.Client(transport=httpx.MockTransport(api)) as http:
        client = SpriteBacktestsClient(
            BACKEND,
            "shell",
            "key",
            client=http,
            sleep=delays.append,
            lease_dir=tmp_path / "leases",
        )
        with pytest.raises(httpx.HTTPStatusError):
            client._upload(_record(), "workspace", archive)
    assert len(calls) == attempts
    assert delays == [2**attempt for attempt in range(attempts - 1)]


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "timed_out", "cancelled"])
def test_wait_stops_at_each_terminal_status(tmp_path: Path, terminal: str) -> None:
    statuses = iter(["running", "running", terminal])
    delays: list[float] = []

    def api(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "sprite.example"
        assert request.url.path == "/jobs/job"
        assert request.headers["Authorization"] == "Bearer node-token"
        return httpx.Response(
            200,
            json={"job_id": "job", "status": next(statuses), "result": {}, "error": ""},
        )

    with httpx.Client(transport=httpx.MockTransport(api), base_url=BACKEND) as http:
        with SpriteBacktestsClient(
            BACKEND,
            "shell",
            "owner-key",
            client=http,
            sleep=delays.append,
            lease_dir=tmp_path / "leases",
        ) as client:
            client.leases.save(_record("https://sprite.example/"))
            result = client.wait("lease:job", poll_interval=0.5)
        assert not http.is_closed
    assert result["id"] == "lease:job" and result["status"] == terminal
    assert delays == [0.5, 0.5]
    # A job that finished without artifacts has nothing left to collect.
    assert client.leases.all()[0]["job"] is None


def test_context_manager_closes_owned_http_client() -> None:
    with SpriteBacktestsClient(BACKEND, "shell", "key") as client:
        assert not client.http.is_closed
        assert client.http.timeout == sprite_client.CALL_TIMEOUT
    assert client.http.is_closed


def test_failed_booking_validation_releases_the_lease(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path / "source")
    fake = FakeSprites(tmp_path, capabilities=())
    client = fake.client()
    with pytest.raises(ValueError, match="sdk-workspace-v1"):
        client.submit(store, job_id)
    ((lease_id, lease),) = fake.leases.items()
    assert lease["closed_reason"] == "released"
    assert [(r.method, r.url.path) for r in fake.backend_requests()] == [
        ("POST", INSTANCE),
        ("DELETE", f"{INSTANCE}{lease_id}/"),
    ]
    assert not fake.worker_requests() and not client.leases.all()
    client.http.close()


def test_a_failed_upload_releases_the_lease(tmp_path: Path) -> None:
    fake = FakeSprites(tmp_path)
    client = fake.client()
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    fake.worker_down = True
    # Nothing ran, so the job can still run locally.
    with pytest.raises(LeaseUnavailable, match="worker unreachable while submitting"):
        client.submit_archive(archive, base=base)
    assert len(fake.worker_requests("/base")) == ATTEMPTS
    ((_, lease),) = fake.leases.items()
    assert lease["closed_reason"] == "released" and not client.leases.all()
    client.http.close()


def test_submit_packs_a_job_and_collects_its_whole_workspace(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path / "source")
    fake = FakeSprites(tmp_path)
    client = fake.client()
    run = client.submit(store, job_id, op="script", options={"path": "missing.py"})
    result = client.wait(run["id"])
    assert result["status"] == "failed" and result["artifacts"]
    client.collect(run["id"], tmp_path / "collected")
    assert (tmp_path / "collected/sprite-request.json").is_file()
    assert (tmp_path / "collected/operation-error.json").is_file()
    with pytest.raises(FileExistsError):
        client.collect(run["id"], tmp_path / "collected")
    client.http.close()


def test_worker_urls_without_tls_never_receive_the_token(tmp_path: Path) -> None:
    fake = FakeSprites(tmp_path)
    booked = fake._book

    def plaintext(body: dict[str, Any]) -> dict[str, Any]:
        lease = booked(body)
        lease["worker_url"] = lease["worker_url"].replace("https://", "http://")
        return lease

    fake._book = plaintext  # type: ignore[method-assign]
    client = fake.client()
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    with pytest.raises(ValueError, match="Refusing to send the lease token"):
        client.submit_archive(archive, base=base)
    assert not fake.worker_requests()
    client.http.close()


def test_empty_upload_fails_without_sending_a_request(tmp_path: Path) -> None:
    archive = tmp_path / "empty.tar.gz"
    archive.touch()

    def api(request: httpx.Request) -> httpx.Response:
        pytest.fail("Empty archives must not send an upload request")

    with httpx.Client(transport=httpx.MockTransport(api)) as http:
        client = SpriteBacktestsClient(
            BACKEND, "shell", "key", client=http, lease_dir=tmp_path / "leases"
        )
        with pytest.raises(ValueError, match="empty workspace archive"):
            client._upload(_record(), "workspace", archive)


def test_cli_rejects_non_object_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WAYFINDER_API_KEY", "owner-key")
    options = tmp_path / "options.json"
    options.write_text("[]")
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--backend",
                BACKEND,
                "--app-name",
                "shell",
                "--job-id",
                "job",
                "--submit-only",
                "--options",
                str(options),
            ]
        )
    assert exc.value.code == 2
    assert "--options must contain a JSON object" in capsys.readouterr().err


def test_a_lease_is_reused_only_for_the_nodes_commit_and_a_working_setup(
    tmp_path: Path,
) -> None:
    fake = FakeSprites(tmp_path)
    client = fake.client(sdk_commit="a" * 40)
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)

    def run_once(name: str) -> dict[str, Any]:
        run = client.submit_archive(archive, base=base)
        assert client.wait(run["id"])["status"] == "succeeded"
        client.collect(run["id"], tmp_path / name)
        return run

    first = run_once("one")
    # The node's SDK changed: the old lease is released and a new one runs the new commit.
    client.sdk_commit = "b" * 40
    second = run_once("two")
    assert second["lease_id"] != first["lease_id"]
    assert fake.leases[first["lease_id"]]["closed_reason"] == "released"
    # A lease whose install failed is never reused.
    fake.leases[second["lease_id"]]["setup"] = {"state": "failed", "error": "no wheel"}
    third = run_once("three")
    assert third["lease_id"] != second["lease_id"]
    # A node that cannot name its commit never reuses an SDK lease.
    client.sdk_commit = None
    assert run_once("four")["lease_id"] != third["lease_id"]
    client.http.close()


@pytest.mark.parametrize(
    "answer",
    [
        lambda request: httpx.Response(200, text="<html>captive portal</html>"),
        lambda request: httpx.Response(500, json={"detail": "boom"}),
        lambda request: httpx.Response(
            302, headers={"Location": "https://login.example"}
        ),
        lambda request: httpx.Response(
            201, json={"id": str(uuid.uuid4()), "status": "ready"}
        ),
    ],
)
def test_an_unusable_backend_answer_leaves_the_node_to_compute_locally(
    tmp_path: Path, answer: Callable[[httpx.Request], httpx.Response]
) -> None:
    http = httpx.Client(transport=httpx.MockTransport(answer), base_url=BACKEND)
    client = SpriteBacktestsClient(
        BACKEND,
        "shell",
        "owner-key",
        client=http,
        sleep=lambda seconds: None,
        lease_dir=tmp_path / "leases",
    )
    archive = tmp_path / "inputs.tgz"
    archive.write_bytes(b"archive")
    with pytest.raises(LeaseUnavailable):
        client.submit_archive(archive)
    http.close()


def test_a_corrupt_lease_record_never_breaks_submissions(tmp_path: Path) -> None:
    fake = FakeSprites(tmp_path)
    client = fake.client()
    (client.leases._ready() / "broken.json").write_text("{not json")
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    assert client.wait(run["id"])["status"] == "succeeded"
    client.http.close()


def test_choosing_a_lease_never_waits_long_on_another_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sprite_client, "LOCK_WAIT_SECONDS", 0.3)
    fake = FakeSprites(tmp_path)
    client = fake.client()
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    with client.leases.locked():  # Another process booking holds the lease lock.
        with pytest.raises(LeaseUnavailable, match="choosing a lease"):
            client.submit_archive(archive, base=base)
    client.http.close()


def test_waiting_rides_out_a_network_outage(tmp_path: Path) -> None:
    fake = FakeSprites(tmp_path)
    clock = FakeClock()
    client = fake.client(sleep=clock.sleep, clock=clock)
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    # The laptop goes offline: neither the worker nor the backend answers for a while.
    failures = {"left": 2 * ATTEMPTS + 2}

    def offline(request: httpx.Request) -> httpx.Response:
        if failures["left"]:
            failures["left"] -= 1
            raise httpx.ConnectError("offline", request=request)
        return fake.handle(request)

    client.http.close()
    client.http = httpx.Client(transport=httpx.MockTransport(offline), base_url=BACKEND)
    assert client.wait(run["id"], poll_interval=5)["status"] == "succeeded"
    assert not failures["left"]
    client.http.close()


def test_release_idle_ends_only_leases_no_submission_uses(tmp_path: Path) -> None:
    fake = FakeSprites(tmp_path)
    client = fake.client()
    archive, base = _pack(_phase_repo(tmp_path), tmp_path / "packed", scale=2)
    run = client.submit_archive(archive, base=base)
    assert client.release_idle() == []  # Its results are not collected yet.
    assert client.wait(run["id"])["status"] == "succeeded"
    client.collect(run["id"], tmp_path / "collected")
    assert client.release_idle() == [run["lease_id"]]
    assert fake.leases[run["lease_id"]]["closed_reason"] == "released"
    assert not client.leases.all()
    client.http.close()
