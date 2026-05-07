from __future__ import annotations

from typing import Any

import httpx

from wayfinder_paths.core.clients.InstanceStateClient import INSTANCE_STATE_CLIENT
from wayfinder_paths.core.config import is_opencode_instance
from wayfinder_paths.mcp.utils import catch_errors, err, ok

_NOT_OPENCODE_ERR = ("not_opencode_instance", "Not running on an OpenCode instance")


@catch_errors
async def shells_get_frontend_context() -> dict[str, Any]:
    """Read the current frontend UI state.

    Returns what the user is currently viewing plus any chart workspace
    created by agent tools.
    """
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    try:
        return ok(await INSTANCE_STATE_CLIENT.get_state())
    except httpx.HTTPStatusError as exc:
        return err("state_http_error", f"HTTP {exc.response.status_code}")


async def shells_create_chart(
    chart_id: str,
    title: str,
    kind: str,
    series: list[dict[str, Any]],
    transforms: list[dict[str, Any]] | None = None,
    overlays: list[dict[str, Any]] | None = None,
    layout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or replace a chart in the user's shell chart workspace.

    Use this when the user asks to show a market, compare assets, chart APYs,
    or create another visual panel. The chart persists with the current
    OpenCode shell until cleared.

    Supported chart kinds:
      - price_candle: primary market price chart. Use source type
        {"type": "market_price", "market_id": "..."}.
      - line: one or more time series.
      - bar: ranked/latest categorical values.
      - table: tabular data.

    Supported source types:
      - market_price: {"type": "market_price", "market_id": "hl-perp-btc"}
      - delta_lab_asset: {"type": "delta_lab_asset", "symbol": "USDC",
        "series": "lending", "venue"?: "...", "basis"?: true}
      - inline: {"type": "inline", "points": [{...}]}

    Supported transforms:
      filter, latest_by, top_n, rebase, pct_change, ratio, spread,
      moving_average. Prefer rebase(base=100) for relative performance.
    """
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    chart = {
        "id": chart_id,
        "title": title,
        "kind": kind,
        "series": series,
        "transforms": transforms or [],
        "overlays": overlays or [],
    }
    if layout:
        chart["layout"] = layout
    try:
        return ok(await INSTANCE_STATE_CLIENT.upsert_workspace_chart(chart))
    except httpx.HTTPStatusError as exc:
        return err("chart_workspace_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("chart_workspace_error", str(exc))


async def shells_set_active_chart(chart_id: str) -> dict[str, Any]:
    """Focus an existing chart in the shell chart workspace."""
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    try:
        state = await INSTANCE_STATE_CLIENT.get_state()
        workspace = state.get("chart_workspace") or {}
        workspace["activeChartId"] = chart_id
        workspace["version"] = int(workspace.get("version") or 1) + 1
        return ok(await INSTANCE_STATE_CLIENT.patch_chart_workspace(workspace))
    except httpx.HTTPStatusError as exc:
        return err("chart_workspace_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("chart_workspace_error", str(exc))


async def shells_add_workspace_chart_series(
    chart_id: str,
    series: dict[str, Any],
) -> dict[str, Any]:
    """Append one data series to an existing chart."""
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    try:
        return ok(
            await INSTANCE_STATE_CLIENT.add_workspace_chart_series(chart_id, series)
        )
    except httpx.HTTPStatusError as exc:
        return err("chart_workspace_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("chart_workspace_error", str(exc))


async def shells_add_workspace_chart_annotation(
    chart_id: str,
    type: str,
    config: dict[str, Any],
    annotation_id: str | None = None,
) -> dict[str, Any]:
    """Add a TradingView annotation to a workspace or default Shells chart.

    Use `shells_get_frontend_context()` to read the current default chart id,
    then pass that chart_id here. If chart_id matches an agent-created
    workspace chart, the annotation attaches there. Otherwise it attaches to
    the default live chart for that id.

    Supported annotation types:
      - horizontal_line: config = {price, color?, label?}
      - vertical_line: config = {time, color?, label?}
      - marker: config = {time, price?, shape?, color?}
      - range: config = {from_time?, to_time?, from_price, to_price, color?}
      - text_label: config = {time, price, text, color?}
      - trend: config = {from: {time, price}, to: {time, price}, color?, label?}
    """
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    try:
        return ok(
            await INSTANCE_STATE_CLIENT.add_workspace_chart_annotation(
                chart_id=chart_id,
                type=type,
                config=config,
                annotation_id=annotation_id,
            )
        )
    except httpx.HTTPStatusError as exc:
        return err("chart_workspace_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("chart_workspace_error", str(exc))


async def shells_add_workspace_chart_overlay(
    chart_id: str,
    overlay: dict[str, Any],
) -> dict[str, Any]:
    """Append a raw overlay or event marker set to a workspace or default chart."""
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    try:
        return ok(
            await INSTANCE_STATE_CLIENT.add_workspace_chart_overlay(chart_id, overlay)
        )
    except httpx.HTTPStatusError as exc:
        return err("chart_workspace_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("chart_workspace_error", str(exc))


async def shells_clear_chart_workspace() -> dict[str, Any]:
    """Remove all agent-created workspace charts."""
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    try:
        return ok(await INSTANCE_STATE_CLIENT.clear_chart_workspace())
    except httpx.HTTPStatusError as exc:
        return err("chart_workspace_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("chart_workspace_error", str(exc))
