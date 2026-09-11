from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import click
import httpx
import pytest

from wayfinder_paths.mcp import cli as mcp_cli
from wayfinder_paths.mcp import server as mcp_server
from wayfinder_paths.paths import heartbeat
from wayfinder_paths.paths.client import PathsApiClient


def _make_fake_group() -> click.Group:
    @click.group()
    def group() -> None:
        pass

    @group.command(name="resource")
    @click.argument("uri", required=False)
    def resource_cmd(uri: str | None = None) -> None:
        del uri

    return group


def test_mcp_cli_main_triggers_heartbeat_for_resource_command(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(mcp_cli, "build_cli", lambda _mcp: _make_fake_group())
    monkeypatch.setattr(
        mcp_cli,
        "maybe_heartbeat_installed_paths",
        lambda **kwargs: calls.append(str(kwargs["trigger"])),
    )
    monkeypatch.setattr(mcp_cli, "path_cli", click.Group("path"))
    monkeypatch.setattr(mcp_cli, "runner_cli", click.Group("runner"))
    monkeypatch.setattr(
        mcp_cli.sys, "argv", ["wayfinder", "resource", "wayfinder://foo"]
    )

    with pytest.raises(SystemExit) as exc:
        mcp_cli.main()

    assert exc.value.code == 0
    assert calls == ["mcp-cli"]


def test_mcp_cli_main_skips_heartbeat_for_path_command(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(mcp_cli, "build_cli", lambda _mcp: _make_fake_group())
    monkeypatch.setattr(
        mcp_cli,
        "maybe_heartbeat_installed_paths",
        lambda **kwargs: calls.append(str(kwargs["trigger"])),
    )

    @click.group(name="path")
    def fake_path() -> None:
        pass

    @fake_path.command(name="version")
    def version_cmd() -> None:
        pass

    monkeypatch.setattr(mcp_cli, "path_cli", fake_path)
    monkeypatch.setattr(mcp_cli, "runner_cli", click.Group("runner"))
    monkeypatch.setattr(mcp_cli.sys, "argv", ["wayfinder", "path", "version"])

    with pytest.raises(SystemExit) as exc:
        mcp_cli.main()

    assert exc.value.code == 0
    assert calls == []


def test_mcp_server_main_triggers_heartbeat_once(monkeypatch) -> None:
    calls: list[str] = []
    runs: list[tuple[str, int, str]] = []

    class FakeMCP:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def run(self, *, transport: str) -> None:
            runs.append((self.host, self.port, transport))

    monkeypatch.setattr(
        mcp_server,
        "maybe_heartbeat_installed_paths",
        lambda **kwargs: calls.append(str(kwargs["trigger"])),
    )
    monkeypatch.setattr(
        mcp_server,
        "build_mcp",
        lambda *, host, port: FakeMCP(host, port),
    )

    mcp_server.main([])

    assert calls == ["mcp-server"]
    assert runs == [("127.0.0.1", 8000, "stdio")]


def test_mcp_server_main_accepts_profile_host_port_and_transport(monkeypatch) -> None:
    calls: list[str] = []
    runs: list[tuple[str, int, str]] = []

    class FakeMCP:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def run(self, *, transport: str) -> None:
            runs.append((self.host, self.port, transport))

    monkeypatch.setattr(
        mcp_server,
        "maybe_heartbeat_installed_paths",
        lambda **kwargs: calls.append(str(kwargs["trigger"])),
    )
    monkeypatch.setattr(
        mcp_server,
        "build_mcp",
        lambda *, host, port: FakeMCP(host, port),
    )

    mcp_server.main(
        [
            "--host",
            "0.0.0.0",
            "--port",
            "8123",
            "--transport",
            "streamable-http",
        ]
    )

    assert calls == ["mcp-server"]
    assert runs == [("0.0.0.0", 8123, "streamable-http")]


def test_mcp_server_starts_after_heartbeat_read_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WAYFINDER_PATHS_API_URL", "https://paths.example")
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-instance")
    state_dir = tmp_path / ".wayfinder"
    state_dir.mkdir()
    (state_dir / "paths.lock.json").write_text(
        json.dumps(
            {
                "paths": {
                    "demo": {"installation_id": "install-1", "heartbeat_token": "test"}
                }
            }
        )
    )
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("The read operation timed out", request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = PathsApiClient(api_base_url="https://paths.example", client=http)
        monkeypatch.setattr(heartbeat, "PathsApiClient", lambda: client)
        server = Mock()
        monkeypatch.setattr(mcp_server, "build_mcp", Mock(return_value=server))

        mcp_server.main(["--transport", "streamable-http"])

    assert len(requests) == 1
    server.run.assert_called_once_with(transport="streamable-http")
    assert not (state_dir / "paths-heartbeat.json").exists()
