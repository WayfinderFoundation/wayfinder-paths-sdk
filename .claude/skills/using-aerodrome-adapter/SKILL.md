---
name: using-aerodrome-adapter
description: How to use the classic Aerodrome adapter on Base for market and Sugar reads, route and liquidity quoting, LP and gauge actions, veAERO lock and vote management, and shared reward helpers.
metadata:
  tags: wayfinder, aerodrome, base, dex, lp, gauge, veaero, voting, rewards
---

## When to use

Use this skill when you are:
- Reading classic Aerodrome pool, gauge, Sugar, and route data on Base
- Ranking classic pools with Sugar epochs or emissions-based analytics
- Quoting or executing LP add/remove, unstaked fee claims, and gauge stake flows
- Inspecting wallet LP balances, staked LP balances, veAERO NFTs, or vote claimables
- Managing veAERO locks, lock extensions, permanent locks, votes, and reward claims

## How to use

- [rules/high-value-reads.md](rules/high-value-reads.md) - Market discovery, Sugar analytics, wallet reads, and shared veAERO read helpers
- [rules/execution-opportunities.md](rules/execution-opportunities.md) - Route and liquidity quoting, LP and gauge actions, and shared veAERO write flows
- [rules/gotchas.md](rules/gotchas.md) - Base-only scope, pagination, raw units, reward-path separation, and vote-window constraints
