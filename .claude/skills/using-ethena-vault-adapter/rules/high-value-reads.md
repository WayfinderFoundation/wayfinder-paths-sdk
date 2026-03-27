# Ethena sUSDe reads (APY + cooldown + positions)

## Data accuracy (no guessing)

- Do **not** invent APYs, balances, or cooldown timestamps.
- Only report values fetched via the adapter.
- If an RPC call fails, respond with "unavailable" and provide the exact call/script to reproduce.

## Primary data source

- Adapter: `wayfinder_paths/adapters/ethena_vault_adapter/adapter.py`
- Addresses (USDe/sUSDe/ENA by chain): `wayfinder_paths/core/constants/ethena_contracts.py`

Notes:
- The canonical sUSDe staking vault lives on **Ethereum mainnet**.
- For `chain_id != 1`, `get_full_user_state(...)` reads USDe/sUSDe balances on the target chain, but reads cooldown + `convertToAssets` from mainnet.

## Ad-hoc read scripts

All ad-hoc scripts go under `.wayfinder_runs/` and use `get_adapter()`.

### Spot APY (mainnet vault)

```python
"""Compute Ethena sUSDe spot APY from the vault vesting model."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.ethena_vault_adapter import EthenaVaultAdapter

async def main():
    adapter = await get_adapter(EthenaVaultAdapter)  # read-only (no wallet needed)
    ok, apy = await adapter.get_apy()
    if not ok:
        raise RuntimeError(apy)
    print("apy=", apy)

if __name__ == "__main__":
    asyncio.run(main())
```

### Cooldown status (mainnet vault)

```python
"""Read cooldownEnd + underlyingAmount for a wallet (mainnet vault)."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.ethena_vault_adapter import EthenaVaultAdapter

USER = "0x0000000000000000000000000000000000000000"

async def main():
    adapter = await get_adapter(EthenaVaultAdapter)
    ok, cd = await adapter.get_cooldown(account=USER)
    if not ok:
        raise RuntimeError(cd)
    print(cd)

if __name__ == "__main__":
    asyncio.run(main())
```

### Full user state (balances + USDe equivalent + cooldown + optional APY)

```python
"""Fetch a user’s USDe/sUSDe balances and cooldown (optionally includes spot APY)."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.ethena_vault_adapter import EthenaVaultAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

USER = "0x0000000000000000000000000000000000000000"

async def main():
    adapter = await get_adapter(EthenaVaultAdapter)
    ok, state = await adapter.get_full_user_state(
        account=USER,
        chain_id=CHAIN_ID_ETHEREUM,
        include_apy=True,
        include_zero_positions=False,
    )
    if not ok:
        raise RuntimeError(state)
    print(state)

if __name__ == "__main__":
    asyncio.run(main())
```

## Key read methods

| Method | Purpose | Wallet needed? |
|--------|---------|----------------|
| `get_apy()` | Spot supply APY (vesting-based estimate) | No |
| `get_cooldown(account)` | Cooldown end timestamp + underlying amount | No |
| `get_full_user_state(account, chain_id?, include_apy?, include_zero_positions?)` | USDe/sUSDe balances, USDe equivalent, cooldown, optional `apySupply` | No |
