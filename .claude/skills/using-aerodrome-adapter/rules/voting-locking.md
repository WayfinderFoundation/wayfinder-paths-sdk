# Voting + locking (veAERO)

## Create a lock (veNFT)

`VotingEscrow.createLock(amount, duration)` mints a veNFT (`tokenId`).

In code:

- `tx_hash, receipt = await adapter.create_lock(..., wait_for_receipt=True)`
- `token_id = adapter.parse_ve_nft_token_id_from_create_lock_receipt(receipt, to_address=...)`

## Vote (weekly epoch)

Votes are cast via the Voter contract:

- `await adapter.vote(token_id=..., pools=[...], weights=[...])`

Notes:

- You can generally vote **once per epoch**.
- Use `await adapter.can_vote_now(token_id=...)` to check:
  - `last_voted_ts`
  - current epoch start
  - next epoch start

## Fees, bribes, rebase (claimables)

Aerodrome voters earn multiple reward types:

- **Fees**: trading fees (epoch-based)
- **Bribes**: external incentives attached to pools to attract votes
- **Rebase**: emissions distributed to veAERO locks (claimable per veNFT)

The adapter’s `get_full_user_state(..., include_usd_values=True)` can surface:

- `rebaseClaimable`
- per-vote `claimableFees` / `claimableBribes` (token lists + amounts)

## Practical strategy notes

- “Best pool to vote” is not just `fees+bribes` — votes are diluted by total votes on that pool.
- `rank_pools_by_usdc_per_ve()` ranks by **USDC per veAERO** (already vote-dilution adjusted).
- Treat it as a heuristic: pricing can fail for obscure bribe tokens; use `require_all_prices=False` when exploring.
