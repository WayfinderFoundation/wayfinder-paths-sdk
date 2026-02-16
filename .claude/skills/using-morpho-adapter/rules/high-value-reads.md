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
    adapter = get_adapter(MorphoAdapter)  # read-only
    ok, markets = await adapter.get_all_markets(chain_id=CHAIN_ID_BASE)
    if not ok:
        raise RuntimeError(markets)
    for m in markets[:10]:
        print(m.get("uniqueKey"), m.get("loanAsset", {}).get("symbol"), "supply_apy=", m.get("supply_apy"))

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
    adapter = get_adapter(MorphoAdapter)
    ok, state = await adapter.get_full_user_state_per_chain(chain_id=CHAIN_ID_BASE, account=USER)
    if not ok:
        raise RuntimeError(state)
    for p in state.get("positions", []):
        print(p.get("marketUniqueKey"), "health=", p.get("healthFactor"))

if __name__ == "__main__":
    asyncio.run(main())
```

### List vaults (MetaMorpho)

```python
ok, vaults = await adapter.get_all_vaults(chain_id=CHAIN_ID_BASE, include_v2=True)
```

## Key read methods

| Method | Purpose | Wallet needed? |
|--------|---------|----------------|
| `get_all_markets(chain_id, listed?, include_idle?)` | Market list + point-in-time APYs/rewards/warnings | No |
| `get_market_state(chain_id, market_unique_key)` | Single market state + allocator liquidity/vault links | No |
| `get_market_historical_apy(chain_id, market_unique_key, interval, start_timestamp?, end_timestamp?)` | APY time series | No |
| `get_full_user_state_per_chain(chain_id, account, include_zero_positions?)` | Positions snapshot | No (if you pass `account`) |
| `get_claimable_rewards(chain_id, account?)` | Claimable Merkl + URD rewards | No (if you pass `account`) |
| `get_all_vaults(chain_id, listed?, include_v2?)` | Vault list + APY/rewards | No |
