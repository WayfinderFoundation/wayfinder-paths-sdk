# EigenCloud Adapter

Adapter for EigenCloud (EigenLayer) restaking on Ethereum mainnet.

- **Type**: `EIGENCLOUD`
- **Module**: `wayfinder_paths.adapters.eigencloud_adapter.adapter.EigenCloudAdapter`

## Supported Flows

- List supported restaking strategies and market metadata via `get_all_markets()`
- Deposit into whitelisted strategies via `deposit()` with automatic ERC-20 approval
- Read delegation state via `get_delegation_state()`
- Delegate, undelegate, and redelegate via `delegate()`, `undelegate()`, and `redelegate()`
- Queue withdrawals, decode `withdrawal_roots`, inspect queued withdrawals, and complete matured withdrawals
- Read positions and combined account state via `get_pos()` and `get_full_user_state()`
- Rewards helpers: read metadata, set claimer, validate prepared claims, and submit prepared claims or raw calldata

## Notes

- Ethereum mainnet only
- Write methods require `wallet_address` and `sign_callback`
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
