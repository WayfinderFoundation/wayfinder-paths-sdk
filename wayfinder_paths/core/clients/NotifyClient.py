from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class NotifyClient(WayfinderClient):
    async def notify(self, title: str, message: str) -> dict[str, Any]:
        url = f"{get_api_base_url()}/v1/opencode/notify/"
        response = await self._authed_request(
            "POST", url, json={"title": title, "message": message}
        )
        return response.json()


NOTIFY_CLIENT = NotifyClient()
