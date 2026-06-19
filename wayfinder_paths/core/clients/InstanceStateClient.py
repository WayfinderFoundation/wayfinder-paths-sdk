from __future__ import annotations

import uuid
from datetime import datetime
from math import isfinite
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

    async def set_active_market(
        self,
        *,
        query: str | None = None,
        market_id: str | None = None,
        market_type: str | None = None,
        chain_id: int | None = None,
    ) -> dict[str, Any]:
        payload = {
            "query": query,
            "market_id": market_id,
            "market_type": market_type,
            "chain_id": chain_id,
        }
        resp = await self._authed_request(
            "POST",
            f"{self._base_url()}/active_market/",
            json={k: v for k, v in payload.items() if v not in (None, "")},
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
        state = await self.get_state()
        workspace = self._workspace_from_state(state)
        overlay = self._normalize_overlay(overlay)
        warnings = self._event_marker_warnings(overlay)
        chart = self._find_workspace_chart(workspace, chart_id)
        if chart is not None:
            self._upsert_overlay(chart.setdefault("overlays", []), overlay)
        else:
            chart_id = self._resolve_default_chart_id(state, chart_id)
            overlays = workspace.setdefault("defaultAnnotations", {}).setdefault(
                chart_id, []
            )
            self._upsert_overlay(overlays, overlay)
        result = await self.patch_chart_workspace(self._bump_workspace(workspace))
        if warnings and isinstance(result, dict):
            result["warnings"] = warnings
        return result

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
        return self._workspace_from_state(state)

    @staticmethod
    def _workspace_from_state(state: dict[str, Any]) -> dict[str, Any]:
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

    @classmethod
    def _resolve_default_chart_id(cls, state: dict[str, Any], chart_id: str) -> str:
        frontend_context = state.get("frontend_context")
        if not isinstance(frontend_context, dict):
            return chart_id
        chart = frontend_context.get("chart")
        if not isinstance(chart, dict):
            return chart_id
        current_chart_id = str(chart.get("id") or "").strip()
        if not current_chart_id:
            return chart_id

        requested = cls._normalize_chart_ref(chart_id)
        aliases = {
            cls._normalize_chart_ref(value)
            for value in (
                current_chart_id,
                chart.get("market_id"),
                chart.get("symbol"),
                chart.get("feed_id"),
            )
            if value
        }
        symbol = str(chart.get("symbol") or "").strip()
        if symbol:
            aliases.add(cls._normalize_chart_ref(symbol.split("-", 1)[0]))
            aliases.add(cls._normalize_chart_ref(symbol.split("/", 1)[0]))

        return current_chart_id if requested in aliases else chart_id

    @staticmethod
    def _normalize_chart_ref(value: Any) -> str:
        return str(value or "").strip().lower().replace("_", "-").replace("/", "-")

    @staticmethod
    def _normalize_overlay(overlay: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(overlay, dict):
            return overlay
        if overlay.get("type") != "event_markers":
            return overlay

        normalized = dict(overlay)
        markers = normalized.pop("markers", None)
        if not isinstance(normalized.get("data"), list) and isinstance(markers, list):
            normalized["data"] = markers
        return normalized

    @staticmethod
    def _upsert_overlay(overlays: list[Any], overlay: dict[str, Any]) -> None:
        overlay_id = overlay.get("id") if isinstance(overlay, dict) else None
        if not overlay_id:
            overlays.append(overlay)
            return
        for idx, existing in enumerate(overlays):
            if isinstance(existing, dict) and existing.get("id") == overlay_id:
                overlays[idx] = overlay
                return
        overlays.append(overlay)

    @classmethod
    def _event_marker_warnings(cls, overlay: dict[str, Any]) -> list[str]:
        if not isinstance(overlay, dict) or overlay.get("type") != "event_markers":
            return []
        events = overlay.get("data")
        if not isinstance(events, list):
            return []

        has_numeric_time = False
        has_unparseable_time = False
        for event in events:
            if not isinstance(event, dict):
                continue
            raw_time = event.get("time", event.get("ts"))
            if cls._is_numeric_time(raw_time):
                has_numeric_time = True
            elif not cls._is_iso_time(raw_time):
                has_unparseable_time = True

        warnings: list[str] = []
        if has_numeric_time:
            warnings.append(
                "event_markers uses numeric Unix timestamps; prefer ISO-8601 "
                "strings like 2026-05-21T06:00:00Z"
            )
        if has_unparseable_time:
            warnings.append(
                "event_markers contains an event with an unparseable time; "
                "prefer ISO-8601 strings like 2026-05-21T06:00:00Z"
            )
        return warnings

    @staticmethod
    def _is_numeric_time(value: Any) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return isfinite(value)
        if not isinstance(value, str):
            return False
        trimmed = value.strip()
        if not trimmed:
            return False
        try:
            value = float(trimmed)
        except ValueError:
            return False
        return isfinite(value)

    @staticmethod
    def _is_iso_time(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        trimmed = value.strip()
        if not trimmed:
            return False
        try:
            datetime.fromisoformat(trimmed.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True

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
