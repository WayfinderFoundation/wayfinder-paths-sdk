---
name: using-definitive-adapter
description: How to place, list and cancel resting onchain token orders (limit, stop-loss, take-profit, breakout buy, trailing stop, TP + SL) through Definitive Flash in Wayfinder Paths.
metadata:
  tags: wayfinder, definitive, flash, limit, stop, trailing-stop, orders
---

## When to use

Use this skill when a user wants an onchain token order that waits for a
price instead of swapping now: "buy ETH if it dips to 2,500", "sell my
WETH if it drops 5% from its high", "take profit at 3,500 or stop at 2,800".
Market swaps stay on BRAP (`onchain_quote_swap` + `onchain_swap`).

## How to use

- [rules/orders.md](rules/orders.md) - MCP tools, order types, sides and prices
- [rules/gotchas.md](rules/gotchas.md) - What cancels an order, what can't be done, safety checks
