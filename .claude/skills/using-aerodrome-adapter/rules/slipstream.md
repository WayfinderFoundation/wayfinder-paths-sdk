# Slipstream (CL) notes

Slipstream is Aerodrome’s concentrated liquidity system (Uniswap v3-style).

## Key primitives

- A CL pool is identified by `(token0, token1, tickSpacing)`.
- Liquidity positions are NFTs defined by:
  - `tickLower`, `tickUpper`
  - deposited `amount0`, `amount1`

Useful reads:

- `slipstream_pool_state(pool=...)` → current tick, liquidity, `fee_pips`, `unstaked_fee_pips`
- `slipstream_range_metrics(...)` → liquidity share and “what your position looks like now”

## Picking a range

Typical workflow:

1. Start with a symmetric band around current tick (e.g. ±5% in price).
2. Quantize ticks to `tickSpacing`:
   - `floor_tick_to_spacing(...)`
   - `ceil_tick_to_spacing(...)`

## Fee / volume heuristics

The adapter can estimate:

- volume per day from Swap logs
- sigma (volatility) from swaps
- a crude “stay in range” probability
- fee APR from range share × volume × effective fee × in-range fraction

Treat these as **rough** heuristics; they depend heavily on log availability and the stability of short lookbacks.

## Staking CL positions

Some Slipstream pools have gauges; staking a position NFT may enable emissions.
The pool also exposes both `fee()` and `unstakedFee()`; do not assume they are constant across pools.

