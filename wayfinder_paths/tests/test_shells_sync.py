from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from wayfinder_paths.paths.cli import (
    _runtime_reload_intent_for_host,
    _sync_after_path_mutation,
)
from wayfinder_paths.paths.client import PathsApiClient, PathsApiError
from wayfinder_paths.paths.shells_sync import (
    ShellsInventorySyncResult,
    _collect,
    sync_shells_inventory,
)


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
    monkeypatch.delenv("WAYFINDER_SHELLS_RUNTIME_RELOAD_INTENT", raising=False)
    _write_lockfile(
        tmp_path,
        {"alpha": {"version": "0.1.0", "activation": {"host": "opencode"}}},
    )
    client = _FakeClient(response={"upserted": 1, "deleted": 0})

    result = sync_shells_inventory(
        trigger="install",
        runtime_reload_intent="restart",
        changed_slugs=["alpha"],
        cwd=tmp_path,
        client=client,
    )

    assert result.status == "recorded"
    assert result.upserted == 1
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["app_name"] == "test-app-789"
    assert call["lockfile_present"] is True
    assert call["paths"][0]["slug"] == "alpha"
    assert call["paths"][0]["enabled"] is True
    assert call["trigger"] == "install"
    assert call["source"] == "direct"
    assert call["runtime_reload_intent"] == "restart"
    assert call["changed_slugs"] == ["alpha"]


def test_sync_reports_missing_lockfile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-app-789")
    monkeypatch.setenv("WAYFINDER_SHELLS_RUNTIME_RELOAD_INTENT", "fresh")
    client = _FakeClient()
    result = sync_shells_inventory(trigger="boot", cwd=tmp_path, client=client)
    assert result.status == "recorded"
    assert client.calls[0]["lockfile_present"] is False
    assert client.calls[0]["paths"] == []
    assert client.calls[0]["source"] == "boot"
    assert client.calls[0]["runtime_reload_intent"] == "fresh"


def test_sync_can_be_suppressed_during_boot_activation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-app-789")
    monkeypatch.setenv("WAYFINDER_SKIP_SHELLS_INVENTORY_SYNC", "1")
    client = _FakeClient()

    result = sync_shells_inventory(trigger="activate", cwd=tmp_path, client=client)

    assert result.status == "skipped"
    assert result.reason == "suppressed"
    assert client.calls == []


def test_sync_swallows_api_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-app-789")
    _write_lockfile(tmp_path, {"alpha": {"version": "0.1.0"}})
    client = _FakeClient(raise_exc=PathsApiError("boom"))
    result = sync_shells_inventory(trigger="activate", cwd=tmp_path, client=client)
    assert result.status == "error"
    assert result.reason == "request_failed"
    assert result.trigger == "activate"


def test_sync_recognizes_legacy_backend_restart_response(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-app-789")
    client = _FakeClient(response={"runtime_restarted": True})

    result = sync_shells_inventory(
        trigger="activate",
        runtime_reload_intent="restart",
        cwd=tmp_path,
        client=client,
    )

    assert result.runtime_reload_status == "restarted"


def test_client_keeps_legacy_payload_shape_when_new_fields_are_omitted() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"upserted": 0, "deleted": 0})

    client = PathsApiClient(
        api_base_url="https://paths.example",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.submit_shells_inventory_sync(
        app_name="test-app",
        lockfile_present=True,
        paths=[],
    )

    assert requests == [{"lockfile_present": True, "paths": []}]


def test_client_translates_network_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = PathsApiClient(
        api_base_url="https://paths.example",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(PathsApiError, match="Shells inventory sync failed"):
        client.submit_shells_inventory_sync(
            app_name="test-app",
            lockfile_present=True,
            paths=[],
        )


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("opencode", "restart"),
        ("OpenCode", "restart"),
        ("claude", "preserve"),
        (None, "preserve"),
    ],
)
def test_runtime_reload_intent_follows_activation_host(host, expected) -> None:
    assert _runtime_reload_intent_for_host(host) == expected


def test_local_mutation_does_not_change_cli_result(monkeypatch) -> None:
    monkeypatch.setattr(
        "wayfinder_paths.paths.cli.sync_shells_inventory",
        lambda **_kwargs: ShellsInventorySyncResult(
            status="skipped",
            reason="not_in_opencode_instance",
        ),
    )
    result: dict[str, object] = {"installed": True}

    _sync_after_path_mutation(
        result,
        trigger="install",
        runtime_reload_intent="preserve",
        changed_slugs=["example"],
    )

    assert result == {"installed": True}


def test_pending_reload_is_attached_as_a_cli_warning(monkeypatch) -> None:
    monkeypatch.setattr(
        "wayfinder_paths.paths.cli.sync_shells_inventory",
        lambda **_kwargs: ShellsInventorySyncResult(
            status="recorded",
            reason="sent",
            runtime_reload_intent="restart",
            runtime_reload_status="pending",
        ),
    )
    result: dict[str, object] = {}

    _sync_after_path_mutation(
        result,
        trigger="activate",
        runtime_reload_intent="restart",
        changed_slugs=[],
    )

    assert result["runtime_restart_pending"] is True
    assert "Restart the Shell runtime" in str(result["warnings"])


def test_old_backend_without_reload_status_requires_restart_warning(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "wayfinder_paths.paths.cli.sync_shells_inventory",
        lambda **_kwargs: ShellsInventorySyncResult(
            status="recorded",
            reason="sent",
            runtime_reload_intent="restart",
        ),
    )
    result: dict[str, object] = {}

    _sync_after_path_mutation(
        result,
        trigger="remove",
        runtime_reload_intent="restart",
        changed_slugs=["legacy-path"],
    )

    assert result["runtime_restart_pending"] is True
