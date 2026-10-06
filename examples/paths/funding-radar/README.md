# Funding Radar — example path with a workspace panel

A complete example of a path that ships a **workspace panel**: a live
funding-rate radar for Hyperliquid perps, defaulting to **equity perps**
(HIP-3 builder dexes — TSLA, NVDA, …).

What it demonstrates:

- **Authenticated reads** via `bridge.fetch` — one markets snapshot
  (`/blockchain/hyperliquid/markets/`, all dexes in one call) every 30s,
  plus a slow per-coin funding-history backfill for the Δ24h funding
  column that stays inside the panel rate budget (one call every 3s).
- **Sortable radar** — current 1h funding (Hyperliquid funding accrues
  hourly), Δ24h price (`markPx` vs `prevDayPx`), Δ24h funding drift.
  Sort + scope persist across reloads via `bridge.setState`.
- **`wf:set_market`** — clicking a row asks the host to switch the
  workspace's active market, populating the chart and trade ticket.
  Requires the `market.switch` capability (declared in `wfpath.yaml`,
  shown to the user in the add-time consent dialog). The panel feature-
  detects it from the host's `wf:hello` capabilities and degrades to a
  read-only table on hosts without it.
- **Theming** from `wf:context` tokens; no build step (plain JS panel).

## Try it

```bash
# Mock data (offline; equity fixtures included)
poetry run wayfinder path preview --path examples/paths/funding-radar --panel radar

# Real read-only data through your own API key
poetry run wayfinder path preview --path examples/paths/funding-radar --panel radar --live
```

In the sandbox, grant/revoke `market.switch` from the capabilities the
host advertises to test the deny path; the Data tab logs every fetch and
set-market with its allow/deny outcome.
