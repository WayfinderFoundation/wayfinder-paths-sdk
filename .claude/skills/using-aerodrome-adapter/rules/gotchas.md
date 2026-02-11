# Gotchas

## Epoch timing (voting)

- Epoch boundaries are weekly; Aerodrome epochs start **Thursday 00:00 UTC**.
- Voting is typically restricted to **once per epoch per veNFT**.
- Use `adapter.can_vote_now(token_id=...)` before submitting votes.

## Fees vs emissions

- v2 pools: emissions require **gauge staking**.
- Don’t assume you earn emissions just by holding LP in your wallet.

## Token pricing in rankings

`rank_pools_by_usdc_per_ve()` needs token→USDC pricing for every bribe/fee token it sees.

- Some tokens won’t price cleanly (low liquidity, missing routes).
- Use `require_all_prices=False` to keep ranking results while dropping unpriceable rewards.

## Vote weights

- Vote weights are relative. A single-pool vote commonly uses `weights=[10_000]`.
- If you split across pools, weights should sum to your intended “total weight”.

## Slipstream range math

- Always quantize ticks to `tickSpacing`.
- Out-of-range positions earn ~0 swap fees until price re-enters the band.
- “Fee APR” estimates from short lookbacks can be wildly unstable.

## Write operations are real

Scripts like `scripts/protocols/aerodrome/aerodrome_smoke.py`,
`scripts/protocols/aerodrome/aerodrome_best_emissions_deploy.py` (without `--dry-run`),
and `scripts/protocols/aerodrome/slipstream_enter_position.py` broadcast transactions.

Double-check:
- wallet label
- chain IDs
- slippage
- pool addresses
