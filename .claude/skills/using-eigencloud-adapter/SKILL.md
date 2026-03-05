---
name: using-eigencloud-adapter
description: How to use the EigenCloud (EigenLayer) adapter for strategy discovery, positions, delegation, queued withdrawals, and rewards claiming (Ethereum mainnet).
metadata:
  tags: wayfinder, eigenlayer, eigencloud, restaking, delegation, withdrawals, rewards, ethereum
---

## When to use

Use this skill when you are:
- Listing EigenLayer strategies (“markets”) and underlying tokens
- Reading a user’s deposited shares / withdrawable shares, delegation, and rewards metadata
- Depositing/restaking into a strategy
- Delegating / undelegating / redelegating
- Queueing and completing withdrawals
- Claiming rewards (requires offchain-prepared claim structs / calldata)

## How to use

- [rules/high-value-reads.md](rules/high-value-reads.md) - Strategy list, positions, delegation, rewards metadata
- [rules/execution-opportunities.md](rules/execution-opportunities.md) - Deposit/delegate/withdraw/claim flows
- [rules/gotchas.md](rules/gotchas.md) - Share accounting, withdrawal roots, and claim data requirements

