# Euler v2 reads (vaults + positions)

## Data accuracy (no guessing)

- Do **not** invent or estimate APYs, caps, cash, totals, or LTVs.
- Only report values fetched from Euler contracts via the adapter.
- If an RPC call fails, respond with "unavailable" and provide the exact script/call to reproduce.

## Primary data source

- Adapter: `wayfinder_paths/adapters/euler_v2_adapter/adapter.py`
- Deployments/perspectives: `wayfinder_paths/core/constants/euler_v2_contracts.py`

Terminology:
- **Vault** = market address and also the ERC-4626 share token.
- **Underlying** = `vault.asset()`
- **Debt token** = `vault.dToken()`

## Ad-hoc read scripts

All read scripts go under `.wayfinder_runs/` and use `get_adapter()`:

### List verified vaults (by perspective)

```python
"""List Euler v2 verified vaults for a chain/perspective."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

async def main():
    adapter = get_adapter(EulerV2Adapter)  # read-only, no wallet needed
    ok, vaults = await adapter.get_verified_vaults(chain_id=CHAIN_ID_BASE, perspective="governed", limit=50)
    if not ok:
        raise RuntimeError(vaults)
    for v in vaults:
        print(v)

if __name__ == "__main__":
    asyncio.run(main())
```

### Fetch markets (vault list + APYs + caps + LTV rows)

```python
"""Fetch Euler v2 markets for a chain."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

async def main():
    adapter = get_adapter(EulerV2Adapter)  # read-only, no wallet needed
    ok, markets = await adapter.get_all_markets(chain_id=CHAIN_ID_BASE, perspective="governed", limit=60, concurrency=10)
    if not ok:
        raise RuntimeError(markets)
    for m in markets:
        print(
            m.get("asset_symbol"),
            "vault=", m.get("vault"),
            "supply_apy=", m.get("supply_apy"),
            "borrow_apy=", m.get("borrow_apy"),
            "cash=", m.get("cash"),
        )

if __name__ == "__main__":
    asyncio.run(main())
```

### Fetch a single vault’s full info (raw lens output)

```python
"""Fetch Euler v2 vault info from VaultLens (raw-ish, but structured)."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

VAULT = "0x0000000000000000000000000000000000000000"

async def main():
    adapter = get_adapter(EulerV2Adapter)
    ok, info = await adapter.get_vault_info_full(chain_id=CHAIN_ID_BASE, vault=VAULT)
    if not ok:
        raise RuntimeError(info)
    print("asset=", info.get("asset"), "symbol=", info.get("assetSymbol"), "supplyCap=", info.get("supplyCap"))

if __name__ == "__main__":
    asyncio.run(main())
```

### Fetch a user snapshot (enabled vaults + balances)

```python
"""Fetch Euler v2 user snapshot for a chain."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.euler_v2_adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

USER = "0x0000000000000000000000000000000000000000"

async def main():
    adapter = get_adapter(EulerV2Adapter)  # read-only, no wallet needed
    ok, state = await adapter.get_full_user_state(chain_id=CHAIN_ID_BASE, account=USER, include_zero_positions=False)
    if not ok:
        raise RuntimeError(state)
    for p in state.get("positions", []):
        if int(p.get("assets") or 0) or int(p.get("borrowed") or 0):
            print(
                "vault=", p.get("vault"),
                "assets=", p.get("assets"),
                "borrowed=", p.get("borrowed"),
                "is_collateral=", p.get("is_collateral"),
                "is_controller=", p.get("is_controller"),
            )

if __name__ == "__main__":
    asyncio.run(main())
```

## Key read methods

| Method | Purpose | Wallet needed? |
|--------|---------|----------------|
| `get_verified_vaults(chain_id, perspective?, limit?)` | Vault addresses (verified list) | No |
| `get_all_markets(chain_id, perspective?, limit?, concurrency?)` | Vault list + supply/borrow APYs + caps + LTV rows | No |
| `get_vault_info_full(chain_id, vault)` | VaultLens `getVaultInfoFull` output | No |
| `get_full_user_state(chain_id, account, include_zero_positions?)` | Enabled vaults + balances + flags for one chain | No (if you pass `account`) |
