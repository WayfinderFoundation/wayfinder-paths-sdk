# EigenCloud gotchas

## Ethereum mainnet only

- `EigenCloudAdapter` does not take a `chain_id` parameter in this repo.
- Do not describe it as multi-chain.

## Shares are not the same as underlying

- Positions are tracked in strategy shares.
- Underlying estimates depend on `sharesToUnderlyingView(...)`.
- `get_all_markets(..., include_share_to_underlying=True)` and `get_pos(...)` expose the relevant conversion helpers.

## `get_full_user_state()` needs withdrawal roots

- `include_queued_withdrawals=True` is not enough by itself.
- You must supply `withdrawal_roots=[...]` if you want queued withdrawals included.
- Use `get_withdrawal_roots_from_tx_hash(...)` after queueing if you do not already track them.

## Queue and completion are separate phases

- `queue_withdrawals(...)` does not complete the withdrawal.
- `complete_withdrawal(...)` submits the completion transaction later.
- The adapter does not pre-confirm that the onchain delay has elapsed.

## Reward claims are proof-driven

- `claim_rewards(...)`, `claim_rewards_batch(...)`, and `claim_rewards_calldata(...)` expect prepared claim data.
- Do not guess claim structs or pretend the adapter can build merkle proofs from scratch.

## Wallet vs explicit account

- Read methods can use an explicit `account=...`.
- Write methods require a configured signing wallet (`wallet_address` and `sign_callback`).

## Optional USD fields are estimates

- `include_usd=True` uses token-price lookups for an estimate.
- Treat those as reporting fields, not exact settlement values.
