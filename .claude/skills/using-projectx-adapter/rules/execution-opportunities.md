# ProjectX execution (ad-hoc scripts)

## Execution pattern

All write operations use ad-hoc scripts under `.wayfinder_runs/`:

1. Write a script using `get_adapter(ProjectXLiquidityAdapter, "wallet_label", config_overrides={...})`
2. Run via `mcp__wayfinder__run_script(script_path, wallet_label)`

## Mint (strategy helper)

`mint_from_balances()` is the preferred entrypoint: it uses the configured pool’s `tick_spacing`
and can do small balancing swaps before minting.

```python
import asyncio

from wayfinder_paths.adapters.projectx_adapter.adapter import ProjectXLiquidityAdapter
from wayfinder_paths.core.constants.projectx import THBILL_USDC_POOL
from wayfinder_paths.core.utils.uniswap_v3_math import ticks_for_range
from wayfinder_paths.mcp.scripting import get_adapter

BAND_BPS = 50  # ±0.5%

async def main():
    adapter = get_adapter(
        ProjectXLiquidityAdapter,
        "main",
        config_overrides={"pool_address": THBILL_USDC_POOL},
    )

    ok, overview = await adapter.pool_overview()
    if not ok:
        raise RuntimeError(overview)

    tick = int(overview["tick"])
    spacing = int(overview["tick_spacing"])
    tick_lower, tick_upper = ticks_for_range(tick, bps=BAND_BPS, spacing=spacing)

    ok, out = await adapter.mint_from_balances(tick_lower, tick_upper, slippage_bps=300)
    print(ok, out)

asyncio.run(main())
```

## Increase liquidity (balanced helper)

```python
import asyncio

from wayfinder_paths.adapters.projectx_adapter.adapter import ProjectXLiquidityAdapter
from wayfinder_paths.core.constants.projectx import THBILL_USDC_POOL
from wayfinder_paths.mcp.scripting import get_adapter

TOKEN_ID = 123

async def main():
    adapter = get_adapter(
        ProjectXLiquidityAdapter,
        "main",
        config_overrides={"pool_address": THBILL_USDC_POOL},
    )

    ok, pos = await adapter.get_position(TOKEN_ID)
    if not ok:
        raise RuntimeError(pos)

    ok, out = await adapter.increase_liquidity_balanced(
        TOKEN_ID,
        tick_lower=int(pos["tick_lower"]),
        tick_upper=int(pos["tick_upper"]),
        slippage_bps=200,
    )
    print(ok, out)

asyncio.run(main())
```

## Remove + burn (close position)

```python
import asyncio

from wayfinder_paths.adapters.projectx_adapter.adapter import ProjectXLiquidityAdapter
from wayfinder_paths.core.constants.projectx import THBILL_USDC_POOL
from wayfinder_paths.mcp.scripting import get_adapter

TOKEN_ID = 123

async def main():
    adapter = get_adapter(
        ProjectXLiquidityAdapter,
        "main",
        config_overrides={"pool_address": THBILL_USDC_POOL},
    )
    ok, tx = await adapter.burn_position(TOKEN_ID)
    print(ok, tx)

asyncio.run(main())
```

## Swap exact in

`swap_exact_in()` only supports ERC20 addresses (use wrapped HYPE for “native”).

```python
import asyncio

from wayfinder_paths.adapters.projectx_adapter.adapter import ProjectXLiquidityAdapter
from wayfinder_paths.core.constants.projectx import THBILL_TOKEN, THBILL_USDC_POOL, USDC_TOKEN
from wayfinder_paths.mcp.scripting import get_adapter

async def main():
    adapter = get_adapter(
        ProjectXLiquidityAdapter,
        "main",
        config_overrides={"pool_address": THBILL_USDC_POOL},
    )
    ok, tx = await adapter.swap_exact_in(USDC_TOKEN, THBILL_TOKEN, 1_000_000, slippage_bps=50)
    print(ok, tx)

asyncio.run(main())
```

