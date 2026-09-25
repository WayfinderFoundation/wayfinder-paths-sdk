from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from wayfinder_paths.jobs.sprite_bundle import (
    ArchiveMetadata,
    pack_job,
    sha256,
)
from wayfinder_paths.jobs.sprite_client import SpriteBacktestsClient, main
from wayfinder_paths.tests.test_jobs_preflight import _make_job


def test_client_submits_bundle_and_collects_using_owner_auth(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path / "source")
    archive = tmp_path / "artifact.tar.gz"
    pack_job(store, job_id, archive)
    metadata = {"sha256": sha256(archive), "size": archive.stat().st_size}
    calls: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST" and request.url.path.endswith("/sprite-backtests/"):
            return httpx.Response(
                201,
                json={
                    "id": "lease",
                    "auth_token": "agent-token",
                    "runtime": {"capabilities": ["sdk-workspace-v1"]},
                },
            )
        if request.method == "PUT":
            assert request.headers["Authorization"] == "Bearer agent-token"
            assert "X-API-Key" not in request.headers
            return httpx.Response(
                200,
                json={
                    "sha256": request.headers["X-Bundle-SHA256"],
                    "size": int(request.headers["X-Bundle-Size"]),
                },
            )
        if request.url.path.endswith("/artifacts/"):
            assert request.headers["X-API-Key"] == "owner-key"
            return httpx.Response(200, content=archive.read_bytes())
        return httpx.Response(200, json={"status": "succeeded", "artifacts": metadata})

    with httpx.Client(
        transport=httpx.MockTransport(api), base_url="https://backend.example"
    ) as http:
        client = SpriteBacktestsClient(
            "https://backend.example", "shell", "owner-key", client=http
        )
        result = client.submit(store, job_id)
        assert "auth_token" not in result
        client.collect("lease", tmp_path / "collected")
        assert (tmp_path / "collected/sprite-request.json").is_file()
        with pytest.raises(FileExistsError):
            client.collect("lease", tmp_path / "collected")
    assert [(request.method, request.url.path) for request in calls] == [
        ("POST", "/api/v1/opencode/instances/shell/sprite-backtests/"),
        ("PUT", "/api/v1/opencode/sprite-backtests/lease/workspace/"),
        ("POST", "/api/v1/opencode/sprite-backtests/lease/jobs/"),
        ("GET", "/api/v1/opencode/instances/shell/sprite-backtests/lease/"),
        ("GET", "/api/v1/opencode/instances/shell/sprite-backtests/lease/artifacts/"),
        ("GET", "/api/v1/opencode/instances/shell/sprite-backtests/lease/"),
    ]


def test_client_retries_individual_upload_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wayfinder_paths.jobs import sprite_client

    monkeypatch.setattr(sprite_client, "CHUNK_BYTES", 4)
    archive = tmp_path / "workspace.tar.gz"
    archive.write_bytes(b"1234567890")
    packed: ArchiveMetadata = {"sha256": sha256(archive), "size": 10}
    calls: list[int] = []
    delays: list[float] = []

    def api(request: httpx.Request) -> httpx.Response:
        part = int(request.headers["X-Bundle-Part"])
        calls.append(part)
        if calls == [0, 1]:
            return httpx.Response(503)
        assert request.content == archive.read_bytes()[part * 4 : (part + 1) * 4]
        body: dict[str, Any] = (
            dict(packed) if part == 2 else {"part": part, "accepted": True}
        )
        return httpx.Response(200, json=body)

    with httpx.Client(
        transport=httpx.MockTransport(api), base_url="https://backend.example"
    ) as http:
        client = SpriteBacktestsClient(
            "https://backend.example", "shell", "key", client=http, sleep=delays.append
        )
        client._upload(
            "/workspace/", {"Authorization": "Bearer scoped"}, archive, packed
        )
    assert calls == [0, 1, 1, 2]
    assert delays == [1]


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "timed_out", "cancelled"])
def test_wait_stops_at_each_terminal_status(terminal: str) -> None:
    statuses = iter(["queued", "running", terminal])
    delays: list[float] = []

    def api(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "owner-key"
        return httpx.Response(200, json={"status": next(statuses)})

    with httpx.Client(
        transport=httpx.MockTransport(api), base_url="https://backend.example"
    ) as http:
        with SpriteBacktestsClient(
            "https://backend.example",
            "shell",
            "owner-key",
            client=http,
            sleep=delays.append,
        ) as client:
            assert client.wait("lease", poll_interval=0.5) == {"status": terminal}
        assert not http.is_closed
    assert delays == [0.5, 0.5]


def test_context_manager_closes_owned_http_client() -> None:
    with SpriteBacktestsClient("https://backend.example", "shell", "key") as client:
        assert not client.http.is_closed
    assert client.http.is_closed


@pytest.mark.parametrize("status_code,attempts", [(400, 1), (503, 3)])
def test_upload_errors_have_bounded_retries(
    tmp_path: Path, status_code: int, attempts: int
) -> None:
    archive = tmp_path / "workspace.tar.gz"
    archive.write_bytes(b"data")
    metadata: ArchiveMetadata = {"sha256": sha256(archive), "size": 4}
    calls: list[httpx.Request] = []
    delays: list[float] = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status_code)

    with httpx.Client(
        transport=httpx.MockTransport(api), base_url="https://backend.example"
    ) as http:
        client = SpriteBacktestsClient(
            "https://backend.example", "shell", "key", client=http, sleep=delays.append
        )
        with pytest.raises(httpx.HTTPStatusError):
            client._upload("/workspace/", {}, archive, metadata)
    assert len(calls) == attempts
    assert delays == list(range(1, attempts))


def test_failed_submission_cancels_lease_with_owner_auth(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path)
    calls: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"id": "lease", "runtime": {}})
        assert request.method == "DELETE"
        assert request.headers["X-API-Key"] == "owner-key"
        assert "Authorization" not in request.headers
        return httpx.Response(204)

    with httpx.Client(
        transport=httpx.MockTransport(api), base_url="https://backend.example"
    ) as http:
        client = SpriteBacktestsClient(
            "https://backend.example", "shell", "owner-key", client=http
        )
        with pytest.raises(ValueError, match="sdk-workspace-v1"):
            client.submit(store, job_id)
    assert len(calls) == 2
    assert (
        calls[-1].url.path == "/api/v1/opencode/instances/shell/sprite-backtests/lease/"
    )


def test_empty_upload_fails_without_sending_a_request(tmp_path: Path) -> None:
    archive = tmp_path / "empty.tar.gz"
    archive.touch()

    def api(request: httpx.Request) -> httpx.Response:
        pytest.fail("Empty archives must not send an upload request")

    with httpx.Client(transport=httpx.MockTransport(api)) as http:
        client = SpriteBacktestsClient(
            "https://backend.example", "shell", "key", client=http
        )
        with pytest.raises(ValueError, match="empty workspace archive"):
            client._upload(
                "/workspace/", {}, archive, {"sha256": sha256(archive), "size": 0}
            )


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
                "https://backend.example",
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


def test_submit_archive_uploads_a_prebuilt_archive_unchanged(tmp_path: Path) -> None:
    archive = tmp_path / "inputs.tar.gz"
    archive.write_bytes(b"prebuilt phase archive")
    uploaded: list[bytes] = []
    calls: list[tuple[str, str]] = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/sprite-backtests/"):
            assert request.headers["X-API-Key"] == "owner-key"
            return httpx.Response(
                201,
                json={
                    "id": "lease",
                    "auth_token": "agent-token",
                    "runtime": {"capabilities": ["sdk-workspace-v1"]},
                },
            )
        if request.method == "PUT":
            uploaded.append(request.content)
            return httpx.Response(
                200, json={"sha256": sha256(archive), "size": archive.stat().st_size}
            )
        assert request.headers["Authorization"] == "Bearer agent-token"
        assert request.read() == (
            b'{"kind":"sdk_job","workspace_sha256":"' + sha256(archive).encode() + b'"}'
        )
        return httpx.Response(202, json={"status": "queued"})

    with httpx.Client(
        transport=httpx.MockTransport(api), base_url="https://backend.example"
    ) as http:
        client = SpriteBacktestsClient(
            "https://backend.example", "shell", "owner-key", client=http
        )
        lease = client.submit_archive(archive, preset="phases")
    assert lease == {"id": "lease", "runtime": {"capabilities": ["sdk-workspace-v1"]}}
    assert b"".join(uploaded) == archive.read_bytes()
    assert calls == [
        ("POST", "/api/v1/opencode/instances/shell/sprite-backtests/"),
        ("PUT", "/api/v1/opencode/sprite-backtests/lease/workspace/"),
        ("POST", "/api/v1/opencode/sprite-backtests/lease/jobs/"),
    ]


def test_submit_archive_cancels_its_lease_when_upload_fails(tmp_path: Path) -> None:
    archive = tmp_path / "inputs.tar.gz"
    archive.write_bytes(b"prebuilt")
    deleted: list[str] = []

    def api(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "id": "lease",
                    "auth_token": "agent-token",
                    "runtime": {"capabilities": ["sdk-workspace-v1"]},
                },
            )
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(204)
        return httpx.Response(400, json={"detail": "bad chunk"})

    with httpx.Client(
        transport=httpx.MockTransport(api), base_url="https://backend.example"
    ) as http:
        client = SpriteBacktestsClient(
            "https://backend.example", "shell", "owner-key", client=http
        )
        with pytest.raises(httpx.HTTPStatusError):
            client.submit_archive(archive)
    assert deleted == ["/api/v1/opencode/instances/shell/sprite-backtests/lease/"]
