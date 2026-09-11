from __future__ import annotations

import errno
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from wayfinder_paths.paths.client import PathsApiClient
from wayfinder_paths.paths.heartbeat import maybe_heartbeat_installed_paths


def _write_lockfile(root: Path) -> None:
    state_dir = root / ".wayfinder"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "paths.lock.json").write_text(
        json.dumps(
            {
                "schemaVersion": "0.1",
                "paths": {
                    "demo-path": {
                        "installation_id": "install-123",
                        "heartbeat_token": "heartbeat-secret",
                    }
                },
            }
        )
        + "\n"
    )


def _write_legacy_lockfile(root: Path) -> None:
    state_dir = root / ".wayfinder"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "packs.lock.json").write_text(
        json.dumps(
            {
                "schemaVersion": "0.1",
                "packs": {
                    "legacy-path": {
                        "installation_id": "install-legacy",
                        "heartbeat_token": "legacy-secret",
                    }
                },
            }
        )
        + "\n"
    )


def test_maybe_heartbeat_installed_paths_sends_and_writes_state(
    tmp_path: Path, monkeypatch
) -> None:
    _write_lockfile(tmp_path)
    monkeypatch.setenv("WAYFINDER_PATHS_API_URL", "https://paths.example")
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-instance")

    class FakeClient:
        calls: list[dict[str, object]] = []

        def submit_batch_install_heartbeats(self, **kwargs):
            self.__class__.calls.append(kwargs)
            return {
                "results": [{"installation_id": "install-123", "status": "recorded"}]
            }

    now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
    result = maybe_heartbeat_installed_paths(
        trigger="mcp-cli",
        cwd=tmp_path,
        client=FakeClient(),
        now=now,
    )

    assert result.status == "recorded"
    assert result.sent == 1
    assert FakeClient.calls == [
        {
            "heartbeats": [
                {
                    "slug": "demo-path",
                    "installation_id": "install-123",
                    "heartbeat_token": "heartbeat-secret",
                    "status": "active",
                }
            ],
            "source": "mcp-cli",
        }
    ]

    state = json.loads((tmp_path / ".wayfinder" / "paths-heartbeat.json").read_text())
    assert state["last_trigger"] == "mcp-cli"
    assert state["sent"] == 1


def test_maybe_heartbeat_installed_paths_reads_legacy_lockfile_and_rewrites_state(
    tmp_path: Path, monkeypatch
) -> None:
    _write_legacy_lockfile(tmp_path)
    monkeypatch.setenv("WAYFINDER_PATHS_API_URL", "https://paths.example")
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-instance")

    class FakeClient:
        calls: list[dict[str, object]] = []

        def submit_batch_install_heartbeats(self, **kwargs):
            self.__class__.calls.append(kwargs)
            return {
                "results": [{"installation_id": "install-legacy", "status": "recorded"}]
            }

    result = maybe_heartbeat_installed_paths(
        trigger="mcp-server",
        cwd=tmp_path,
        client=FakeClient(),
        now=datetime(2026, 4, 7, 12, 0, tzinfo=UTC),
    )

    assert result.status == "recorded"
    assert FakeClient.calls == [
        {
            "heartbeats": [
                {
                    "slug": "legacy-path",
                    "installation_id": "install-legacy",
                    "heartbeat_token": "legacy-secret",
                    "status": "active",
                }
            ],
            "source": "mcp-server",
        }
    ]
    assert (tmp_path / ".wayfinder" / "paths-heartbeat.json").exists()
    assert (tmp_path / ".wayfinder" / "packs-heartbeat.json").exists() is False


def test_maybe_heartbeat_installed_paths_respects_cooldown(
    tmp_path: Path, monkeypatch
) -> None:
    _write_lockfile(tmp_path)
    monkeypatch.setenv("WAYFINDER_PATHS_API_URL", "https://paths.example")
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-instance")
    state_path = tmp_path / ".wayfinder" / "paths-heartbeat.json"
    state_path.write_text(
        json.dumps(
            {
                "schemaVersion": "0.1",
                "last_success_at": datetime(2026, 4, 7, 10, 0, tzinfo=UTC).isoformat(),
                "last_trigger": "mcp-server",
            }
        )
        + "\n"
    )

    class FakeClient:
        def submit_batch_install_heartbeats(self, **kwargs):
            raise AssertionError("batch heartbeat should not be called during cooldown")

    result = maybe_heartbeat_installed_paths(
        trigger="mcp-cli",
        cwd=tmp_path,
        client=FakeClient(),
        now=datetime(2026, 4, 7, 12, 0, tzinfo=UTC),
        cooldown=timedelta(hours=24),
    )

    assert result.status == "skipped"
    assert result.reason == "cooldown_active"


def test_maybe_heartbeat_installed_paths_reads_legacy_cooldown_state(
    tmp_path: Path, monkeypatch
) -> None:
    _write_legacy_lockfile(tmp_path)
    monkeypatch.setenv("WAYFINDER_PATHS_API_URL", "https://paths.example")
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-instance")
    legacy_state_path = tmp_path / ".wayfinder" / "packs-heartbeat.json"
    legacy_state_path.write_text(
        json.dumps(
            {
                "schemaVersion": "0.1",
                "last_success_at": datetime(2026, 4, 7, 10, 0, tzinfo=UTC).isoformat(),
                "last_trigger": "mcp-server",
            }
        )
        + "\n"
    )

    class FakeClient:
        def submit_batch_install_heartbeats(self, **kwargs):
            raise AssertionError("batch heartbeat should not be called during cooldown")

    result = maybe_heartbeat_installed_paths(
        trigger="mcp-cli",
        cwd=tmp_path,
        client=FakeClient(),
        now=datetime(2026, 4, 7, 12, 0, tzinfo=UTC),
        cooldown=timedelta(hours=24),
    )

    assert result.status == "skipped"
    assert result.reason == "cooldown_active"


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout,
        httpx.ConnectError,
        httpx.RemoteProtocolError,
        503,
        "<html>Unavailable</html>",
        "null",
        "[]",
        "{}",
        '{"results": null}',
    ],
)
def test_heartbeat_failure_does_not_start_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[httpx.RequestError] | int | str,
) -> None:
    _write_lockfile(tmp_path)
    monkeypatch.setenv("WAYFINDER_PATHS_API_URL", "https://paths.example")
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-instance")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            if isinstance(failure, str):
                return httpx.Response(200, text=failure)
            if isinstance(failure, int):
                return httpx.Response(failure, text="Unavailable")
            raise failure("Heartbeat unavailable", request=request)
        return httpx.Response(200, json={"results": [{"status": "recorded"}]})

    with httpx.Client(transport=httpx.MockTransport(handle), timeout=60) as http:
        client = PathsApiClient(api_base_url="https://paths.example", client=http)
        result = maybe_heartbeat_installed_paths(
            trigger="mcp-server", cwd=tmp_path, client=client
        )

        assert result.status == "error"
        assert result.reason == "request_failed"
        assert result.attempted == 1
        assert result.sent == 0
        assert not (tmp_path / ".wayfinder" / "paths-heartbeat.json").exists()

        recovered = maybe_heartbeat_installed_paths(
            trigger="mcp-server", cwd=tmp_path, client=client
        )

        assert recovered.status == "recorded"
        assert recovered.sent == 1
        assert len(requests) == 2
        assert requests[0].url.path == "/api/v1/paths/installations/heartbeat-batch/"
        assert requests[0].extensions["timeout"] == httpx.Timeout(5).as_dict()
        assert http.timeout == httpx.Timeout(60)


@pytest.mark.parametrize("operation", ["mkdir", "write_text"])
def test_heartbeat_state_write_failure_is_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    _write_lockfile(tmp_path)
    monkeypatch.setenv("WAYFINDER_PATHS_API_URL", "https://paths.example")
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-instance")

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"status": "recorded"}]})

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = PathsApiClient(api_base_url="https://paths.example", client=http)
        with monkeypatch.context() as patch:
            patch.setattr(Path, operation, fail_write)
            result = maybe_heartbeat_installed_paths(
                trigger="mcp-server", cwd=tmp_path, client=client
            )

        assert result.status == "recorded"
        assert result.sent == 1
        assert not (tmp_path / ".wayfinder" / "paths-heartbeat.json").exists()
        recovered = maybe_heartbeat_installed_paths(
            trigger="mcp-server", cwd=tmp_path, client=client
        )
        assert recovered.status == "recorded"
        assert (tmp_path / ".wayfinder" / "paths-heartbeat.json").exists()
