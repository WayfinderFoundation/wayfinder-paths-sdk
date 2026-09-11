# Aerodrome Slipstream Adapter (Base)

This adapter supports Aerodrome Slipstream concentrated-liquidity pools on Base.
Slipstream positions are NFPM NFTs, not fungible classic LP tokens.

## Supported flows

- Deployment-aware pool discovery across the initial, gauge-caps, and Gauges V3
  Slipstream deployments
- Pool state, range sizing, fee APR, volume, volatility, and in-range
  probability analytics
- NFPM position lifecycle: mint, increase, decrease, collect, and burn
- Gauge staking and unstaking for position NFTs, plus position reward claims
- Shared veAERO lock, vote, fee, bribe, and rebase helpers

## Current protocol notes

- New write flows default to the current Gauges V3 deployment. Older
  deployments remain scanned because existing pools and gauges are still live.
- Tick spacing is part of the pool identity. Pass the exact tick spacing for
  pool lookup and minting; common docs examples include 1, 50, 200, and 2000.
- NPM-only actions require wallet ownership of the NFT. If the NFT is staked,
  unstake it before increase, decrease, collect, or burn.
- Gauges V3 introduces minimum stake time and early-unstake/getReward penalty
  behavior at the gauge layer. Inspect pool/gauge terms before claiming or
  withdrawing shortly after staking.
- The adapter uses swap logs for analytics but does not execute swaps.

