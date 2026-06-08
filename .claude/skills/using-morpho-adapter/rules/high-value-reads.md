# Morpho reads (markets + vaults + positions)

## Data accuracy (no guessing)

- Do **not** invent APYs, reward APRs, or health metrics.
- Use the adapter/clients to fetch from Morpho API and/or on-chain contracts.

## Primary data source

- Adapter: `wayfinder_paths/adapters/morpho_adapter/adapter.py`
- Off-chain reads use `MorphoClient` (GraphQL): `wayfinder_paths/core/clients/MorphoClient.py`

## High-value reads

### List markets on a chain

```python
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.morpho_adapter import MorphoAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

async def main():
    adapter = await get_adapter(MorphoAdapter)  # read-only
    ok, markets = await adapter.get_all_markets(chain_id=CHAIN_ID_BASE)
    if not ok:
        raise RuntimeError(markets)
    for m in markets[:10]:
        print(
            m.get("marketId"),
            m.get("loan", {}).get("symbol"),
            "supply_apy=",
            (m.get("state") or {}).get("supply_apy"),
            "supply_assets_usd=",
            (m.get("state") or {}).get("supply_assets_usd"),
        )

if __name__ == "__main__":
    asyncio.run(main())
```

### User snapshot (per-chain)

```python
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.morpho_adapter import MorphoAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

USER = "0x0000000000000000000000000000000000000000"

async def main():
    adapter = await get_adapter(MorphoAdapter)
    ok, state = await adapter.get_full_user_state_per_chain(chain_id=CHAIN_ID_BASE, account=USER)
    if not ok:
        raise RuntimeError(state)
    for p in state.get("positions", []):
        print(p.get("marketUniqueKey"), "health=", p.get("healthFactor"))

if __name__ == "__main__":
    asyncio.run(main())
```

### List vaults (Morpho Vault V1 + V2)

```python
ok, vaults = await adapter.get_all_vaults(chain_id=CHAIN_ID_BASE, include_v2=True)
```

Vault entries are distinct from market entries. Vault deposit/withdraw uses a `vault_address`; market supply/borrow uses a `market_unique_key` argument that should contain the current Morpho `marketId`.

## Key read methods

| Method | Purpose | Wallet needed? |
|--------|---------|----------------|
| `get_all_markets(chain_id, listed?, include_idle?)` | Market list + point-in-time APYs/rewards/warnings; returns `marketId` plus `uniqueKey` alias | No |
| `get_market_state(chain_id, market_unique_key)` | Single market state + allocator liquidity/vault links. Pass Morpho `marketId` as `market_unique_key` | No |
| `get_market_historical_apy(chain_id, market_unique_key, interval, start_timestamp?, end_timestamp?)` | APY time series | No |
| `get_full_user_state_per_chain(chain_id, account, include_zero_positions?)` | Positions snapshot | No (if you pass `account`) |
| `get_claimable_rewards(chain_id, account?)` | Claimable Merkl rewards by default; legacy URD only when explicitly requested | No (if you pass `account`) |
| `get_all_vaults(chain_id, listed?, include_v2?)` | Vault list + APY/rewards; Vault V2 includes share price, idle/liquidity, and adapter allocations | No |
