# Ethena Vault Adapter

Adapter for the [Ethena](https://ethena.fi/) sUSDe staking vault on Ethereum mainnet.

- **Type**: `ETHENA`
- **Module**: `wayfinder_paths.adapters.ethena_vault_adapter.adapter.EthenaVaultAdapter`

## Overview

The EthenaVaultAdapter provides:
- Staking USDe into the canonical sUSDe ERC-4626 vault (deposit)
- Two-step withdrawal via cooldown (`cooldownShares`/`cooldownAssets`) then `unstake`
- Position and cooldown queries (supports non-mainnet sUSDe balances via OFT)
- Spot APY derived from Ethena's linear vesting model

## Key Addresses (Ethereum Mainnet)

| Contract | Address |
|----------|---------|
| USDe | `0x4c9EDD5852cd905f086C759E8383e09bff1E68B3` |
| sUSDe Vault | `0x9D39A5DE30e57443BfF2A8307A4256c8797A3497` |
| ENA | `0x57e114B691Db790C35207b2e685D4A43181e6061` |

On non-mainnet EVM chains, USDe/sUSDe/ENA are LayerZero OFT tokens. See `ethena_contracts.py` for per-chain addresses.

## Usage

```python
from wayfinder_paths.adapters.ethena_vault_adapter import EthenaVaultAdapter
from wayfinder_paths.mcp.scripting import get_adapter

adapter = get_adapter(EthenaVaultAdapter, "main")
```

## Methods

### get_apy

Compute a spot supply APY from Ethena's linear vesting model (~8-hour vesting periods).

```python
ok, apy = await adapter.get_apy()
```

### get_cooldown

Check the cooldown status for an account.

```python
ok, cd = await adapter.get_cooldown(account="0x...")
# cd = {"cooldownEnd": 1700100000, "underlyingAmount": 50000000000000000000}
```

### get_full_user_state

Fetch a user's full Ethena position: USDe/sUSDe balances, USDe equivalent, cooldown status, and optionally APY.

```python
ok, state = await adapter.get_full_user_state(
    account="0x...",
    chain_id=1,              # default: Ethereum mainnet
    include_apy=True,
    include_zero_positions=False,
)
```

Returns a dict with keys:
- `protocol`, `hubChainId`, `chainId`, `account`
- `positions` (list with balance/cooldown/APY per chain)

### Deposit

Stake USDe into the sUSDe vault (ERC-4626 `deposit`). Handles approval automatically.

```python
ok, tx_hash = await adapter.deposit_usde(
    amount_assets=100 * 10**18,
    receiver="0x...",  # optional, defaults to wallet_address
)
```

### Withdraw (two-step)

**Step 1 - Start cooldown** (choose one):

```python
# By shares (sUSDe amount)
ok, tx_hash = await adapter.request_withdraw_by_shares(shares=50 * 10**18)

# By assets (USDe amount)
ok, tx_hash = await adapter.request_withdraw_by_assets(assets=100 * 10**18)
```

**Step 2 - Claim after cooldown expires:**

```python
ok, tx_hash = await adapter.claim_withdraw(
    receiver="0x...",       # optional, defaults to wallet_address
    require_matured=True,   # pre-checks cooldown is finished
)
```

## Return Format

All methods return `(success: bool, data: Any)` tuples.

## Testing

```bash
poetry run pytest wayfinder_paths/adapters/ethena_vault_adapter/test_adapter.py -v
```
