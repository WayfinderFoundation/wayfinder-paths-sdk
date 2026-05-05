from __future__ import annotations

from typing import Any

import httpx

from wayfinder_paths.core.clients.InstanceStateClient import INSTANCE_STATE_CLIENT
from wayfinder_paths.core.config import is_opencode_instance
from wayfinder_paths.mcp.utils import err, ok

_NOT_OPENCODE_ERR = ("not_opencode_instance", "Not running on an OpenCode instance")


async def shells_get_frontend_context() -> dict[str, Any]:
    """Read the current frontend UI state.

    Returns what the user is currently viewing: active chart (market, type,
    interval) and any existing SDK projections per chart.
    """
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    try:
        return ok(await INSTANCE_STATE_CLIENT.get_state())
    except httpx.HTTPStatusError as exc:
        return err("state_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("state_error", str(exc))


async def shells_add_chart_projection(
    chart_id: str,
    type: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Add a projection (overlay) to a specific chart.

    The chart_id is available at frontend_context.chart.id (e.g. "hl-perp-BTC").
    Call get_frontend_context() first to read it.

    Supported types (engine-agnostic; the FE renderer maps to TradingView shapes):
      - horizontal_line: config = {price, color?, label?}
      - vertical_line:   config = {time (unix sec), color?, label?}
      - marker:          config = {time, price?, shape? (arrow_up /
                                   arrow_down / flag / icon / emoji), color?}
      - range:           config = {from_time?, to_time?, from_price, to_price,
                                   color?}
      - text_label:      config = {time, price, text, color?}
      - trend:           config = {from: {time, price}, to: {time, price},
                                   color?, label?}

    Notes:
      - `marker` does not accept a `label` — TV's marker shapes auto-generate
        text. Use `text_label` for an annotated point.
      - All `time` values are unix seconds.
      - Adding a chart projection emits a state-changed notification; the FE
        renders within one poll cycle (~5s) or sooner if the SSE stream is
        connected.

    Args:
        chart_id: Chart key like "hl-perp-BTC" or "hl-perp-ETH".
        type: Projection type (see list above).
        config: Type-specific configuration dict.
    """
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    try:
        projection = await INSTANCE_STATE_CLIENT.add_projection(
            chart_id, {"type": type, "config": config}
        )
        return ok(projection)
    except httpx.HTTPStatusError as exc:
        return err("projection_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("projection_error", str(exc))


async def shells_remove_chart_projection(
    chart_id: str,
    projection_id: str,
) -> dict[str, Any]:
    """Remove a projection from a chart by its ID.

    Args:
        chart_id: Chart key like "hl-perp-BTC".
        projection_id: UUID of the projection to remove.
    """
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    try:
        await INSTANCE_STATE_CLIENT.remove_projection(chart_id, projection_id)
        return ok({"removed": projection_id})
    except httpx.HTTPStatusError as exc:
        return err("projection_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("projection_error", str(exc))


async def shells_clear_chart_projections(chart_id: str) -> dict[str, Any]:
    """Remove all projections from a chart.

    Args:
        chart_id: Chart key like "hl-perp-BTC".
    """
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    try:
        state = await INSTANCE_STATE_CLIENT.clear_projections(chart_id)
        return ok(state)
    except httpx.HTTPStatusError as exc:
        return err("projection_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("projection_error", str(exc))
