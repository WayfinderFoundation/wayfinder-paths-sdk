from __future__ import annotations

from typing import Any

import httpx

from wayfinder_paths.core.clients.InstanceStateClient import INSTANCE_STATE_CLIENT
from wayfinder_paths.mcp.utils import err, ok


async def get_frontend_context() -> dict[str, Any]:
    """Read the current frontend UI state.

    Returns what the user is currently viewing: active chart (market, type,
    interval) and any existing SDK projections per chart.
    """
    try:
        return ok(await INSTANCE_STATE_CLIENT.get_state())
    except httpx.HTTPStatusError as exc:
        return err("state_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("state_error", str(exc))


async def add_chart_projection(
    chart_id: str,
    type: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Add a projection (overlay) to a specific chart.

    The chart_id must match the frontend's chart key, e.g. "hl-perp-BTC".
    Use get_ui_state() first to read frontend_context.chart and derive the
    chart_id as "{market_type}-{market_id}".

    Supported types:
      - horizontal_line: config = {price, color?, label?}
      - marker: config = {time (unix sec), position (aboveBar/belowBar),
                          shape (circle/arrowUp/arrowDown), color?, label?}
      - line_series: config = {data: [{time, value}], color?, label?,
                               line_width?}

    Args:
        chart_id: Chart key like "hl-perp-BTC" or "hl-perp-ETH".
        type: Projection type: horizontal_line, marker, or line_series.
        config: Type-specific configuration dict.
    """
    try:
        projection = await INSTANCE_STATE_CLIENT.add_projection(
            chart_id, {"type": type, "config": config}
        )
        return ok(projection)
    except httpx.HTTPStatusError as exc:
        return err("projection_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("projection_error", str(exc))


async def remove_chart_projection(
    chart_id: str,
    projection_id: str,
) -> dict[str, Any]:
    """Remove a projection from a chart by its ID.

    Args:
        chart_id: Chart key like "hl-perp-BTC".
        projection_id: UUID of the projection to remove.
    """
    try:
        await INSTANCE_STATE_CLIENT.remove_projection(chart_id, projection_id)
        return ok({"removed": projection_id})
    except httpx.HTTPStatusError as exc:
        return err("projection_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("projection_error", str(exc))


async def clear_chart_projections(chart_id: str) -> dict[str, Any]:
    """Remove all projections from a chart.

    Args:
        chart_id: Chart key like "hl-perp-BTC".
    """
    try:
        state = await INSTANCE_STATE_CLIENT.clear_projections(chart_id)
        return ok(state)
    except httpx.HTTPStatusError as exc:
        return err("projection_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("projection_error", str(exc))
