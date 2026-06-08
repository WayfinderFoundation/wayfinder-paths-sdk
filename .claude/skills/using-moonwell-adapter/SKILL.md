---
name: using-moonwell-adapter
description: How to use the Moonwell adapter for Moonwell Core lending/borrowing across Base, OP Mainnet, Moonbeam, and Moonriver, including market discovery, APYs, collateral, rewards, and common gotchas.
metadata:
  tags: wayfinder, moonwell, lending, borrowing, base, optimism, moonbeam, moonriver, apy, collateral
---

## When to use

Use this skill when you are:
- Fetching Moonwell market data (APYs, collateral factors)
- Reading user positions on Moonwell
- Writing scripts that lend/borrow/manage collateral on Moonwell
- Deciding whether a Moonwell flow belongs in `MoonwellAdapter` or `MorphoAdapter`

## How to use

- [rules/high-value-reads.md](rules/high-value-reads.md) - Market discovery, APYs, user positions
- [rules/execution-opportunities.md](rules/execution-opportunities.md) - Ad-hoc scripts for lend/borrow/collateral
- [rules/gotchas.md](rules/gotchas.md) - mToken addresses, units, and common failures
