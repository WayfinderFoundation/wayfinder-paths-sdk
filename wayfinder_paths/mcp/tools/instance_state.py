from __future__ import annotations

import json
from typing import Any

import httpx

from wayfinder_paths.core.clients.InstanceStateClient import INSTANCE_STATE_CLIENT
from wayfinder_paths.core.config import is_opencode_instance
from wayfinder_paths.mcp.utils import catch_errors, err, ok

_NOT_OPENCODE_ERR = ("not_opencode_instance", "Not running on an OpenCode instance")


def _http_error_message(exc: httpx.HTTPStatusError) -> tuple[str, Any | None]:
    response = exc.response
    details: Any | None = None
    try:
        details = response.json()
    except json.JSONDecodeError:
        details = response.text
    if isinstance(details, dict):
        message = str(
            details.get("error") or details.get("detail") or response.reason_phrase
        )
    else:
        message = str(details or response.reason_phrase)
    return f"HTTP {response.status_code}: {message}", details


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


async def shells_search_chart_series(
    query: str,
    kind: str | None = None,
    venue: str | None = None,
    market_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search backend-supported chart datasets before creating a chart.

    Use this first for charts that need market prices, funding rates, APYs,
    lending rates, Boros/Pendle data, or DeFiLlama yield snapshots. Search by
    natural intent/assets first, then inspect each result's `kind`, `shape`,
    and `supported_chart_kinds` before choosing it. Do not pass `kind` unless
    the user clearly requested a data family like funding/yield or you need to
    narrow a large result set. For asset price/performance charts, prefer the
    returned Hyperliquid perp datasets over spot/fallback sources unless the
    user explicitly asks for spot. Returned results include the exact `source`
    object and shape metadata for chart series specs.
    """
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    try:
        return ok(
            await INSTANCE_STATE_CLIENT.search_chart_series(
                query=query,
                kind=kind,
                venue=venue,
                market_type=market_type,
                limit=limit,
            )
        )
    except httpx.HTTPStatusError as exc:
        return err("chart_series_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("chart_series_error", str(exc))


async def shells_set_active_market(
    query: str | None = None,
    market_id: str | None = None,
    market_type: str | None = None,
    chain_id: int | None = None,
    clear_workspace: bool = True,
) -> dict[str, Any]:
    """Switch the default Shells chart and trading context to one market.

    Use this for requests like "show AAVE", "switch to PENGU perp", or
    "open this Polymarket market". This updates the live chart, order book,
    trades, and trade ticket together. Prefer this over `shells_create_chart`
    when the user wants a single tradable token, perp, spot, or prediction
    market rather than a custom visual pane.

    Args:
      query: Natural search text, e.g. "AAVE perp" or "Timberwolves".
      market_id: Exact Shells market id if known, e.g. "hl-perp-btc".
      market_type: Optional narrowing: hl-perp, hl-spot, onchain-spot,
        polymarket.
      chain_id: Optional EVM chain id for onchain spot resolution.
      clear_workspace: Set false only if an existing custom pane should stay
        active while the trading context changes.
    """
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    try:
        return ok(
            await INSTANCE_STATE_CLIENT.set_active_market(
                query=query,
                market_id=market_id,
                market_type=market_type,
                chain_id=chain_id,
                clear_workspace=clear_workspace,
            )
        )
    except httpx.HTTPStatusError as exc:
        message, details = _http_error_message(exc)
        return err("active_market_http_error", message, details)
    except Exception as exc:  # noqa: BLE001
        return err("active_market_error", str(exc))


async def shells_create_chart(
    chart_id: str,
    title: str,
    kind: str,
    series: list[dict[str, Any]],
    transforms: list[dict[str, Any]] | None = None,
    overlays: list[dict[str, Any]] | None = None,
    lookback_days: int | None = None,
    limit: int | None = None,
    layout: dict[str, Any] | None = None,
    context_market_id: str | None = None,
) -> dict[str, Any]:
    """Create or replace a chart in the user's shell chart workspace.

    Use this when the user asks to show a market, compare assets, chart APYs,
    or create another visual panel. The backend validates renderability before
    saving; if this returns `ok: false`, revise the source/kind instead of
    telling the user the chart is ready. The chart persists with the current
    OpenCode shell until cleared.

    Supported chart kinds:
      - price_candle: primary market price chart. Use source type
        {"type": "market_price", "market_id": "..."} or a dataset_series
        returned by `shells_search_chart_series` for Hyperliquid perp prices.
      - line: one or more time series.
      - bar: ranked/latest categorical values.
      - table: tabular data.

    Supported source types:
      - market_price: {"type": "market_price", "market_id": "hl-perp-btc"}
      - dataset_series: use `shells_search_chart_series` and copy the returned
        source object. Preferred for assets, funding, APYs, Delta Lab registry
        series, DeFiLlama snapshots, and CoinGecko fallback prices.
      - delta_lab_asset: {"type": "delta_lab_asset", "symbol": "USDC",
        "series": "lending", "venue"?: "...", "basis"?: true}. Legacy
        fallback only; use the dataset_series result when search returns one.
      - inline: {"type": "inline", "points": [{...}]}

    Supported transforms:
      filter, latest_by, top_n, rebase, pct_change, scale, multiply, ratio,
      spread, moving_average. Prefer rebase(base=100) for relative performance.
      Put transforms on a single series when only that data needs conversion,
      e.g. annualize hourly funding with
      {"type": "scale", "factor": 8760, "unit": "apy"}. Use chart-level
      transforms only when all series should be transformed together.

    Series can include optional `axis` ("left" or "right") and `color`.
    Keep comparable units on the same axis; use a right axis for unrelated
    units only when the user asks to overlay them.

    For tradable perp charts, set `context_market_id` (for example
    "hl-perp-pengu"). The shell will keep the workspace chart active while
    switching the order book, trades, and trade ticket to that market.

    Use lookback_days for requested windows. Examples: 30 for one month,
    90 for three months, 365 for one year.
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
    if lookback_days:
        chart["lookback_days"] = lookback_days
    if limit:
        chart["limit"] = limit
    if layout:
        chart["layout"] = layout
    if context_market_id:
        chart["context_market_id"] = context_market_id
    try:
        return ok(await INSTANCE_STATE_CLIENT.upsert_workspace_chart(chart))
    except httpx.HTTPStatusError as exc:
        message, details = _http_error_message(exc)
        return err("chart_workspace_http_error", message, details)
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
    """Add or replace one data series on an existing chart.

    If `series.id` already exists on the chart, this replaces that series and
    re-validates the full chart before saving. Use this to fix scale, axis,
    color, or source choices after a chart was created.
    """
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
        Use this for date-only events. `time` may be Unix seconds or an ISO
        date string like "2026-04-19".
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
