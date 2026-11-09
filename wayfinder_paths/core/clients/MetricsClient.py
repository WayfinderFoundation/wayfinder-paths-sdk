from __future__ import annotations

from wayfinder_paths.core.api_origin import UntrustedApiOriginError
from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class MetricsClient(WayfinderClient):
    async def report_tool(
        self, *, tool: str, success: bool, code: str, duration_ms: float
    ) -> None:
        try:
            url = f"{get_api_base_url()}/metric-reporting/"
        except UntrustedApiOriginError:
            # Best-effort: the tool's own result already reports the bad URL.
            return
        await self._authed_request(
            "POST",
            url,
            json={
                "tool": tool,
                "success": success,
                "code": code,
                "duration_ms": duration_ms,
            },
        )


METRICS_CLIENT = MetricsClient()
