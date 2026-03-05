---
name: using-lido-adapter
description: How to use the Lido adapter for staking ETH, wrapping stETH↔wstETH, and managing async withdrawals via the WithdrawalQueue (Ethereum mainnet).
metadata:
  tags: wayfinder, lido, steth, wsteth, staking, withdrawal, ethereum
---

## When to use

Use this skill when you are:
- Staking ETH into Lido (receive `stETH` or `wstETH`)
- Wrapping/Unwrapping `stETH` and `wstETH`
- Requesting or claiming withdrawals via Lido’s WithdrawalQueue
- Reading user balances, withdrawal requests, and rate data

## How to use

- [rules/high-value-reads.md](rules/high-value-reads.md) - Rates, withdrawals, and user snapshots
- [rules/execution-opportunities.md](rules/execution-opportunities.md) - stake/wrap/unwrap/request/claim flows
- [rules/gotchas.md](rules/gotchas.md) - Limits, units, splitting, and return shapes

