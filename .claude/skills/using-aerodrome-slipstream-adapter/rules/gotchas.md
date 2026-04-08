# Aerodrome Slipstream gotchas

## Base only

- `AerodromeSlipstreamAdapter` is Base-only in this repo.
- Do not pretend it supports arbitrary `chain_id` inputs.

## Deployment variants matter

- Slipstream uses multiple deployment variants on Base.
- `get_all_markets(...)` returns the deployment set it scanned.
- Reads can accept `deployments=...`; writes use the adapter’s configured `write_deployment` unless a method resolves a manager from the token id.
- `get_pool(...)` can fail with a multi-match error if the same pair and tick spacing exists across deployments and you do not pass `deployment_variant`.

## `get_all_markets()` is not a plain list

- It returns a dict with `deployments`, `total`, and `markets`.
- Pagination works across the combined deployment set.

## NFT token ids are the position identity

- Slipstream positions are NFTs, not fungible LP tokens.
- `token_id` identifies the position; use `get_pos(token_id=...)` before mutating it.
- If a `token_id` is not unique across configured position managers, pass `position_manager=...` explicitly.

## Wallet ownership vs staked ownership matters

- `increase_liquidity(...)`, `decrease_liquidity(...)`, `collect_fees(...)`, and `burn_position(...)` require the wallet to currently own the NFT.
- Once staked, the gauge is the NFT owner, so you usually need `unstake_position(...)` before those NPM-only actions.

## Concentrated-liquidity risk

- Tick range selection matters.
- A position can go out of range, which changes inventory composition and fee earning behavior.
- Do not reuse classic Aerodrome assumptions for Slipstream positions.
- Analytics like `slipstream_fee_apr_percent(...)` and `slipstream_prob_in_range_week(...)` are adapter-level estimates based on current state and swap logs, not protocol-guaranteed outputs.

## veAERO vote timing

- `can_vote_now(token_id=...)` only checks `lastVoted` against the current epoch start.
- It does not apply Aerodrome's first-hour / last-hour vote-window restrictions.
- Use `vote(..., check_window=True)` or `reset_vote(..., check_window=True)` for execution safety.

## Raw units and slippage mins

- Token amounts and liquidity values are raw on-chain integers.
- Explicit min amounts must be non-negative raw values.

## Burn requires a cleared position

- `burn_position(...)` only works when liquidity is zero and the position is ready to burn.
- In practice that usually means: decrease liquidity, collect fees, then burn.

## Fees vs gauge rewards vs veNFT rewards

- `collect_fees(...)` claims pool trading fees from the NFT position.
- `claim_position_rewards(...)` claims gauge emissions for a staked position NFT.
- `claim_gauge_rewards(...)` is a shared Aerodrome reward helper on gauge lists.
- `claim_fees(...)`, `claim_bribes(...)`, and `claim_rebases(...)` are veAERO-linked reward paths.

Do not collapse these into one generic "claim" action.

## Full wallet reads can be expensive

- `get_full_user_state(...)` enumerates deployments, pools, gauges, positions, and veNFTs.
- `include_vote_claimables=True` adds additional reward-contract and claimable scans.
