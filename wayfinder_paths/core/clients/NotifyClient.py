from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


def normalize_notify_delivery(delivery: str | None) -> str:
    value = str(delivery or "email").strip().lower()
    if value in {"text", "imessage", "mobile-thread"}:
        value = "mobile"
    if value not in {"email", "mobile"}:
        raise ValueError("delivery must be one of: email, mobile")
    return value


class NotifyClient(WayfinderClient):
    async def notify(
        self,
        title: str,
        message: str,
    ) -> dict[str, Any]:
        url = f"{get_api_base_url()}/opencode/notify/"
        response = await self._authed_request(
            "POST", url, json={"title": title, "message": message}
        )
        return response.json()

    async def notify_mobile(
        self,
        message: str,
        override: bool = False,
    ) -> dict[str, Any]:
        url = f"{get_api_base_url()}/opencode/sendblue/agent-notify/"
        response = await self._authed_request(
            "POST", url, json={"message": message, "override": override}
        )
        return response.json()


NOTIFY_CLIENT = NotifyClient()
