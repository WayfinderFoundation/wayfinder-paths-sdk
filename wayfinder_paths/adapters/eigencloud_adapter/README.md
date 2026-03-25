# EigenCloud Adapter

Adapter for EigenCloud (EigenLayer) restaking on Ethereum mainnet.

- **Type**: `EIGENCLOUD`
- **Module**: `wayfinder_paths.adapters.eigencloud_adapter.adapter.EigenCloudAdapter`

## Supported Flows

- List supported restaking strategies and market metadata via `get_all_markets()`
- Deposit into whitelisted strategies via `deposit()` with automatic ERC-20 approval
- Delegate, undelegate, and redelegate via `delegate()`, `undelegate()`, and `redelegate()`
- Queue withdrawals via `queue_withdrawals()` and submit withdrawal completion transactions for eligible queued withdrawals via `complete_withdrawal()`
- Read positions via `get_pos()` and combined account state via `get_full_user_state()`
- Rewards reads and claims via `get_rewards_metadata()`, `check_claim()`, `claim_rewards()`, `claim_rewards_batch()`, and `claim_rewards_calldata()`

## Notes

- Ethereum mainnet only
- Write methods require `wallet_address` and `sign_callback`
- `get_full_user_state()` includes queued withdrawals only when `withdrawal_roots` are provided
- Withdrawal eligibility is enforced onchain by EigenLayer; `complete_withdrawal()` submits the completion transaction but does not pre-check the delay
- Rewards proof generation is not implemented here; claim methods expect a prepared claim struct or calldata

## Quick Usage

```python
from wayfinder_paths.adapters.eigencloud_adapter import EigenCloudAdapter
from wayfinder_paths.core.constants.contracts import EIGENCLOUD_STRATEGIES

adapter = EigenCloudAdapter(
    sign_callback=sign_cb,
    wallet_address="0x...",
)

ok, markets = await adapter.get_all_markets()
ok, tx = await adapter.deposit(
    strategy=EIGENCLOUD_STRATEGIES["stETH"],
    amount=10**18,
)
```

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/eigencloud_adapter/ -v
```
