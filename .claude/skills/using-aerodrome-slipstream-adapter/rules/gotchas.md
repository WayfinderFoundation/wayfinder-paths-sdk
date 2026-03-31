# Aerodrome Slipstream gotchas

## Base only

- `AerodromeSlipstreamAdapter` is Base-only in this repo.
- Do not pretend it supports arbitrary `chain_id` inputs.

## Deployment variants matter

- Slipstream uses multiple deployment variants on Base.
- `get_all_markets(...)` returns the deployment set it scanned.
- Reads can accept `deployments=...`; writes use the adapter’s configured `write_deployment` unless a method resolves a manager from the token id.

## `get_all_markets()` is not a plain list

- It returns a dict with `deployments`, `total`, and `markets`.
- Pagination works across the combined deployment set.

## NFT token ids are the position identity

- Slipstream positions are NFTs, not fungible LP tokens.
- `token_id` identifies the position; use `get_pos(token_id=...)` before mutating it.

## Concentrated-liquidity risk

- Tick range selection matters.
- A position can go out of range, which changes inventory composition and fee earning behavior.
- Do not reuse classic Aerodrome assumptions for Slipstream positions.

## Raw units and slippage mins

- Token amounts and liquidity values are raw on-chain integers.
- Explicit min amounts must be non-negative raw values.

## Burn requires a cleared position

- `burn_position(...)` only works when liquidity is zero and the position is ready to burn.
- In practice that usually means: decrease liquidity, collect fees, then burn.

## Fees vs gauge rewards vs veNFT rewards

- `collect_fees(...)` claims pool trading fees from the NFT position.
- `claim_position_rewards(...)` claims gauge emissions for staked positions.
- `claim_fees(...)`, `claim_bribes(...)`, and `claim_rebases(...)` are veAERO-linked reward paths.

Do not collapse these into one generic "claim" action.
