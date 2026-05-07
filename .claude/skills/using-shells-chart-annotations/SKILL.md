---
name: using-shells-chart-annotations
description: How to read Wayfinder Shells frontend state, create chart panes, and add TradingView annotations or overlays to the default live chart or agent-created workspace charts.
metadata:
  tags: wayfinder, shells, opencode, frontend, charts, annotations, overlays
---

## TL;DR

Read the current Shells chart id, then write chart changes through the chart workspace API. The same annotation tool works for the default live chart and for agent-created workspace charts.

**Typical flow (default chart):**

```
1. shells_get_frontend_context()
   -> {"ok": true, "data": {"frontend_context": {"chart": {"id": "hl-perp-BTC", "market_id": "hl-perp-BTC", "market_type": "hl-perp"}}}}
2. chart_id = data["frontend_context"]["chart"]["id"]
3. shells_add_workspace_chart_annotation(
     chart_id=chart_id,
     type="horizontal_line",
     config={"price": 73500, "color": "#ef4444", "label": "Support"}
   )
4. The annotation appears on the user's default chart.
```

**Typical flow (agent-created visual pane):**

```
1. shells_create_chart(
     chart_id="btc-eth-relative",
     title="BTC vs ETH",
     kind="line",
     series=[...],
     transforms=[{"type": "rebase", "base": 100}]
   )
2. shells_set_active_chart(chart_id="btc-eth-relative")
3. shells_add_workspace_chart_annotation(
     chart_id="btc-eth-relative",
     type="text_label",
     config={"time": 1760000000, "price": 120, "text": "Relative breakout"}
   )
```

## MCP tools

| Tool | Args | Use |
|------|------|-----|
| `shells_get_frontend_context` | none | Read current default chart context and workspace |
| `shells_create_chart` | `chart_id`, `title`, `kind`, `series`, `transforms?`, `overlays?`, `layout?` | Create or replace a visual pane |
| `shells_set_active_chart` | `chart_id` | Focus an existing workspace chart |
| `shells_add_workspace_chart_annotation` | `chart_id`, `type`, `config`, `annotation_id?` | Add one TradingView annotation to a default or workspace chart |
| `shells_add_workspace_chart_overlay` | `chart_id`, `overlay` | Add a raw overlay, usually bulk `event_markers` |
| `shells_add_workspace_chart_series` | `chart_id`, `series` | Add a data series to an existing workspace chart |
| `shells_clear_chart_workspace` | none | Clear agent-created charts and default-chart annotations |

All gate on `is_opencode_instance()` and return `{"ok": false, "error": {"code": "not_opencode_instance"}}` when run outside Shells.

## Chart panes

Use `shells_create_chart` when the user asks for a new visual pane, not when they only want to annotate the live chart.

| Chart kind | Use |
|------------|-----|
| `price_candle` | Primary market price chart. Use `{"type": "market_price", "market_id": "..."}` as the source. |
| `line` | One or more time series, such as relative performance or APYs over time. |
| `bar` | Ranked or latest categorical values. |
| `table` | Tabular data. |

Supported source types:

- `market_price`: `{"type": "market_price", "market_id": "hl-perp-btc"}`
- `delta_lab_asset`: `{"type": "delta_lab_asset", "symbol": "USDC", "series": "lending", "venue"?: "...", "basis"?: true}`
- `inline`: `{"type": "inline", "points": [{...}]}`

Supported transforms: `filter`, `latest_by`, `top_n`, `rebase`, `pct_change`, `ratio`, `spread`, `moving_average`. Prefer `rebase(base=100)` for relative performance across different units.

## Annotation types

| `type` | `config` |
|--------|----------|
| `horizontal_line` | `price`, `color?`, `label?` |
| `vertical_line` | `time` (unix sec), `color?`, `label?` |
| `marker` | `time`, `price?`, `shape?` (`arrow_up` / `arrow_down` / `flag` / `icon` / `emoji`), `color?` |
| `range` | `from_time?`, `to_time?`, `from_price`, `to_price`, `color?` |
| `text_label` | `time`, `price`, `text`, `color?` |
| `trend` | `from: {time, price}`, `to: {time, price}`, `color?`, `label?` |

## Gotchas

- `marker` does not accept `label`. Use `text_label` for annotated points.
- All `time` values are unix seconds.
- For default chart annotations, use the exact `frontend_context.chart.id`.
- For workspace charts, use the `chart_id` passed to `shells_create_chart`.
- Default chart annotations are stored in `chart_workspace.defaultAnnotations`; workspace chart annotations are stored in the chart's `overlays`.
- Chart workspace state is scoped to the current Shells instance, not the user vault.
