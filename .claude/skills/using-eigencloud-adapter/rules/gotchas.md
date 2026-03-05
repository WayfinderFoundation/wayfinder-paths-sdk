# EigenCloud gotchas

## Mainnet-only

- EigenCloud adapter is wired to Ethereum mainnet contracts and does not accept a `chain_id` parameter.

## Shares vs underlying

- Restaking uses **strategy shares**, not ERC-4626 shares.
- `get_pos(...)` reports `deposit_shares` and `withdrawable_shares`.
- Underlying conversions are best-effort via `sharesToUnderlyingView(...)` and should be treated as estimates.

## Withdrawal roots are the key handle

- `queue_withdrawals(...)`, `undelegate(...)`, and `redelegate(...)` can return `withdrawal_roots` (when `include_withdrawal_roots=True`).
- If you missed roots, you can recover them from a transaction receipt:
  - `get_withdrawal_roots_from_tx_hash(tx_hash)`
- Use roots to read/complete withdrawals:
  - `get_queued_withdrawal(withdrawal_root)`
  - `complete_withdrawal(withdrawal_root, ...)`

## Delegation approver signatures

- Some operators require a delegation approver signature.
- The adapter supports passing:
  - `approver_signature` (bytes/hex),
  - `approver_expiry` (int),
  - `approver_salt` (bytes32-ish; hex/bytes/int).

## Rewards claims require offchain data

- You cannot “derive” a valid claim from on-chain state alone.
- Use the EigenLayer app/CLI/indexer to build claim structs (or raw calldata) and pass them to:
  - `claim_rewards(...)`
  - `claim_rewards_batch(...)`
  - `claim_rewards_calldata(...)`

