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
1. shells_search_chart_series(query="BTC ETH relative performance")
   -> copy compatible `source` objects and each result's `shape.default_y`
2. shells_create_chart(
     chart_id="btc-eth-relative",
     title="BTC vs ETH",
     kind="line",
     lookback_days=90,
     series=[
       {"id": "btc", "label": "BTC", "source": <BTC source>, "x": "ts", "y": "price_usd"},
       {"id": "eth", "label": "ETH", "source": <ETH source>, "x": "ts", "y": "price_usd"}
     ],
     transforms=[{"type": "rebase", "base": 100}]
   )
3. shells_set_active_chart(chart_id="btc-eth-relative")
4. shells_add_workspace_chart_annotation(
     chart_id="btc-eth-relative",
     type="text_label",
     config={"time": 1760000000, "price": 120, "text": "Relative breakout"}
   )
```

## MCP tools

| Tool | Args | Use |
|------|------|-----|
| `shells_get_frontend_context` | none | Read current default chart context and workspace |
| `shells_search_chart_series` | `query`, `kind?`, `venue?`, `market_type?`, `limit?` | Discover supported chart datasets and their column shapes |
| `shells_create_chart` | `chart_id`, `title`, `kind`, `series`, `transforms?`, `overlays?`, `lookback_days?`, `limit?`, `layout?`, `context_market_id?` | Validate, create, or replace a visual pane |
| `shells_set_active_chart` | `chart_id` | Focus an existing workspace chart |
| `shells_add_workspace_chart_annotation` | `chart_id`, `type`, `config`, `annotation_id?` | Add one TradingView annotation to a default or workspace chart |
| `shells_add_workspace_chart_overlay` | `chart_id`, `overlay` | Add a raw overlay, usually bulk `event_markers` |
| `shells_add_workspace_chart_series` | `chart_id`, `series` | Add or replace a data series on an existing workspace chart |
| `shells_clear_chart_workspace` | none | Clear agent-created charts and default-chart annotations |

All gate on `is_opencode_instance()` and return `{"ok": false, "error": {"code": "not_opencode_instance"}}` when run outside Shells.

## Chart panes

Use `shells_create_chart` when the user asks for a new visual pane, not when they only want to annotate the live chart.

If `shells_create_chart` returns `ok: false`, do not tell the user the chart is ready. Read the error, pick a compatible source/kind, and retry.

| Chart kind | Use |
|------------|-----|
| `price_candle` | Primary market price chart. Use `{"type": "market_price", "market_id": "..."}` or a Hyperliquid perp `dataset_series` returned by search. |
| `line` | One or more time series, such as relative performance or APYs over time. |
| `bar` | Ranked or latest categorical values. |
| `table` | Tabular data. |

Supported source types:

- `market_price`: `{"type": "market_price", "market_id": "hl-perp-btc"}`
- `dataset_series`: returned by `shells_search_chart_series`; preferred for known backend datasets
- `delta_lab_asset`: `{"type": "delta_lab_asset", "symbol": "USDC", "series": "lending", "venue"?: "...", "basis"?: true}`
- `inline`: `{"type": "inline", "points": [{...}]}`

Single-series time-series workspace charts render in TradingView. Multi-series
comparisons currently use the workspace line renderer; keep using `line` plus
`rebase` for relative performance charts.

For tradable perp charts, pass `context_market_id` so the shell switches the
order book, trades, and trade ticket while keeping the workspace chart active:

```json
{
  "chart_id": "pengu-price",
  "title": "PENGU Perp",
  "kind": "price_candle",
  "context_market_id": "hl-perp-pengu",
  "series": [
    {
      "id": "pengu-price",
      "label": "PENGU",
      "source": {"type": "dataset_series", "dataset_id": "hyperliquid.perp.price", "params": {"coin": "PENGU", "interval": "1h"}},
      "x": "ts",
      "y": "price_usd"
    }
  ]
}
```

Supported transforms: `filter`, `latest_by`, `top_n`, `rebase`, `pct_change`, `scale`, `multiply`, `ratio`, `spread`, `moving_average`. Prefer `rebase(base=100)` for relative performance across different units.

Transforms can live on the chart or on a single series. Use series-level transforms when only one dataset needs conversion, for example annualizing hourly funding:

```json
{
  "id": "btc-funding",
  "label": "BTC Funding",
  "source": {"type": "dataset_series", "dataset_id": "hyperliquid.perp.funding", "params": {"coin": "BTC"}},
  "x": "ts",
  "y": "funding_rate",
  "unit": "apy",
  "axis": "right",
  "transforms": [{"type": "scale", "factor": 8760, "unit": "apy"}]
}
```

Series can set `color` and `axis` (`left` or `right`). Keep related units on the same axis; use a right axis only when overlaying unrelated units.

## Dataset selection

Always search known datasets before inventing or fetching your own data.

1. Use `shells_search_chart_series` with the user intent/assets first. Do not pass `kind` by default; inspect returned `kind`, `shape.default_y`, `shape.columns`, and `shape.supported_chart_kinds` to decide whether to use the candidate.
2. Prefer a common source family across compared series. For example, use Hyperliquid perp prices for BTC and ETH together; do not mix Hyperliquid BTC with CoinGecko ETH unless there is no common source.
3. For asset price/performance requests, prefer Hyperliquid perp price series over spot/fallback price series unless the user explicitly asks for spot. Prefer Delta Lab for lending/yield/Boros/Pendle/funding research series, CoinGecko only as broad spot price fallback, and DeFiLlama for current ranked yield tables/bars.
4. Pass `kind` only to narrow a known data family (`funding`, `yield`, `price`) or a large result set. Do not pass chart kinds such as `line` as the first search because that hides useful candidate metadata.
5. Use Polymarket-specific tools/API for prediction markets. Do not route Polymarket discovery through chart-series search.
6. Use `inline` only when the registry does not expose the needed data. If using inline data, keep it small and describe the columns in the chart label or nearby message.
7. Set `lookback_days` on `shells_create_chart` when the user gives a time window. Use 90 for "3 months".
8. When a chart represents a tradable Hyperliquid perp, set
   `context_market_id` to `hl-perp-<symbol-lowercase>` unless the tool result
   already provides a more specific market id.

Registry results include:

- `source`: copy this into the chart series spec.
- `shape.default_y`: use this as the series `y` field unless a different column is intentional.
- `shape.columns`: read this before choosing transforms such as `latest_by`, `top_n`, `ratio`, or `spread`.

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
