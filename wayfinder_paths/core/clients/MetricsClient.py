from __future__ import annotations

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class MetricsClient(WayfinderClient):
    async def report_tool(self, *, tool: str, success: bool, code: str) -> None:
        url = f"{get_api_base_url()}/metric-reporting/"
        await self._authed_request(
            "POST",
            url,
            json={"tool": tool, "success": success, "code": code},
        )


METRICS_CLIENT = MetricsClient()
