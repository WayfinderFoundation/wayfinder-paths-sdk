from __future__ import annotations

import click
import pytest

from wayfinder_paths.mcp import cli as mcp_cli
from wayfinder_paths.mcp import server as mcp_server


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
    runs: list[tuple[str, str, int, str]] = []

    class FakeMCP:
        def __init__(self, profile: str, host: str, port: int) -> None:
            self.profile = profile
            self.host = host
            self.port = port

        def run(self, *, transport: str) -> None:
            runs.append((self.profile, self.host, self.port, transport))

    monkeypatch.setattr(
        mcp_server,
        "maybe_heartbeat_installed_paths",
        lambda **kwargs: calls.append(str(kwargs["trigger"])),
    )
    monkeypatch.setattr(
        mcp_server,
        "build_mcp",
        lambda profile, *, host, port: FakeMCP(profile, host, port),
    )

    mcp_server.main([])

    assert calls == ["mcp-server:all"]
    assert runs == [("all", "127.0.0.1", 8000, "stdio")]


def test_mcp_server_main_accepts_profile_and_port(monkeypatch) -> None:
    calls: list[str] = []
    runs: list[tuple[str, str, int, str]] = []

    class FakeMCP:
        def __init__(self, profile: str, host: str, port: int) -> None:
            self.profile = profile
            self.host = host
            self.port = port

        def run(self, *, transport: str) -> None:
            runs.append((self.profile, self.host, self.port, transport))

    monkeypatch.setattr(
        mcp_server,
        "maybe_heartbeat_installed_paths",
        lambda **kwargs: calls.append(str(kwargs["trigger"])),
    )
    monkeypatch.setattr(
        mcp_server,
        "build_mcp",
        lambda profile, *, host, port: FakeMCP(profile, host, port),
    )

    mcp_server.main(
        [
            "--profile",
            "research",
            "--host",
            "0.0.0.0",
            "--port",
            "8123",
            "--transport",
            "streamable-http",
        ]
    )

    assert calls == ["mcp-server:research"]
    assert runs == [("research", "0.0.0.0", 8123, "streamable-http")]
