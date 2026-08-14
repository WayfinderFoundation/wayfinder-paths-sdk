from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class NotifyClient(WayfinderClient):
    async def notify(
        self,
        title: str,
        message: str,
        delivery: str = "email",
        override: bool = True,
    ) -> dict[str, Any]:
        """Direct callers (scripts, monitors, jobs) are trusted senders —
        override defaults on, so quiet hours and the frequency budget don't
        gate them. The agent's notification tool passes override=False and
        goes through the warning handshake instead. Near-duplicate rejection
        applies to everyone."""
        url = f"{get_api_base_url()}/opencode/notify/"
        payload: dict[str, Any] = {"title": title, "message": message}
        if delivery != "email":
            payload["delivery"] = delivery
            if override:
                payload["override"] = True
        response = await self._authed_request("POST", url, json=payload)
        return response.json()


NOTIFY_CLIENT = NotifyClient()
