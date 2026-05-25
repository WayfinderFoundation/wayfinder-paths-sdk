---
name: using-aave-v3-adapter
description: How to use the Aave V3 adapter for lending/borrowing across supported chains (markets, APYs, rewards, collateral, and common gotchas).
metadata:
  tags: wayfinder, aave, aave-v3, lending, borrowing, apy, rewards, collateral
---

## When to use

Use this skill when you are:
- Fetching Aave V3 market data (APYs, caps, LTVs, rewards emissions)
- Reading user positions/snapshots on Aave V3
- Reading or setting eMode categories
- Reading or transacting with Aave Earn ERC-4626 vaults
- Writing scripts that supply/withdraw/borrow/repay, manage collateral, set eMode, use Earn vaults, or claim rewards

## How to use

- [rules/high-value-reads.md](rules/high-value-reads.md) - Markets, user snapshots, eMode, Earn vaults
- [rules/execution-opportunities.md](rules/execution-opportunities.md) - Supply/withdraw/borrow/repay/collateral/eMode/Earn/rewards
- [rules/gotchas.md](rules/gotchas.md) - Rate mode, native wrapping, risk modes, Earn vaults, rewards asset list
