from typing import Any

import pytest

import wayfinder_paths.core.clients.InstanceStateClient as instance_state_module
from wayfinder_paths.core.clients.InstanceStateClient import InstanceStateClient
from wayfinder_paths.mcp.tools import instance_state


class FakeResponse:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def json(self) -> dict[str, Any]:
        return self.payload


@pytest.mark.asyncio
async def test_instance_state_client_lists_shell_paths(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def fake_request(self, method: str, url: str, **kwargs):
        calls.append((method, url))
        return FakeResponse(
            {
                "paths": [{"slug": "shell-ready"}],
                "compatibility": {
                    "compatible_count": 1,
                    "total_bonded_count": 2,
                    "incompatible_bonded_count": 1,
                },
            }
        )

    monkeypatch.setattr(instance_state_module, "get_api_base_url", lambda: "https://api.test")
    monkeypatch.setattr(
        instance_state_module,
        "get_opencode_instance_id",
        lambda: "shell-app",
    )
    monkeypatch.setattr(InstanceStateClient, "_authed_request", fake_request)

    payload = await InstanceStateClient().list_paths()

    assert calls == [("GET", "https://api.test/opencode/instances/shell-app/paths/")]
    assert payload["paths"] == [{"slug": "shell-ready"}]
    assert payload["compatibility"]["incompatible_bonded_count"] == 1


@pytest.mark.asyncio
async def test_shells_list_paths_tool_requires_opencode(monkeypatch):
    monkeypatch.delenv("OPENCODE_INSTANCE_ID", raising=False)

    payload = await instance_state.shells_list_paths()

    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_opencode_instance"


@pytest.mark.asyncio
async def test_shells_list_paths_tool_returns_backend_inventory(monkeypatch):
    class FakeClient:
        async def list_paths(self):
            return {
                "paths": [{"slug": "shell-ready"}],
                "compatibility": {
                    "compatible_count": 1,
                    "total_bonded_count": 2,
                    "incompatible_bonded_count": 1,
                },
            }

    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "shell-app")
    monkeypatch.setattr(instance_state, "INSTANCE_STATE_CLIENT", FakeClient())

    payload = await instance_state.shells_list_paths()

    assert payload == {
        "ok": True,
        "result": {
            "paths": [{"slug": "shell-ready"}],
            "compatibility": {
                "compatible_count": 1,
                "total_bonded_count": 2,
                "incompatible_bonded_count": 1,
            },
        },
    }
