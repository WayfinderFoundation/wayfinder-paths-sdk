---
name: using-aerodrome-slipstream-adapter
description: How to use the Aerodrome Slipstream adapter for concentrated liquidity on Base (pool discovery, market and position reads, mint/increase/decrease flows, gauge staking, and veAERO-linked reward claims).
metadata:
  tags: wayfinder, aerodrome, slipstream, base, concentrated-liquidity, lp, nft, gauge, veaero
---

## When to use

Use this skill when you are:
- Reading Slipstream pool, gauge, and deployment-aware market data on Base
- Discovering concentrated-liquidity pools or inspecting a specific NFT position
- Minting, increasing, decreasing, collecting, or burning Slipstream positions
- Staking LP NFT positions into gauges and claiming rewards
- Managing veAERO voting and reward-claim flows that also apply to Slipstream gauges

## How to use

- [rules/high-value-reads.md](rules/high-value-reads.md) - Pool discovery, market lists, deployment-aware reads, and wallet state
- [rules/execution-opportunities.md](rules/execution-opportunities.md) - Mint/increase/decrease/collect/burn, gauge staking, and veAERO actions
- [rules/gotchas.md](rules/gotchas.md) - Deployment variants, NFT token ids, tick-range risk, and Base-only constraints
