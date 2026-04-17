from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url, get_opencode_instance_id


class InstanceStateClient(WayfinderClient):
    def _base_url(self) -> str:
        return f"{get_api_base_url()}/v1/opencode/instances/{get_opencode_instance_id()}/context"

    async def get_state(self) -> dict[str, Any]:
        resp = await self._authed_request("GET", f"{self._base_url()}/")
        return resp.json()

    async def get_frontend_context(self) -> dict[str, Any]:
        state = await self.get_state()
        return state["frontend_context"]

    async def get_chart_id(self) -> str:
        fs = await self.get_frontend_context()
        return fs["chart"]["id"]

    async def patch_projection(
        self, chart_id: str, projections: list[dict[str, Any]]
    ) -> dict[str, Any]:
        resp = await self._authed_request(
            "PATCH",
            f"{self._base_url()}/sdk_projection/{chart_id}/",
            json={"projections": projections},
        )
        return resp.json()

    async def add_projection(
        self, chart_id: str, projection: dict[str, Any]
    ) -> dict[str, Any]:
        resp = await self._authed_request(
            "POST", f"{self._base_url()}/sdk_projection/{chart_id}/", json=projection
        )
        return resp.json()

    async def remove_projection(self, chart_id: str, projection_id: str) -> None:
        await self._authed_request(
            "DELETE", f"{self._base_url()}/sdk_projection/{chart_id}/{projection_id}/"
        )

    async def clear_projections(self, chart_id: str) -> dict[str, Any]:
        return await self.patch_projection(chart_id, [])


INSTANCE_STATE_CLIENT = InstanceStateClient()
