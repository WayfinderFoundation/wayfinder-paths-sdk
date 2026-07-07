# Uniswap V3 execution (ad-hoc scripts)

## Execution pattern

All write operations use ad-hoc scripts under `.wayfinder_runs/`:

1. Write script with `get_adapter(UniswapAdapter, "wallet_label")`
2. Run via `mcp__wayfinder__core_run_script(script_path, wallet_label)`

## Add liquidity (new position)

```python
"""Add ETH/USDC liquidity on Base with ±5% range."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.uniswap_adapter import UniswapAdapter
from wayfinder_paths.core.constants.contracts import BASE_WETH, BASE_USDC
from wayfinder_paths.core.utils.uniswap_v3_math import get_pool_slot0, ticks_for_range

CHAIN_ID = 8453
FEE = 500
TICK_SPACING = 10
BAND_BPS = 500  # ±5%

async def main():
    adapter = await get_adapter(UniswapAdapter, "main")

    _, pool_address = await adapter.get_pool(BASE_WETH, BASE_USDC, FEE)
    slot0 = await get_pool_slot0(pool_address, CHAIN_ID, 18, 6)

    current_tick = slot0["tick"]
    eth_price = slot0["price"]

    tick_lower, tick_upper = ticks_for_range(current_tick, BAND_BPS, TICK_SPACING)

    # ~$1 of each side
    weth_amount = int((1.0 / eth_price) * 10**18)
    usdc_amount = 1_000_000  # 1 USDC

    _, tx_hash = await adapter.add_liquidity(
        token0=BASE_WETH,
        token1=BASE_USDC,
        fee=FEE,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        amount0_desired=weth_amount,
        amount1_desired=usdc_amount,
        slippage_bps=300,  # 3% — safer for small positions
    )
    print(f"Minted! TX: {tx_hash}")

asyncio.run(main())
```

## Remove liquidity (close position)

```python
"""Remove all liquidity, collect fees, burn NFT."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.uniswap_adapter import UniswapAdapter

TOKEN_ID = 12345

async def main():
    adapter = await get_adapter(UniswapAdapter, "main")
    _, pos = await adapter.get_position(TOKEN_ID)
    print(f"Liquidity: {pos['liquidity']}")

    _, tx_hash = await adapter.remove_liquidity(
        TOKEN_ID,
        collect=True,  # collect fees in same tx
        burn=True,      # burn the NFT (only if fully removing)
    )
    print(f"Removed! TX: {tx_hash}")

asyncio.run(main())
```

## Partial remove (decrease liquidity)

```python
"""Remove half the liquidity from a position."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.uniswap_adapter import UniswapAdapter

TOKEN_ID = 12345

async def main():
    adapter = await get_adapter(UniswapAdapter, "main")
    _, pos = await adapter.get_position(TOKEN_ID)
    half = pos['liquidity'] // 2

    _, tx_hash = await adapter.remove_liquidity(
        TOKEN_ID,
        liquidity=half,
        collect=True,
        burn=False,  # keep the NFT
    )
    print(f"Decreased! TX: {tx_hash}")

asyncio.run(main())
```

## Increase liquidity (add to existing position)

```python
"""Add more tokens to an existing position."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.uniswap_adapter import UniswapAdapter

TOKEN_ID = 12345

async def main():
    adapter = await get_adapter(UniswapAdapter, "main")
    _, tx_hash = await adapter.increase_liquidity(
        token_id=TOKEN_ID,
        amount0_desired=500_000_000_000_000,  # 0.0005 WETH
        amount1_desired=1_000_000,             # 1 USDC
        slippage_bps=300,
    )
    print(f"Increased! TX: {tx_hash}")

asyncio.run(main())
```

## Collect fees only

```python
"""Collect accrued fees without removing liquidity."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.uniswap_adapter import UniswapAdapter

TOKEN_ID = 12345

async def main():
    adapter = await get_adapter(UniswapAdapter, "main")
    _, tx_hash = await adapter.collect_fees(TOKEN_ID)
    print(f"Collected! TX: {tx_hash}")

asyncio.run(main())
```

## Key execution methods

| Method | Purpose | Params |
|--------|---------|--------|
| `add_liquidity(token0, token1, fee, tick_lower, tick_upper, amount0_desired, amount1_desired, slippage_bps=50)` | Mint new position | amounts in raw (wei) |
| `increase_liquidity(token_id, amount0_desired, amount1_desired, slippage_bps=50)` | Add to existing | amounts in raw |
| `remove_liquidity(token_id, liquidity=None, slippage_bps=50, collect=True, burn=False)` | Decrease/close | liquidity=None for full |
| `collect_fees(token_id)` | Collect accrued fees | — |

## Uniswap v4 swaps

Uniswap v4 is supported on Ethereum (1), Base (8453), Arbitrum (42161), and Robinhood (4663). Use the v4 methods directly for v4-only tokens — on newer chains (Robinhood) aggregators (BRAP/LiFi) may have no v4 coverage, so those tokens either don't quote or route through dust pools at a huge markup.

```python
# chain_id: 1 (Ethereum), 8453 (Base), 42161 (Arbitrum), or 4663 (Robinhood)
adapter = await get_adapter(UniswapAdapter, chain_id=8453, wallet_label="main")

# 1. Discover pools, ranked by live liquidity (deepest first).
ok, pools = await adapter.v4_find_pools(token_in, token_out)

# 2. Quote exact-in against the best (deepest) pool.
ok, quote = await adapter.v4_quote(token_in, token_out, amount_in_wei)
#   -> {"amount_out", "pool_id", "fee", "liquidity"}

# 3. Execute (native input rides as msg.value; ERC-20 goes through Permit2).
ok, result = await adapter.v4_swap_exact_in(
    token_in=token_in, token_out=token_out,
    amount_in=amount_in_wei, slippage_bps=50,
)
```

- **`token_in`/`token_out`**: use `0x0000…0000` for native ETH (v4 pools are native, not WETH — GeckoTerminal may still label them "WETH").
- **`amount_in`**: base units (wei), an int — not a decimal string.
- **Pool selection is by liquidity, never fee tier.** v4 lets anyone open a pool at any fee; the 10/20/50% pools are traps with dust in them. `v4_find_pools`/`v4_quote` already pick the deepest, but if you inspect pools yourself, rank by `liquidity`.
- **Quote first, always.** A direct v4 quote is usually far better than the aggregator's dust route for v4-only tokens (INDEX/ETH: v4 direct returned ~24% more than BRAP's fallback).
- Supported chains: `v4.v4_supported(chain_id)` — Ethereum, Base, Arbitrum, Robinhood.
- Pool discovery: mainstream pairs on the big chains are found by enumerating standard fee tiers (fast, scan-free); hooked/custom-tier pools (e.g. Robinhood memes) are found via an event scan on small chains. Either way, `v4_find_pools`/`v4_quote` rank by liquidity — trust the deepest pool.
