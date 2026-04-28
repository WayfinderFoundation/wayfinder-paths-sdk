---
name: using-compound-adapter
description: How to use the Compound adapter for the functionality implemented in this repo (Compound III / Comet market reads, user state, base lend/withdraw, base borrow/repay, collateral supply/withdraw, and reward claims).
metadata:
  tags: wayfinder, compound, compound-iii, comet, lending, borrowing, collateral, rewards
---

## When to use

Use this skill when you are:
- Reading Compound III / Comet markets configured in this repo on Ethereum, Base, Arbitrum, or Polygon
- Reading a user's position in one Comet market or across all configured Comet markets
- Writing scripts that supply/withdraw the base asset, borrow/repay the base asset, manage collateral assets, or claim rewards on Compound

## How to use

- [rules/high-value-reads.md](rules/high-value-reads.md) - Markets, positions, reward accrual, and cross-market user state
- [rules/execution-opportunities.md](rules/execution-opportunities.md) - Base lend/withdraw, base borrow/repay, collateral flows, and reward claims
- [rules/gotchas.md](rules/gotchas.md) - Comet-only scope, method signatures, raw units, and full-withdraw/full-repay behavior
