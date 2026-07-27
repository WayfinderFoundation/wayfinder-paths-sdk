from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from wayfinder_paths.core.clients.InstanceStateClient import INSTANCE_STATE_CLIENT
from wayfinder_paths.core.config import is_opencode_instance
from wayfinder_paths.mcp.arg_validation import MCPArgumentError, normalize_enum
from wayfinder_paths.mcp.tool_annotations import (
    ChartIndicatorList,
    ChartIndicators,
    ChartKind,
    ChartSeriesSpecs,
    ChartTransforms,
)
from wayfinder_paths.mcp.utils import catch_errors, err, ok, repo_root

_NOT_OPENCODE_ERR = ("not_opencode_instance", "Not running on an OpenCode instance")
_VISUAL_SPEC_DIR = Path(".wayfinder_runs") / "visual_specs"
_RATE_PERCENT_FIELDS = {
    "implied_apy",
    "underlying_apy",
    "supply_apr",
    "borrow_apr",
    "net_supply_apy",
    "net_borrow_apy",
    "fixed_rate_mark",
    "floating_rate_oracle",
    "apy",
    "apy_base",
    "apy_base_7d",
    "reward_apr",
}
_CHART_KINDS = {"price_candle", "line", "bar", "table"}
_INDICATOR_NAMES = {
    "atr",
    "bollinger",
    "ema",
    "macd",
    "rsi",
    "sma",
    "stochastic",
    "supertrend",
    "volume",
    "vwap",
}


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


def _normalizes_scale(transforms: list[Any]) -> bool:
    return any(
        isinstance(t, dict)
        and str(t.get("type") or "").strip().lower() in {"scale", "multiply"}
        for t in transforms
    )


def _normalize_chart_series_for_display(
    series: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add safe display transforms for common Delta Lab decimal rate fields.

    Agents still should provide explicit transforms, but this prevents raw
    decimal APYs such as 0.12 from being rendered or summarized as 0.12%.
    """

    normalized: list[dict[str, Any]] = []
    for item in series:
        if not isinstance(item, dict):
            normalized.append(item)
            continue
        y_field = str(item.get("y") or "").strip()
        transforms = (
            item.get("transforms") if isinstance(item.get("transforms"), list) else []
        )
        if y_field == "funding_rate" and not _normalizes_scale(transforms):
            transforms = [
                *transforms,
                {
                    "type": "scale",
                    "factor": 876000,
                    "unit": "%",
                    "label_suffix": "(annualized %)",
                },
            ]
            item = {**item, "unit": "%", "transforms": transforms}
        elif y_field in _RATE_PERCENT_FIELDS and not _normalizes_scale(transforms):
            transforms = [
                *transforms,
                {
                    "type": "scale",
                    "factor": 100,
                    "unit": "%",
                    "label_suffix": "(%)",
                },
            ]
            item = {**item, "unit": "%", "transforms": transforms}
        normalized.append(item)
    return normalized


def _validate_chart_series(series: Any) -> list[dict[str, Any]]:
    if not isinstance(series, list) or not series:
        raise MCPArgumentError(
            "series must be a non-empty list of chart series specs",
            field="series",
            received=series,
            suggested_arguments={
                "series": [
                    {
                        "id": "series-1",
                        "source": {"type": "inline", "points": []},
                    }
                ]
            },
        )
    for index, item in enumerate(series):
        field = f"series[{index}]"
        if not isinstance(item, dict):
            raise MCPArgumentError(
                f"{field} must be an object",
                field=field,
                received=item,
            )
        if not str(item.get("id") or "").strip():
            raise MCPArgumentError(
                f"{field}.id is required",
                field=f"{field}.id",
                received=item.get("id"),
                suggested_arguments={"id": f"series-{index + 1}"},
            )
        if not isinstance(item.get("source"), dict):
            raise MCPArgumentError(
                f"{field}.source must be an object copied from "
                "visual_search_chart_series or an inline source",
                field=f"{field}.source",
                received=item.get("source"),
                suggested_arguments={
                    "source": {"type": "inline", "points": []},
                },
            )
    return series


def _validate_chart_transforms(
    transforms: Any,
    *,
    field_name: str = "transforms",
) -> None:
    if transforms is None:
        return
    if not isinstance(transforms, list) or any(
        not isinstance(item, dict) for item in transforms
    ):
        raise MCPArgumentError(
            f"{field_name} must be a list of transform objects",
            field=field_name,
            received=transforms,
            suggested_arguments={field_name: []},
        )


def _validate_indicators(indicators: Any) -> None:
    if not isinstance(indicators, list):
        raise MCPArgumentError(
            "indicators must be a list; use [] to clear them",
            field="indicators",
            received=indicators,
            suggested_arguments={"indicators": []},
        )
    for index, indicator in enumerate(indicators):
        field = f"indicators[{index}]"
        if not isinstance(indicator, dict):
            raise MCPArgumentError(
                f"{field} must be an object with name and optional inputs",
                field=field,
                received=indicator,
            )
        name = str(indicator.get("name") or "").strip().lower()
        if name not in _INDICATOR_NAMES:
            raise MCPArgumentError(
                f"{field}.name is unsupported",
                field=f"{field}.name",
                received=indicator.get("name"),
                allowed_values=_INDICATOR_NAMES,
            )
        if "inputs" in indicator and not isinstance(indicator["inputs"], dict):
            raise MCPArgumentError(
                f"{field}.inputs must be an object",
                field=f"{field}.inputs",
                received=indicator["inputs"],
                suggested_arguments={"inputs": {}},
            )


def _resolve_visual_spec_path(path_raw: str) -> tuple[Path, str] | dict[str, Any]:
    raw = str(path_raw or "").strip()
    if not raw:
        return err("invalid_chart_spec_path", "path is required")

    root = repo_root().resolve(strict=False)
    allowed_dir = (root / _VISUAL_SPEC_DIR).resolve(strict=False)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve(strict=False)

    try:
        resolved.relative_to(allowed_dir)
    except ValueError:
        return err(
            "invalid_chart_spec_path",
            "path must be under .wayfinder_runs/visual_specs",
            {"path": str(resolved), "allowed_dir": str(allowed_dir)},
        )

    # Display path only. On Shells, .wayfinder_runs is a symlink out of the
    # repo (/wf/user_vault/scripts), so the resolved path legitimately escapes
    # the repo root — that must not fail validation.
    try:
        display_path = str(resolved.relative_to(root))
    except ValueError:
        display_path = str(resolved)

    if resolved.suffix.lower() != ".json":
        return err(
            "invalid_chart_spec_path",
            "chart spec path must end in .json",
            {"path": display_path},
        )
    if not resolved.exists():
        return err("not_found", "Chart spec not found", {"path": display_path})

    return resolved, display_path


def _compact_chart_result(
    *,
    chart: dict[str, Any],
    response: dict[str, Any],
    path: str | None = None,
) -> dict[str, Any]:
    workspace = response.get("chart_workspace") if isinstance(response, dict) else {}
    if not isinstance(workspace, dict):
        workspace = {}

    series = chart.get("series")
    chart_summary: dict[str, Any] = {
        "id": chart.get("id"),
        "title": chart.get("title"),
        "kind": chart.get("kind"),
        "series_count": len(series) if isinstance(series, list) else 0,
        "lookback_days": chart.get("lookback_days"),
        "limit": chart.get("limit"),
    }
    if path is not None:
        chart_summary["path"] = path
    return {
        "chart": chart_summary,
        "chart_workspace": {
            "activeChartId": workspace.get("activeChartId"),
            "version": workspace.get("version"),
        },
        "chart_validation": response.get("chart_validation")
        if isinstance(response, dict)
        else None,
    }


_PREVIEW_SAMPLE_POINTS = 3


def _compact_preview_summary(resolved: dict[str, Any]) -> dict[str, Any]:
    resolved_series = resolved.get("series") if isinstance(resolved, dict) else []
    summaries: list[dict[str, Any]] = []
    for item in resolved_series if isinstance(resolved_series, list) else []:
        if not isinstance(item, dict):
            continue
        points = item.get("points") if isinstance(item.get("points"), list) else []
        y_values = [
            point["y"]
            for point in points
            if isinstance(point, dict)
            and isinstance(point.get("y"), (int, float))
            and not isinstance(point.get("y"), bool)
        ]
        summaries.append(
            {
                "id": item.get("id"),
                "label": item.get("label"),
                "unit": item.get("unit"),
                "points": len(points),
                "first_x": points[0].get("x") if points else None,
                "last_x": points[-1].get("x") if points else None,
                "y_first": y_values[0] if y_values else None,
                "y_last": y_values[-1] if y_values else None,
                "y_min": min(y_values) if y_values else None,
                "y_max": max(y_values) if y_values else None,
                "sample_head": points[:_PREVIEW_SAMPLE_POINTS],
                "sample_tail": points[
                    max(len(points) - _PREVIEW_SAMPLE_POINTS, _PREVIEW_SAMPLE_POINTS) :
                ],
            }
        )
    return {"series": summaries}


@catch_errors
async def visual_get_frontend_context(include_health: bool = False) -> dict[str, Any]:
    """Read the current UI state and chart workspace.

    Use `include_health=True` only to audit or repair chart sources; it adds
    bounded warnings and replacement candidates.
    """
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    try:
        return ok(await INSTANCE_STATE_CLIENT.get_state(include_health=include_health))
    except httpx.HTTPStatusError as exc:
        return err("state_http_error", f"HTTP {exc.response.status_code}")


@catch_errors
async def visual_search_chart_series(
    query: str,
    kind: str | None = None,
    venue: str | None = None,
    market_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Find backend-supported series for market, rate, and yield charts.

    Search by intent first; add `kind` only to narrow a known data family.
    Inspect `shape` and `supported_chart_kinds`, then copy the returned
    `source`. Prefer Hyperliquid perps for price performance unless spot was
    requested.
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


@catch_errors
async def visual_set_active_market(
    query: str | None = None,
    market_id: str | None = None,
    market_type: str | None = None,
    chain_id: int | None = None,
) -> dict[str, Any]:
    """Switch the live chart and trading panels to one tradable market.

    Prefer this over `visual_create_chart` for a single token, perp, spot, or
    prediction market. Pass either natural `query` or exact `market_id`;
    `market_type` may be hl-perp, hl-spot, onchain-spot, or polymarket. Onchain
    tokens require onchain-spot and may need `chain_id`.
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
            )
        )
    except httpx.HTTPStatusError as exc:
        message, details = _http_error_message(exc)
        return err("active_market_http_error", message, details)
    except Exception as exc:  # noqa: BLE001
        return err("active_market_error", str(exc))


@catch_errors
async def visual_preview_series(
    series: ChartSeriesSpecs,
    kind: ChartKind = "line",
    transforms: ChartTransforms = None,
    lookback_days: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Resolve chart data without saving and return compact value statistics.

    Preview unfamiliar sources or derived math before charting. It uses the
    same spec and automatic rate-to-percent normalization as
    `visual_create_chart`, so inspect ranges and units here first.
    """
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    series = _validate_chart_series(series)
    kind = normalize_enum(kind, field_name="kind", allowed_values=_CHART_KINDS)
    _validate_chart_transforms(transforms)
    payload: dict[str, Any] = {
        "kind": kind,
        "series": _normalize_chart_series_for_display(series),
        "transforms": transforms or [],
    }
    if lookback_days:
        payload["lookback_days"] = lookback_days
    if limit:
        payload["limit"] = limit
    try:
        resolved = await INSTANCE_STATE_CLIENT.resolve_chart_data(payload)
        return ok(_compact_preview_summary(resolved))
    except httpx.HTTPStatusError as exc:
        message, details = _http_error_message(exc)
        return err("chart_data_http_error", message, details)
    except Exception as exc:  # noqa: BLE001
        return err("chart_data_error", str(exc))


@catch_errors
async def visual_set_chart_indicators(
    chart_id: str,
    indicators: ChartIndicatorList,
) -> dict[str, Any]:
    """Replace a chart's TradingView indicators; pass `[]` to clear them.

    Each item is `{name, inputs?, id?, forceOverlay?}`. Names: sma, ema,
    bollinger, supertrend, vwap, rsi, macd, atr, stochastic, volume. Use
    friendly inputs such as `length`, `source`, `mult`, `factor`, or
    `fast`/`slow`/`signal`; omit inputs for defaults. Only live,
    price_candle, and single-series time charts support indicators.
    """
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    _validate_indicators(indicators)
    try:
        response = await INSTANCE_STATE_CLIENT.set_chart_indicators(
            chart_id, indicators
        )
        workspace = (
            response.get("chart_workspace") if isinstance(response, dict) else {}
        )
        if not isinstance(workspace, dict):
            workspace = {}
        return ok(
            {
                "chart_id": chart_id,
                "indicators": (workspace.get("defaultIndicators") or {}).get(
                    chart_id, []
                ),
                "chart_workspace": {
                    "activeChartId": workspace.get("activeChartId"),
                    "version": workspace.get("version"),
                },
            }
        )
    except httpx.HTTPStatusError as exc:
        message, details = _http_error_message(exc)
        return err("chart_workspace_http_error", message, details)
    except Exception as exc:  # noqa: BLE001
        return err("chart_workspace_error", str(exc))


@catch_errors
async def visual_create_chart(
    chart_id: str,
    title: str,
    kind: ChartKind,
    series: ChartSeriesSpecs,
    transforms: ChartTransforms = None,
    overlays: list[dict[str, Any]] | None = None,
    lookback_days: int | None = None,
    limit: int | None = None,
    layout: dict[str, Any] | None = None,
    context_market_id: str | None = None,
    indicators: ChartIndicators = None,
) -> dict[str, Any]:
    """Create or replace a persistent workspace chart.

    Kinds: price_candle, line, bar, table. Prefer dataset_series from
    `visual_search_chart_series`; market_price, inline, and legacy
    delta_lab_asset also work. Transforms: filter, latest_by, top_n,
    rebase, pct_change, scale, ratio, spread, and moving_average.

    Known rates auto-scale to percent; hourly funding auto-annualizes.
    Chart transforms affect every series unless scoped by `series_ids`; scale
    a derived ratio inside the ratio transform so its inputs stay unchanged.
    `context_market_id` binds trading panels. Indicators require a
    TradingView-backed chart. Revise failed validation or implausible ranges.
    """
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    chart_id = str(chart_id or "").strip()
    title = str(title or "").strip()
    if not chart_id:
        raise MCPArgumentError(
            "chart_id is required; use a stable slug such as 'btc-funding'",
            field="chart_id",
            received=chart_id,
            suggested_arguments={"chart_id": "market-metric"},
        )
    if not title:
        raise MCPArgumentError(
            "title is required",
            field="title",
            received=title,
            suggested_arguments={"title": "Market metric"},
        )
    kind = normalize_enum(kind, field_name="kind", allowed_values=_CHART_KINDS)
    series = _validate_chart_series(series)
    _validate_chart_transforms(transforms)
    if indicators is not None:
        _validate_indicators(indicators)
    chart = {
        "id": chart_id,
        "title": title,
        "kind": kind,
        "series": _normalize_chart_series_for_display(series),
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
        response = await INSTANCE_STATE_CLIENT.upsert_workspace_chart(chart)
    except httpx.HTTPStatusError as exc:
        message, details = _http_error_message(exc)
        return err("chart_workspace_http_error", message, details)
    except Exception as exc:  # noqa: BLE001
        return err("chart_workspace_error", str(exc))

    result = _compact_chart_result(chart=chart, response=response)
    if indicators:
        try:
            await INSTANCE_STATE_CLIENT.set_chart_indicators(chart_id, indicators)
            result["indicators"] = indicators
        except httpx.HTTPStatusError as exc:
            message, details = _http_error_message(exc)
            result["indicators_error"] = {"message": message, "details": details}
        except Exception as exc:  # noqa: BLE001
            result["indicators_error"] = {"message": str(exc)}
    return ok(result)


@catch_errors
async def visual_import_chart_spec(path: str) -> dict[str, Any]:
    """Import one chart spec from `.wayfinder_runs/visual_specs/*.json`.

    Use this for script-generated specs too large to send through model
    context. The JSON must match the `visual_create_chart` schema.
    """
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)

    resolved = _resolve_visual_spec_path(path)
    if isinstance(resolved, dict):
        return resolved
    spec_path, display_path = resolved

    try:
        payload = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return err(
            "invalid_chart_spec_json",
            f"Invalid JSON: {exc.msg}",
            {"path": display_path, "line": exc.lineno, "column": exc.colno},
        )

    if not isinstance(payload, dict):
        return err(
            "invalid_chart_spec",
            "Chart spec JSON must be an object",
            {"path": display_path},
        )

    chart = dict(payload)
    series = chart.get("series")
    if isinstance(series, list):
        chart["series"] = _normalize_chart_series_for_display(series)

    try:
        response = await INSTANCE_STATE_CLIENT.upsert_workspace_chart(chart)
        return ok(
            _compact_chart_result(
                path=display_path,
                chart=chart,
                response=response,
            )
        )
    except httpx.HTTPStatusError as exc:
        message, details = _http_error_message(exc)
        return err("chart_workspace_http_error", message, details)
    except Exception as exc:  # noqa: BLE001
        return err("chart_workspace_error", str(exc))


@catch_errors
async def visual_set_active_chart(chart_id: str) -> dict[str, Any]:
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
        message, details = _http_error_message(exc)
        return err("chart_workspace_http_error", message, details)
    except Exception as exc:  # noqa: BLE001
        return err("chart_workspace_error", str(exc))


@catch_errors
async def visual_add_workspace_chart_series(
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
        message, details = _http_error_message(exc)
        return err("chart_workspace_http_error", message, details)
    except Exception as exc:  # noqa: BLE001
        return err("chart_workspace_error", str(exc))


@catch_errors
async def visual_add_workspace_chart_annotation(
    chart_id: str,
    type: str,
    config: dict[str, Any],
    annotation_id: str | None = None,
) -> dict[str, Any]:
    """Add an annotation to a workspace or live chart.

    Types: vertical_line `{time, label?}`, horizontal_line `{price, label?}`,
    marker `{time, price?, shape?}`, range, text_label, and trend. Use
    vertical_line for dated events; marker is a price-anchored entry/exit
    point, not an event line. Get the current live `chart_id` from
    `visual_get_frontend_context`.
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


@catch_errors
async def visual_add_workspace_chart_overlay(
    chart_id: str,
    overlay: dict[str, Any],
) -> dict[str, Any]:
    """Append an overlay to a workspace or live chart.

    Event sets use `{type: "event_markers", data: [{time, price?, label?}]}`.
    Legacy `markers` is accepted and normalized to `data`.
    """
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


@catch_errors
async def visual_clear_chart_workspace() -> dict[str, Any]:
    """Remove all agent-created workspace charts."""
    if not is_opencode_instance():
        return err(*_NOT_OPENCODE_ERR)
    try:
        return ok(await INSTANCE_STATE_CLIENT.clear_chart_workspace())
    except httpx.HTTPStatusError as exc:
        return err("chart_workspace_http_error", f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        return err("chart_workspace_error", str(exc))
