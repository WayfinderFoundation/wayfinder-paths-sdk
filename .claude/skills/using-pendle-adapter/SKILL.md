---
name: using-pendle-adapter
description: How to use the Pendle adapter in Wayfinder Paths for PTs (Principal Tokens), YTs (Yield Tokens), and Pendle limit orders: market discovery, historical data, Hosted SDK swap tx building, taker fills, maker orders, inputs/outputs, chain IDs, unit handling, and approvals.
metadata:
  tags: wayfinder, pendle, pt, yt, yield, swap, execution
---

## When to use

Use this skill when you are:
- Screening Pendle PTs and YTs across chains (fixed vs floating yield, liquidity/volume, expiry)
- Pulling Pendle market time series (prices, APYs, TVL)
- Building swap payloads (tx + approvals) to buy/sell PTs or YTs via Pendle Hosted SDK
- Fetching, filling, creating, or cancelling Pendle limit orders

## How to use

- [rules/high-value-reads.md](rules/high-value-reads.md) - Discovery + time series (data in/out)
- [rules/execution-opportunities.md](rules/execution-opportunities.md) - Swap payload building and “best PT” selection
- [rules/gotchas.md](rules/gotchas.md) - Units, chain IDs, address formats, and integration hazards
