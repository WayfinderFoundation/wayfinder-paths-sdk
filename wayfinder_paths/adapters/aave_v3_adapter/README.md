# Aave v3 Adapter

Adapter for Aave v3 pools across supported chains.

- **Type**: `AAVE_V3`
- **Module**: `wayfinder_paths.adapters.aave_v3_adapter.adapter.AaveV3Adapter`

## Methods

### get_all_markets (on-chain)

Fetch reserve snapshots via `UiPoolDataProvider.getReservesData(...)` and (optionally)
incentives via `UiIncentiveDataProviderV3.getReservesIncentivesData(...)`.

```python
from wayfinder_paths.adapters.aave_v3_adapter import AaveV3Adapter

adapter = AaveV3Adapter(config={})
ok, markets = await adapter.get_all_markets(chain_id=42161, include_rewards=True)
```

### get_full_user_state (all chains)

Queries all supported Aave V3 chains and merges positions into a single result.

```python
ok, state = await adapter.get_full_user_state(account="0x...")
# state["positions"] includes a "chain_id" field on each position
```

### get_full_user_state_per_chain (single chain)

Fetch user supplies/borrows via `UiPoolDataProvider.getUserReservesData(...)` and
(optionally) claimable incentives via `UiIncentiveDataProviderV3.getUserReservesIncentivesData(...)`.

```python
ok, state = await adapter.get_full_user_state_per_chain(chain_id=42161, account="0x...")
```

### lend / unlend / borrow / repay

Core pool operations (variable rate mode = `2`).

```python
ok, tx = await adapter.lend(chain_id=42161, underlying_token="0x...", qty=123)
ok, tx = await adapter.unlend(chain_id=42161, underlying_token="0x...", qty=123)
ok, tx = await adapter.borrow(chain_id=42161, underlying_token="0x...", qty=123)
ok, tx = await adapter.repay(chain_id=42161, underlying_token="0x...", qty=123)
```

### set_collateral / remove_collateral

Enable/disable supplied assets as collateral.

```python
ok, tx = await adapter.set_collateral(chain_id=42161, underlying_token="0x...")
ok, tx = await adapter.remove_collateral(chain_id=42161, underlying_token="0x...")
```

### claim_all_rewards

Claims all rewards via the per-chain RewardsController.

```python
ok, tx = await adapter.claim_all_rewards(chain_id=42161)
```

