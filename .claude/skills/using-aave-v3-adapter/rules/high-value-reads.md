# Aave V3 reads (markets + positions)

## Data accuracy (no guessing)

- Do **not** invent or estimate APYs, reward APRs, caps, or LTVs.
- Only report values fetched from Aave contracts via the adapter.
- If an RPC call fails, respond with "unavailable" and provide the exact script/call to reproduce.

## Primary data source

- Adapter: `wayfinder_paths/adapters/aave_v3_adapter/adapter.py`
- Reads:
  - `get_all_markets(chain_id, include_rewards=...)`
  - `get_full_user_state_per_chain(chain_id, account, include_rewards=...)`

## Ad-hoc read scripts

All read scripts go under `.wayfinder_runs/` and use `get_adapter()`:

### List markets on a chain

```python
"""Fetch Aave V3 markets (base rates + optional incentives)."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.aave_v3_adapter import AaveV3Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ARBITRUM

async def main():
    adapter = get_adapter(AaveV3Adapter)  # read-only, no wallet needed
    ok, markets = await adapter.get_all_markets(chain_id=CHAIN_ID_ARBITRUM, include_rewards=True)
    if not ok:
        raise RuntimeError(markets)
    for m in markets:
        print(
            m.get("symbol"),
            "ltv_bps=", m.get("ltv_bps"),
            "supply_apy=", m.get("supply_apy"),
            "reward_supply_apr=", m.get("reward_supply_apr"),
        )

if __name__ == "__main__":
    asyncio.run(main())
```

### Get a user's per-chain snapshot

```python
"""Fetch Aave V3 user snapshot for a chain."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.aave_v3_adapter import AaveV3Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ARBITRUM

USER = "0x0000000000000000000000000000000000000000"

async def main():
    adapter = get_adapter(AaveV3Adapter)  # read-only if you pass account explicitly
    ok, state = await adapter.get_full_user_state_per_chain(chain_id=CHAIN_ID_ARBITRUM, account=USER, include_rewards=True)
    if not ok:
        raise RuntimeError(state)
    for p in state.get("positions", []):
        if int(p.get("supply_raw") or 0) or int(p.get("variable_borrow_raw") or 0):
            print(p.get("symbol"), "supply_usd=", p.get("supply_usd"), "borrow_usd=", p.get("variable_borrow_usd"))

if __name__ == "__main__":
    asyncio.run(main())
```

## Key read methods

| Method | Purpose | Wallet needed? |
|--------|---------|----------------|
| `get_all_markets(chain_id, include_rewards?)` | Market list + point-in-time rates/rewards | No |
| `get_full_user_state_per_chain(chain_id, account, include_rewards?, include_zero_positions?)` | Positions snapshot on one chain | No (if you pass `account`) |
| `get_full_user_state(account, include_rewards?, include_zero_positions?)` | Positions snapshot across supported chains | No (if you pass `account`) |
