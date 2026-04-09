---
name: using-aerodrome-slipstream-adapter
description: How to use the Aerodrome Slipstream adapter on Base for deployment-aware concentrated-liquidity reads, range analytics, position lifecycle actions, gauge staking, and shared veAERO helpers.
metadata:
  tags: wayfinder, aerodrome, slipstream, base, concentrated-liquidity, lp, nft, gauge, veaero
---

## When to use

Use this skill when you are:
- Reading Slipstream pool, gauge, and deployment-aware market data on Base
- Discovering concentrated-liquidity pools, selecting the best pool for a pair, or inspecting a specific NFT position
- Running range, volume, fee APR, volatility, or in-range probability analytics on a Slipstream pool
- Minting, increasing, decreasing, collecting, or burning Slipstream positions
- Staking LP NFT positions into gauges and claiming position rewards
- Managing veAERO locks, votes, and reward-claim flows that also apply to Slipstream gauges

## How to use

- [rules/high-value-reads.md](rules/high-value-reads.md) - Deployment-aware market discovery, pool analytics, position reads, and shared veAERO reads
- [rules/execution-opportunities.md](rules/execution-opportunities.md) - Mint, increase, decrease, collect, burn, gauge staking, and shared veAERO write flows
- [rules/gotchas.md](rules/gotchas.md) - Deployment variants, position-manager ownership, claim-path separation, and Base-only constraints
