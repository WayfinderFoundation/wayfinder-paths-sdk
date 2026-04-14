# OndoRwaAdapter

Permissioned Ondo RWA adapter for subscribe/redeem and wrap/unwrap flows.

- **Type**: `ONDO_RWA`
- **Module**: `wayfinder_paths.adapters.ondo_rwa_adapter.adapter.OndoRwaAdapter`

## Scope

- Ethereum mainnet: `OUSG`, `rOUSG`, `USDY`, `rUSDY`
- Mantle: `USDY` <-> `mUSD` wrapper flow
- Read-only v1: Polygon `OUSG`, Arbitrum `USDY`

This adapter models Ondo as a permissioned RWA subscribe/redeem plus wrapper protocol. It does not expose lending-style methods such as `borrow`, `repay`, `set_collateral`, or `claim_rewards`.

## Methods

- `subscribe(product, deposit_token, amount, min_received, ...)`
- `redeem(product, amount, receiving_token, min_received, ...)`
- `wrap(product|token_address, amount, ...)`
- `unwrap(product|token_address, amount, ...)`
- `get_all_markets()`
- `get_pos(account, ...)`
- `get_full_user_state(account, ...)`

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/ondo_rwa_adapter/test_adapter.py -v
poetry run pytest wayfinder_paths/adapters/ondo_rwa_adapter/test_gorlami_simulation.py -v
```
