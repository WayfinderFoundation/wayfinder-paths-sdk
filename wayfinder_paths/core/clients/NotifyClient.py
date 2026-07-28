from __future__ import annotations

from typing import Any
from uuid import uuid4

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


def normalize_notify_delivery(delivery: str | None) -> str:
    value = str(delivery or "email").strip().lower()
    if value == "text":
        value = "sms"
    if value not in {"email", "sms", "linq"}:
        raise ValueError("delivery must be one of: email, sms, text, linq")
    return value


class NotifyClient(WayfinderClient):
    async def notify(
        self,
        title: str,
        message: str,
        *,
        delivery: str = "email",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        url = f"{get_api_base_url()}/opencode/notify/"
        delivery_normalized = normalize_notify_delivery(delivery)
        payload: dict[str, str] = {"title": title, "message": message}
        if delivery_normalized != "email":
            payload["delivery"] = delivery_normalized
        if delivery_normalized == "linq":
            payload["idempotency_key"] = idempotency_key or str(uuid4())
        response = await self._authed_request("POST", url, json=payload)
        return response.json()


NOTIFY_CLIENT = NotifyClient()
