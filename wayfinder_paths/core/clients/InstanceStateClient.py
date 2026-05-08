from __future__ import annotations

import uuid
from typing import Any

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url, get_opencode_instance_id


class InstanceStateClient(WayfinderClient):
    def _base_url(self) -> str:
        return f"{get_api_base_url()}/opencode/instances/{get_opencode_instance_id()}/context"

    def _opencode_base_url(self) -> str:
        return f"{get_api_base_url()}/opencode"

    async def get_state(self) -> dict[str, Any]:
        resp = await self._authed_request("GET", f"{self._base_url()}/")
        return resp.json()

    async def search_chart_series(
        self,
        *,
        query: str = "",
        kind: str | None = None,
        venue: str | None = None,
        market_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        params = {
            "query": query,
            "kind": kind,
            "venue": venue,
            "market_type": market_type,
            "limit": limit,
        }
        resp = await self._authed_request(
            "GET",
            f"{self._opencode_base_url()}/chart-series/",
            params={k: v for k, v in params.items() if v not in (None, "")},
        )
        return resp.json()

    async def get_frontend_context(self) -> dict[str, Any]:
        state = await self.get_state()
        return state["frontend_context"]

    async def get_chart_id(self) -> str:
        fs = await self.get_frontend_context()
        return fs["chart"]["id"]

    async def patch_chart_workspace(self, workspace: dict[str, Any]) -> dict[str, Any]:
        resp = await self._authed_request(
            "PATCH",
            f"{self._base_url()}/chart_workspace/",
            json=workspace,
        )
        return resp.json()

    async def upsert_workspace_chart(self, chart: dict[str, Any]) -> dict[str, Any]:
        resp = await self._authed_request(
            "POST",
            f"{self._base_url()}/chart_workspace/",
            json=chart,
        )
        return resp.json()

    async def add_workspace_chart_series(
        self, chart_id: str, series: dict[str, Any]
    ) -> dict[str, Any]:
        workspace = await self._get_workspace()
        chart = self._find_workspace_chart(workspace, chart_id)
        if chart is None:
            raise ValueError(f"workspace chart not found: {chart_id}")
        chart_series = chart.setdefault("series", [])
        series_id = str(series.get("id") or "").strip()
        replaced = False
        if series_id:
            for idx, existing in enumerate(chart_series):
                if isinstance(existing, dict) and existing.get("id") == series_id:
                    chart_series[idx] = series
                    replaced = True
                    break
        if not replaced:
            chart_series.append(series)
        return await self.upsert_workspace_chart(chart)

    async def add_workspace_chart_overlay(
        self, chart_id: str, overlay: dict[str, Any]
    ) -> dict[str, Any]:
        workspace = await self._get_workspace()
        chart = self._find_workspace_chart(workspace, chart_id)
        if chart is not None:
            chart.setdefault("overlays", []).append(overlay)
        else:
            workspace.setdefault("defaultAnnotations", {}).setdefault(
                chart_id, []
            ).append(overlay)
        return await self.patch_chart_workspace(self._bump_workspace(workspace))

    async def add_workspace_chart_annotation(
        self,
        chart_id: str,
        type: str,
        config: dict[str, Any],
        annotation_id: str | None = None,
    ) -> dict[str, Any]:
        overlay = {
            "id": annotation_id or str(uuid.uuid4()),
            "type": "annotation",
            "annotation": {"type": type, "config": config},
        }
        return await self.add_workspace_chart_overlay(chart_id, overlay)

    async def clear_chart_workspace(self) -> dict[str, Any]:
        resp = await self._authed_request(
            "DELETE", f"{self._base_url()}/chart_workspace/"
        )
        return resp.json()

    async def _get_workspace(self) -> dict[str, Any]:
        state = await self.get_state()
        workspace = state.get("chart_workspace")
        if not isinstance(workspace, dict):
            return {
                "version": 1,
                "activeChartId": None,
                "charts": [],
                "defaultAnnotations": {},
            }
        workspace.setdefault("charts", [])
        workspace.setdefault("defaultAnnotations", {})
        return workspace

    @staticmethod
    def _find_workspace_chart(
        workspace: dict[str, Any], chart_id: str
    ) -> dict[str, Any] | None:
        for chart in workspace.get("charts") or []:
            if isinstance(chart, dict) and chart.get("id") == chart_id:
                return chart
        return None

    @staticmethod
    def _bump_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
        workspace["version"] = int(workspace.get("version") or 1) + 1
        return workspace


INSTANCE_STATE_CLIENT = InstanceStateClient()
