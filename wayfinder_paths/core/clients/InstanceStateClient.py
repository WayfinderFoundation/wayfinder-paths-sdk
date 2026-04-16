from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url, get_opencode_instance_id


class InstanceStateClient(WayfinderClient):
    def _base_url(self) -> str:
        return f"{get_api_base_url()}/v1/opencode/instances/{get_opencode_instance_id()}/state"

    async def get_state(self) -> dict[str, Any]:
        resp = await self._authed_request("GET", f"{self._base_url()}/")
        return resp.json()

    async def get_frontend_state(self) -> dict[str, Any]:
        state = await self.get_state()
        return state["frontend_state"]

    async def patch_projection(self, projections: list[dict[str, Any]]) -> dict[str, Any]:
        resp = await self._authed_request(
            "PATCH",
            f"{self._base_url()}/sdk_projection/",
            json={"sdk_projection": projections},
        )
        return resp.json()

    async def add_projection(self, projection: dict[str, Any]) -> dict[str, Any]:
        resp = await self._authed_request(
            "POST", f"{self._base_url()}/sdk_projection/", json=projection
        )
        return resp.json()

    async def remove_projection(self, projection_id: str) -> None:
        await self._authed_request(
            "DELETE", f"{self._base_url()}/sdk_projection/{projection_id}/"
        )

    async def clear_projections(self) -> dict[str, Any]:
        return await self.patch_projection([])


INSTANCE_STATE_CLIENT = InstanceStateClient()
