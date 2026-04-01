---
name: using-aerodrome-adapter
description: How to use the Aerodrome adapter for classic Aerodrome pools on Base (market discovery, route and liquidity quoting, LP/gauge state, veAERO voting, and reward claims).
metadata:
  tags: wayfinder, aerodrome, base, dex, lp, gauge, veaero, voting, rewards
---

## When to use

Use this skill when you are:
- Reading Aerodrome pool, gauge, and route data on Base
- Ranking Aerodrome pools by incentives, fees, or gauge state
- Quoting or executing classic Aerodrome liquidity add/remove flows
- Inspecting LP, staked LP, and veAERO voting state for a wallet
- Managing veAERO locks, votes, and reward claims

## How to use

- [rules/high-value-reads.md](rules/high-value-reads.md) - Pool discovery, market lists, gauge state, ranking, and wallet reads
- [rules/execution-opportunities.md](rules/execution-opportunities.md) - Route quoting, liquidity changes, gauge staking, and veAERO actions
- [rules/gotchas.md](rules/gotchas.md) - Base-only scope, zero-address gauges, raw units, and veAERO timing constraints
