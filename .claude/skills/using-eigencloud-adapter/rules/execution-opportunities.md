# EigenCloud execution opportunities

## Primary execution surface

- Adapter: `wayfinder_paths/adapters/eigencloud_adapter/adapter.py`
- README: `wayfinder_paths/adapters/eigencloud_adapter/README.md`

## Deposit / restake

- `await adapter.deposit(strategy=..., amount=..., token=None, check_whitelist=True)`
- This is the main restaking entrypoint.
- The adapter handles ERC-20 approval for `StrategyManager` when needed.

Use `get_all_markets()` first to confirm:
- the strategy address
- underlying token
- whether the strategy is currently whitelisted for deposit

## Delegation changes

- `await adapter.delegate(operator=..., approver_signature=b"", approver_expiry=0, approver_salt=None)`
- `await adapter.undelegate(staker=None, include_withdrawal_roots=True)`
- `await adapter.redelegate(new_operator=..., approver_signature=b"", approver_expiry=0, approver_salt=None, include_withdrawal_roots=True)`

These flows affect operator assignment, not the underlying restaked shares directly.

## Withdrawal queue and completion

### Queue withdrawals

- `await adapter.queue_withdrawals(strategies=[...], deposit_shares=[...], include_withdrawal_roots=True)`

This enters the EigenLayer withdrawal queue. If `include_withdrawal_roots=True`, capture the returned roots for follow-up reads and completion.

### Complete withdrawals

- `await adapter.complete_withdrawal(withdrawal_root=..., receive_as_tokens=True, tokens_override=None)`

Use a queued-withdrawal root from:
- the queue transaction output
- `get_withdrawal_roots_from_tx_hash(...)`
- your own bookkeeping / indexer

## Rewards

- `await adapter.set_rewards_claimer(claimer=...)`
- `await adapter.claim_rewards(claim=..., recipient=...)`
- `await adapter.claim_rewards_batch(claims=[...], recipient=...)`
- `await adapter.claim_rewards_calldata(calldata=..., value=0)`

These claim methods assume you already have valid claim data. The adapter does not generate the merkle proof payload for you.

## Wallet + scripting pattern

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.eigencloud_adapter import EigenCloudAdapter

adapter = await get_adapter(EigenCloudAdapter, "main")
ok, tx = await adapter.deposit(strategy="0x...", amount=10**18)
```

## Execution checklist

1. Confirm strategy metadata or current delegation state first.
2. Use raw token/share units expected by the adapter.
3. Record withdrawal roots from queue transactions.
4. Treat claims as proof-driven operations, not discoverable amounts.
5. Re-read `get_pos(...)` or `get_full_user_state(...)` after state changes.
