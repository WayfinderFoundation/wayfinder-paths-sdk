---
name: using-uniswap-adapter
description: How to use the Uniswap V3-only adapter for concentrated-liquidity NFT positions on Base/Arbitrum/Ethereum (LP provisioning, position management, tick math, and common gotchas).
metadata:
  tags: wayfinder, uniswap, v3, liquidity, lp, base, arbitrum, tick, nft
---

## Current support boundary

This skill is for the SDK's `UniswapAdapter`, which is a **Uniswap V3
NonfungiblePositionManager adapter**. It is not a swap router and does not
support Universal Router command execution, Permit2 signatures/transfers, v2
routes, or Uniswap v4 PoolManager/PositionManager flows.

Use a separate design before adding Universal Router or v4 behavior. Current
Uniswap docs recommend v4 for new integrations and Universal Router for
programmatic swaps, but those are different protocol surfaces from this adapter.

## When to use

Use this skill when you are:
- Provisioning concentrated liquidity on Uniswap V3 (or V3 forks)
- Reading LP positions, pool state, or uncollected fees
- Writing scripts that add/remove liquidity or collect fees
- Working with tick math, price ranges, or sqrtPriceX96

Do not use this skill when you are:
- Building token swaps through Universal Router or SwapRouter02
- Building Permit2 signature-transfer or allowance-transfer flows
- Building Uniswap v4 pools, hooks, swaps, or liquidity positions
- Building v2 pair liquidity or constant-product router flows

## How to use

- [rules/high-value-reads.md](rules/high-value-reads.md) - Pool state, positions, fees, tick/price conversions
- [rules/execution-opportunities.md](rules/execution-opportunities.md) - Add/remove liquidity, collect fees
- [rules/gotchas.md](rules/gotchas.md) - Tick math pitfalls, slippage, token ordering, ABIs
