---
name: using-shells-projections
description: How to read the Wayfinder Shells frontend UI state and draw annotations like price lines, markers, ranges, trends, and labels onto default or agent-created chart panes.
metadata:
  tags: wayfinder, shells, opencode, frontend, charts, annotations, overlays
---

## TL;DR

Read the current Shells chart id, then add annotations through the chart workspace API. The same tool works for the default live chart and for agent-created workspace charts.

**Typical flow (default chart):**

```
1. shells_get_frontend_context()
   → {"ok": true, "data": {"frontend_context": {"chart": {"id": "hl-perp-BTC", "market_id": "hl-perp-BTC", "market_type": "hl-perp"}}}}
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
2. shells_add_workspace_chart_annotation(
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
| `shells_add_workspace_chart_annotation` | `chart_id`, `type`, `config`, `annotation_id?` | Add one TradingView annotation to a default or workspace chart |
| `shells_add_workspace_chart_overlay` | `chart_id`, `overlay` | Add a raw overlay, usually bulk `event_markers` |
| `shells_add_workspace_chart_series` | `chart_id`, `series` | Add a data series to an existing workspace chart |
| `shells_clear_chart_workspace` | none | Clear agent-created charts and default-chart annotations |

All gate on `is_opencode_instance()` and return `{"ok": false, "error": {"code": "not_opencode_instance"}}` when run outside Shells.

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
