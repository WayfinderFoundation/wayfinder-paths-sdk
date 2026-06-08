---
name: using-morpho-adapter
description: How to use the Morpho adapter for Morpho Markets + Vaults (market discovery, positions, market collateral/borrow ops, vault deposit/withdraw ops, rewards, and common gotchas).
metadata:
  tags: wayfinder, morpho, morpho-blue, metamorpho, lending, borrowing, rewards, vaults
---

## When to use

Use this skill when you are:
- Fetching Morpho market/vault data (APYs, rewards, warnings, historical APY)
- Reading user positions on Morpho Blue markets
- Executing market supply/withdraw/borrow/repay + collateral ops on a specific market
- Claiming current Merkl rewards, checking legacy URD distributions, or interacting with Morpho vaults (ERC-4626)

## How to use

- [rules/high-value-reads.md](rules/high-value-reads.md) - Markets/vaults + user snapshots
- [rules/execution-opportunities.md](rules/execution-opportunities.md) - Lend/borrow/collateral/rewards/vault ops
- [rules/gotchas.md](rules/gotchas.md) - Market keys, shares-based full-close, bundler/allocator config
