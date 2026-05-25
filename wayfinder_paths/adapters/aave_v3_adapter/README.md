# Aave v3 Adapter

Adapter for Aave v3 pools across supported chains.

- **Type**: `AAVE_V3`
- **Module**: `wayfinder_paths.adapters.aave_v3_adapter.adapter.AaveV3Adapter`

## Methods

### get_all_markets (on-chain)

Fetch reserve snapshots via `UiPoolDataProvider.getReservesData(...)` and (optionally)
incentives via `UiIncentiveDataProviderV3.getReservesIncentivesData(...)`.
The adapter supports the compact current UI data provider shape, Aave Origin
deployments, and legacy Aave V3 periphery deployments used by existing markets.

```python
from wayfinder_paths.adapters.aave_v3_adapter import AaveV3Adapter

adapter = AaveV3Adapter(config={})
ok, markets = await adapter.get_all_markets(chain_id=42161, include_rewards=True)
```

Market rows include current risk and liquidity fields such as `ltv_bps`,
`liquidation_threshold_bps`, `liquidation_bonus_bps`, `reserve_factor_bps`,
`emode_category_id`, `borrowable_in_isolation`, `debt_ceiling`,
`flash_loan_enabled`, `virtual_underlying_balance`, and `deficit` when the
deployed UI provider exposes it.

### get_full_user_state (all chains)

Queries all supported Aave V3 chains and merges positions into a single result.

```python
ok, state = await adapter.get_full_user_state(account="0x...")
# state["positions"] includes a "chain_id" field on each position
```

### get_full_user_state_per_chain (single chain)

Fetch user supplies/borrows via `UiPoolDataProvider.getUserReservesData(...)` and
(optionally) claimable incentives via `UiIncentiveDataProviderV3.getUserReservesIncentivesData(...)`.
The response includes `account_data` from `Pool.getUserAccountData(...)`,
`user_emode_category_id`, and available `emode_categories` where the deployed UI
provider exposes `getEModes(...)`.

When `include_rewards=True` (default), each position includes market-level APY and reward data
computed from `UiPoolDataProvider.getReservesData(...)` and `UiIncentiveDataProviderV3.getReservesIncentivesData(...)`:

| Field | Description |
|-------|-------------|
| `supply_apy` | Base supply APY (from `liquidityRate`) |
| `variable_borrow_apy` | Base variable borrow APY (from `variableBorrowRate`) |
| `reward_supply_apr` | Incentive APR earned on supply side |
| `reward_borrow_apr` | Incentive APR offsetting borrow cost |
| `supply_apy_with_rewards` | `supply_apy + reward_supply_apr` |
| `borrow_apy_with_rewards` | `variable_borrow_apy - reward_borrow_apr` |
| `rewards` | Per-user unclaimed reward entries (token, symbol, unclaimed amount) |

```python
ok, state = await adapter.get_full_user_state_per_chain(chain_id=42161, account="0x...")
for pos in state["positions"]:
    print(pos["symbol"], pos["supply_apy"], pos["reward_supply_apr"])
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

### get_emode_categories / set_emode / disable_emode

Read available eMode categories and switch the strategy wallet into or out of
eMode. `category_id=0` disables eMode.

```python
ok, categories = await adapter.get_emode_categories(chain_id=42161)
ok, tx = await adapter.set_emode(chain_id=42161, category_id=1)
ok, tx = await adapter.disable_emode(chain_id=42161)
```

### claim_all_rewards

Claims all rewards via the per-chain RewardsController.

```python
ok, tx = await adapter.claim_all_rewards(chain_id=42161)
```

### Aave Earn vaults

Earn vault helpers cover ERC-4626 vault reads and the Aave aToken variants.
Pass an explicit vault address; the adapter resolves the underlying asset via
`asset()` and the aToken through the configured Aave V3 market.

```python
ok, vault = await adapter.get_earn_vault_state(
    chain_id=42161,
    vault_address="0x...",
    account="0x...",
)

ok, tx = await adapter.earn_vault_deposit(chain_id=42161, vault_address="0x...", assets=123)
ok, tx = await adapter.earn_vault_deposit_atokens(chain_id=42161, vault_address="0x...", assets=123)
ok, tx = await adapter.earn_vault_mint(chain_id=42161, vault_address="0x...", shares=123)
ok, tx = await adapter.earn_vault_mint_with_atokens(chain_id=42161, vault_address="0x...", shares=123)
ok, tx = await adapter.earn_vault_withdraw(chain_id=42161, vault_address="0x...", assets=123)
ok, tx = await adapter.earn_vault_withdraw_atokens(chain_id=42161, vault_address="0x...", assets=123)
ok, tx = await adapter.earn_vault_redeem(chain_id=42161, vault_address="0x...", shares=123)
ok, tx = await adapter.earn_vault_redeem_as_atokens(chain_id=42161, vault_address="0x...", shares=123)
```
