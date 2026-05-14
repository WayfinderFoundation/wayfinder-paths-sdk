from __future__ import annotations

import importlib
from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.core.clients.NotifyClient import (
    NotifyClient,
    normalize_notify_delivery,
)


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


notify_client_module = importlib.import_module(
    "wayfinder_paths.core.clients.NotifyClient"
)
notify_tool_module = importlib.import_module("wayfinder_paths.mcp.tools.notify")


@pytest.mark.asyncio
async def test_notify_client_default_email_payload_omits_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        notify_client_module,
        "get_api_base_url",
        lambda: "https://example.com/api/v1",
    )
    client = NotifyClient()
    client._authed_request = AsyncMock(return_value=_Response({"sent": True}))  # type: ignore[method-assign]

    out = await client.notify(title="Alert", message="Body")

    assert out == {"sent": True}
    client._authed_request.assert_awaited_once()
    args, kwargs = client._authed_request.await_args
    assert args == ("POST", "https://example.com/api/v1/opencode/notify/")
    assert kwargs["json"] == {"title": "Alert", "message": "Body"}


@pytest.mark.asyncio
async def test_notify_client_text_alias_requests_sms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        notify_client_module,
        "get_api_base_url",
        lambda: "https://example.com/api/v1",
    )
    client = NotifyClient()
    client._authed_request = AsyncMock(return_value=_Response({"sent": True}))  # type: ignore[method-assign]

    await client.notify(title="Alert", message="Body", delivery="text")

    assert client._authed_request.await_args.kwargs["json"] == {
        "title": "Alert",
        "message": "Body",
        "delivery": "sms",
    }


def test_normalize_notify_delivery_rejects_unknown_delivery() -> None:
    with pytest.raises(ValueError):
        normalize_notify_delivery("fax")


@pytest.mark.asyncio
async def test_shells_notify_passes_sms_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = AsyncMock()
    fake_client.notify.return_value = {"sent": True, "delivery": "sms"}
    monkeypatch.setattr(notify_tool_module, "NOTIFY_CLIENT", fake_client)

    out = await notify_tool_module.shells_notify("Alert", "Body", delivery="text")

    assert out == {"ok": True, "result": {"sent": True, "delivery": "sms"}}
    fake_client.notify.assert_awaited_once_with(
        title="Alert",
        message="Body",
        delivery="sms",
    )


@pytest.mark.asyncio
async def test_shells_notify_rejects_invalid_delivery() -> None:
    out = await notify_tool_module.shells_notify("Alert", "Body", delivery="fax")

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_request"
