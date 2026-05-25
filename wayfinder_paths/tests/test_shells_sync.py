from __future__ import annotations

import json
from pathlib import Path

from wayfinder_paths.paths.client import PathsApiError
from wayfinder_paths.paths.shells_sync import _collect, sync_shells_inventory


def _write_lockfile(root: Path, paths: dict[str, object]) -> None:
    state_dir = root / ".wayfinder"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "paths.lock.json").write_text(
        json.dumps({"schemaVersion": "0.1", "paths": paths}) + "\n"
    )


class _FakeClient:
    def __init__(
        self, response: dict | None = None, raise_exc: Exception | None = None
    ):
        self.response = response or {"upserted": 0, "deleted": 0}
        self.raise_exc = raise_exc
        self.calls: list[dict[str, object]] = []

    def submit_shells_inventory_sync(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        return self.response


def test_collect_marks_opencode_activation_enabled():
    items = _collect(
        {
            "paths": {
                "alpha": {"version": "0.1.0", "activation": {"host": "opencode"}},
                "beta": {"version": "0.2.0", "activation": {"host": "claude"}},
                "gamma": {"version": "0.3.0"},
                "no-version": {"activation": {"host": "opencode"}},
            }
        }
    )
    by_slug = {item["slug"]: item for item in items}
    assert by_slug["alpha"]["enabled"] is True
    assert by_slug["beta"]["enabled"] is False
    assert by_slug["gamma"]["enabled"] is False
    assert "no-version" not in by_slug
    for item in items:
        assert item["host"] == "opencode"


def test_sync_skips_when_not_in_opencode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENCODE_INSTANCE_ID", raising=False)
    client = _FakeClient()
    result = sync_shells_inventory(trigger="install", cwd=tmp_path, client=client)
    assert result.status == "skipped"
    assert result.reason == "not_in_opencode_instance"
    assert client.calls == []


def test_sync_posts_payload_when_in_opencode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-app-789")
    _write_lockfile(
        tmp_path,
        {"alpha": {"version": "0.1.0", "activation": {"host": "opencode"}}},
    )
    client = _FakeClient(response={"upserted": 1, "deleted": 0})

    result = sync_shells_inventory(trigger="install", cwd=tmp_path, client=client)

    assert result.status == "recorded"
    assert result.upserted == 1
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["app_name"] == "test-app-789"
    assert call["lockfile_present"] is True
    assert call["paths"][0]["slug"] == "alpha"
    assert call["paths"][0]["enabled"] is True


def test_sync_reports_missing_lockfile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-app-789")
    client = _FakeClient()
    result = sync_shells_inventory(trigger="boot", cwd=tmp_path, client=client)
    assert result.status == "recorded"
    assert client.calls[0]["lockfile_present"] is False
    assert client.calls[0]["paths"] == []


def test_sync_swallows_api_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-app-789")
    _write_lockfile(tmp_path, {"alpha": {"version": "0.1.0"}})
    client = _FakeClient(raise_exc=PathsApiError("boom"))
    result = sync_shells_inventory(trigger="activate", cwd=tmp_path, client=client)
    assert result.status == "error"
    assert result.reason == "request_failed"
    assert result.trigger == "activate"
