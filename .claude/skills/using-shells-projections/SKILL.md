---
name: using-shells-projections
description: How to read the Wayfinder Shells frontend UI state (active chart) and draw projections (overlays like price lines, markers, ranges, trends) onto the user's chart in real-time.
metadata:
  tags: wayfinder, shells, opencode, frontend, projections, charts, overlays
---

## TL;DR

Read what the user is viewing in Wayfinder Shells, then draw on top of their chart.

**Typical flow (MCP):**

```
1. shells_get_frontend_context()
   → {"ok": true, "data": {"frontend_context": {"chart": {"id": "hl-perp-BTC", "market_id": "BTC", "market_type": "hl-perp", "interval": "1m"}}, "sdk_projection": {...}}}
2. chart_id = data["frontend_context"]["chart"]["id"]   # "hl-perp-BTC"
3. shells_add_chart_projection(chart_id="hl-perp-BTC", type="horizontal_line", config={"price": 73500, "color": "#ef4444", "label": "Support"})
4. Line appears on the user's chart (within ~5s, faster on SSE).
```

**Python (from scripts):**

```python
from wayfinder_paths.core.clients.InstanceStateClient import INSTANCE_STATE_CLIENT

state = await INSTANCE_STATE_CLIENT.get_state()
chart_id = state["frontend_context"]["chart"]["id"]

await INSTANCE_STATE_CLIENT.add_projection(chart_id, {
    "type": "horizontal_line",
    "config": {"price": 73500, "color": "#ef4444", "label": "Support"},
})
```

## MCP tools

| Tool | Args | Use |
|------|------|-----|
| `shells_get_frontend_context` | (none) | Read current chart context + all projections |
| `shells_add_chart_projection` | `chart_id`, `type`, `config` | Add one overlay |
| `shells_clear_chart_projections` | `chart_id` | Wipe all overlays on a chart |

All gate on `is_opencode_instance()` and return `{"ok": false, "error": {"code": "not_opencode_instance"}}` when run outside Shells.

## Projection types

The backend is type-agnostic — these are the renderers the frontend currently maps to TradingView shapes.

| `type` | `config` |
|--------|----------|
| `horizontal_line` | `price`, `color?`, `label?` |
| `vertical_line` | `time` (unix sec), `color?`, `label?` |
| `marker` | `time`, `price?`, `shape?` (`arrow_up` / `arrow_down` / `flag` / `icon` / `emoji`), `color?` |
| `range` | `from_time?`, `to_time?`, `from_price`, `to_price`, `color?` |
| `text_label` | `time`, `price`, `text`, `color?` |
| `trend` | `from: {time, price}`, `to: {time, price}`, `color?`, `label?` |

## Gotchas

- **`marker` does not accept `label`** — TradingView's marker shapes auto-generate text. Use `text_label` for an annotated point.
- **All `time` values are unix seconds** (not ms).
- **Per-chart scope:** Projections are stored per `chart_id`; switching markets shows only that chart's overlays.
- **Latency:** Adding a projection emits a state-changed notification; FE renders within one poll cycle (~5s) or sooner on SSE.
- **Client returns data directly** — `INSTANCE_STATE_CLIENT` is a `WayfinderClient`, not an adapter, so no `(ok, data)` tuple.
- **Shells-only:** Detection via `OPENCODE_INSTANCE_ID` env var or `http://localhost:4096/global/health`.

## Use the helpers

`InstanceStateClient` exposes shortcuts so scripts don't have to walk the state dict:

```python
chart_id = await INSTANCE_STATE_CLIENT.get_chart_id()              # "hl-perp-BTC"
fc = await INSTANCE_STATE_CLIENT.get_frontend_context()             # just the frontend_context subtree
await INSTANCE_STATE_CLIENT.clear_projections(chart_id)             # wipe all
await INSTANCE_STATE_CLIENT.patch_projection(chart_id, [...])       # replace full list
```

## When to use

- "Highlight the liquidation price on my chart"
- "Mark where my entry / exit was"
- "Draw a target range I'm laddering into"
- "Show the funding flip points I'm watching"

Don't use for unrelated reporting — that's `shells_notify` (see `/using-shells-notify`).
