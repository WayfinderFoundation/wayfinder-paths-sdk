from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
