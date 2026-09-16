---
name: using-uniswap-adapter
description: How to use Uniswap V3 and V4 swaps and concentrated liquidity, including Arc V4 explicit-position management.
metadata:
  tags: wayfinder, uniswap, v3, liquidity, lp, base, arbitrum, tick, nft
---

## When to use

Use this skill when you are:
- Provisioning concentrated liquidity on Uniswap V3 (or V3 forks)
- Reading LP positions, pool state, or uncollected fees
- Writing scripts that add/remove liquidity or collect fees
- Working with tick math, price ranges, or sqrtPriceX96

## How to use

- [rules/high-value-reads.md](rules/high-value-reads.md) - Pool state, positions, fees, tick/price conversions
- [rules/execution-opportunities.md](rules/execution-opportunities.md) - Add/remove liquidity, collect fees
- [rules/gotchas.md](rules/gotchas.md) - Tick math pitfalls, slippage, token ordering, ABIs

## Arc V4

Arc (5042) has V4, not V3. Use `UniswapAdapter({"chain_id": 5042}, ...)` and `v4_*`
methods; V3 NFT/pool calls fail instead of selecting another chain.
`v4_mint_position` accepts a `PoolKey`, ticks, liquidity and raw amount maxima;
`v4_increase_liquidity` / `v4_decrease_liquidity` take explicit IDs and limits.
`v4_collect_fees` uses zero-liquidity decrease; `v4_close_position` burns and
returns both currencies to the owner. Persist minted `token_ids` from receipts.
`v4_get_positions(ids)` reads only those IDs (max 100), with no history scans.

New liquidity supports hookless pools only. Use canonical currency order,
aligned ticks and deliberate slippage limits. Native USDC (zero address, 18
decimals) and ERC-20 USDC (`0x3600…0000`, 6 decimals) are different pool keys but
share a wallet balance. Do not normalize one pool into the other. Native input
uses `msg.value` and refunds excess; ERC-20 input uses Permit2.
