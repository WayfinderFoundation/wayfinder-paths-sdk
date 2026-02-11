# ProjectX reads (pool state + positions + swaps)

## Data accuracy (no guessing)

- Do **not** invent or estimate ticks, prices, fees, or volumes.
- Only report values fetched from on-chain contracts (RPC) or the ProjectX subgraph/points endpoints.
- If you can’t fetch data (missing RPCs / network), respond with “unavailable” and show the exact script call needed.

## Full user state (positions + balances + points in one call)

`get_full_user_state()` is the single best method for checking a wallet's ProjectX positions.

**Pool-agnostic** (no `pool_address` needed): returns all positions across all pools + points.
Overview and balances are silently skipped when no pool is configured.

```python
import asyncio

from wayfinder_paths.adapters.projectx_adapter.adapter import ProjectXLiquidityAdapter
from wayfinder_paths.mcp.scripting import get_adapter


async def main():
    adapter = get_adapter(ProjectXLiquidityAdapter, "main")

    ok, state = await adapter.get_full_user_state()
    print("ok:", ok)
    if ok:
        print("positions:", state["positions"])
        print("points:", state["points"])

asyncio.run(main())
```

**Pool-scoped** (with `pool_address`): also returns pool overview and token balances.

```python
import asyncio

from wayfinder_paths.adapters.projectx_adapter.adapter import ProjectXLiquidityAdapter
from wayfinder_paths.core.constants.projectx import THBILL_USDC_POOL
from wayfinder_paths.mcp.scripting import get_adapter


async def main():
    adapter = get_adapter(
        ProjectXLiquidityAdapter,
        "main",
        config_overrides={"pool_address": THBILL_USDC_POOL},
    )

    ok, state = await adapter.get_full_user_state()
    print("ok:", ok)
    if ok:
        print("positions:", state["positions"])
        print("balances:", state["balances"])
        print("points:", state["points"])
        print("pool:", state["poolOverview"])
        if state["errors"]:
            print("errors:", state["errors"])

asyncio.run(main())
```

Optional flags: `include_overview`, `include_balances`, `include_positions`, `include_points` (all default `True`).

## Primary data sources

- Adapter: `wayfinder_paths/adapters/projectx_adapter/adapter.py`
  - Class: `ProjectXLiquidityAdapter` (pool-scoped)
- Addresses:
  - `PRJX_NPM`, `PRJX_ROUTER`: `wayfinder_paths/core/constants/contracts.py`
  - `PRJX_FACTORY`: `wayfinder_paths/core/constants/projectx.py`
- ABIs:
  - `PROJECTX_POOL_ABI`, `PROJECTX_ROUTER_ABI`, `PROJECTX_FACTORY_ABI`: `wayfinder_paths/core/constants/projectx_abi.py`
  - `NONFUNGIBLE_POSITION_MANAGER_ABI`: `wayfinder_paths/core/constants/uniswap_v3_abi.py`
- Pool constants (example pool + token addresses): `wayfinder_paths/core/constants/projectx.py`
- Subgraph URL resolution: `get_prjx_subgraph_url(config)`

## Required configuration

- `config.json` must include RPC URLs for HyperEVM **chain id 999** under `rpcs["999"]`.
- `pool_address` is optional. Pool-specific methods (`pool_overview`, `current_balances`, `list_positions`, `fetch_swaps`, `live_fee_snapshot`) require it; cross-pool reads (`get_full_user_state` without overview/balances, `_list_all_positions`, `fetch_prjx_points`) do not.
- Accepts `pool_address`, `pool`, `projectx_pool_address`, `projectx_pool` in config (also checks nested `strategy` config).

## Ad-hoc read scripts

### Pool overview + balances (configured pool)

```python
import asyncio

from wayfinder_paths.adapters.projectx_adapter.adapter import ProjectXLiquidityAdapter
from wayfinder_paths.core.constants.projectx import THBILL_USDC_POOL
from wayfinder_paths.mcp.scripting import get_adapter

async def main():
    adapter = get_adapter(
        ProjectXLiquidityAdapter,
        "main",
        config_overrides={"pool_address": THBILL_USDC_POOL},
    )

    ok, overview = await adapter.pool_overview()
    print("overview:", ok, overview)

    ok, balances = await adapter.current_balances()
    print("balances:", ok, balances)

asyncio.run(main())
```

### List active positions for the configured pool

`list_positions()` is **pool-scoped**: it filters wallet positions down to those matching
the configured pool’s token0/token1/fee.

```python
import asyncio

from wayfinder_paths.adapters.projectx_adapter.adapter import ProjectXLiquidityAdapter
from wayfinder_paths.core.constants.projectx import THBILL_USDC_POOL
from wayfinder_paths.mcp.scripting import get_adapter

async def main():
    adapter = get_adapter(
        ProjectXLiquidityAdapter,
        "main",
        config_overrides={"pool_address": THBILL_USDC_POOL},
    )

    ok, positions = await adapter.list_positions()
    print("ok:", ok)
    if ok:
        for p in positions:
            print(f"id={p.token_id} liq={p.liquidity} ticks=[{p.tick_lower}, {p.tick_upper}]")

asyncio.run(main())
```

### Fetch recent swaps (subgraph)

`start_timestamp` / `end_timestamp` are **unix seconds**.

```python
import asyncio
import time

from wayfinder_paths.adapters.projectx_adapter.adapter import ProjectXLiquidityAdapter
from wayfinder_paths.core.constants.projectx import THBILL_USDC_POOL
from wayfinder_paths.mcp.scripting import get_adapter

async def main():
    adapter = get_adapter(
        ProjectXLiquidityAdapter,
        "main",
        config_overrides={"pool_address": THBILL_USDC_POOL},
    )

    now = int(time.time())
    ok, swaps = await adapter.fetch_swaps(
        limit=50,
        start_timestamp=now - 3600,
        end_timestamp=now,
    )
    print("ok:", ok)
    if ok:
        print("n_swaps:", len(swaps))
        print("example:", swaps[0] if swaps else None)

asyncio.run(main())
```

### Fetch ProjectX points (API)

```python
import asyncio

from wayfinder_paths.adapters.projectx_adapter.adapter import ProjectXLiquidityAdapter

WALLET = "0x0000000000000000000000000000000000000000"

async def main():
    ok, pts = await ProjectXLiquidityAdapter.fetch_prjx_points(WALLET)
    print("ok:", ok, "points:", pts)

asyncio.run(main())
```

## Key read methods

| Method | Purpose | Notes |
|--------|---------|-------|
| `get_full_user_state()` | All-in-one: overview + balances + positions + points | Pool-agnostic (skips overview/balances without pool) |
| `pool_overview()` | Pool tick/spacing/fee + token metadata | Requires `pool_address` |
| `current_balances(owner=...)` | Raw balances for pool token0/token1 | Requires `pool_address` |
| `list_positions(owner=...)` | Active NPM positions for this pool | Requires `pool_address` |
| `fetch_swaps(limit=..., start_timestamp=..., end_timestamp=...)` | Recent swap history | Subgraph (HTTP) |
| `fetch_prjx_points(wallet_address)` | Points program totals | HTTP API |
| `get_position(token_id)` | Single position struct | Inherited from `UniswapV3BaseAdapter` |
| `get_positions(owner=...)` | All NPM positions for an owner | Not pool-filtered |
| `get_uncollected_fees(token_id)` | Pending fees (amount0/amount1) | Simulates `collect(...)` via `call()` |

