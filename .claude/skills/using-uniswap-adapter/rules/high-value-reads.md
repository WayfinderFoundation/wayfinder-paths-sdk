# Uniswap V3 reads (pool state + positions)

## Data accuracy (no guessing)

- Do **not** invent or estimate prices, ticks, or fee amounts.
- Only report values fetched from on-chain contracts via the adapter.
- If you can't fetch data, respond with "unavailable" and show the exact script needed.

## Primary data source

- Adapter: `wayfinder_paths/adapters/uniswap_adapter/adapter.py`
- Supported chains: Ethereum (1), Arbitrum (42161), Base (8453), Polygon (137), BSC (56), Avalanche (43114)
- ABIs: `wayfinder_paths/core/constants/uniswap_v3_abi.py`
  - `NONFUNGIBLE_POSITION_MANAGER_ABI` — NPM (positions, mint, collect, etc.)
  - `UNISWAP_V3_POOL_ABI` — pool contract (`slot0`)
  - `UNISWAP_V3_FACTORY_ABI` — factory (`getPool`)

## Ad-hoc read scripts

### Get current pool price and tick

```python
"""Fetch current ETH/USDC pool state on Base."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.uniswap_adapter import UniswapAdapter
from wayfinder_paths.core.constants.contracts import BASE_WETH, BASE_USDC
from wayfinder_paths.core.utils.uniswap_v3_math import get_pool_slot0

async def main():
    adapter = get_adapter(UniswapAdapter, "main")

    _, pool_address = await adapter.get_pool(BASE_WETH, BASE_USDC, 500)
    print(f"Pool: {pool_address}")

    # token0=WETH (18 dec), token1=USDC (6 dec) on Base
    slot0 = await get_pool_slot0(pool_address, 8453, 18, 6)
    print(f"ETH price: ${slot0['price']:,.2f}")
    print(f"Current tick: {slot0['tick']}")

asyncio.run(main())
```

### List all positions for a wallet

```python
"""List all Uniswap V3 positions."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.uniswap_adapter import UniswapAdapter

async def main():
    adapter = get_adapter(UniswapAdapter, "main")
    _, positions = await adapter.get_positions()
    for p in positions:
        print(f"  ID={p['token_id']} liq={p['liquidity']} "
              f"ticks=[{p['tick_lower']}, {p['tick_upper']}] fee={p['fee']}")

asyncio.run(main())
```

### Check uncollected fees

```python
"""Check uncollected fees on a position."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.uniswap_adapter import UniswapAdapter

TOKEN_ID = 12345

async def main():
    adapter = get_adapter(UniswapAdapter, "main")
    _, fees = await adapter.get_uncollected_fees(TOKEN_ID)
    print(f"Uncollected: amount0={fees['amount0']} amount1={fees['amount1']}")

asyncio.run(main())
```

## Key read methods

| Method | Purpose | Wallet needed? |
|--------|---------|----------------|
| `get_pool(token_a, token_b, fee)` | Find pool address | No |
| `get_position(token_id)` | Single position details | No |
| `get_positions(owner?)` | All positions for owner | Yes (or pass owner) |
| `get_uncollected_fees(token_id)` | Pending fees (amount0, amount1) | Yes (uses owner for call) |

## Tick/price math helpers

All in `wayfinder_paths.core.utils.uniswap_v3_math`:

| Function | Purpose |
|----------|---------|
| `get_pool_slot0(pool_address, chain_id, dec0, dec1)` | Async — returns `{sqrt_price_x96, tick, price}` |
| `sqrt_price_x96_to_price(sqrtP, dec0, dec1)` | On-chain sqrtPriceX96 → human price |
| `tick_to_price(tick)` | Tick → raw pool price (token1_raw / token0_raw) |
| `round_tick_to_spacing(tick, spacing)` | Snap tick to valid spacing |
| `ticks_for_range(current_tick, bps, spacing)` | Symmetric tick range from on-chain tick |
| `band_from_bps(mid_price, bps)` | Price ± bps band |
| `amounts_for_liq_inrange(sqrtP, sqrtA, sqrtB, liq)` | Amounts for a given liquidity |
| `liq_for_amounts(sqrtP, sqrtA, sqrtB, amt0, amt1)` | Liquidity from amounts |
| `sqrt_price_x96_from_tick(tick)` | Tick → sqrtPriceX96 |

## Fee tiers and tick spacing

| Fee (bps) | Fee % | Tick spacing | Typical pairs |
|-----------|-------|-------------|---------------|
| 100 | 0.01% | 1 | Stablecoin pairs |
| 500 | 0.05% | 10 | ETH/USDC, major pairs |
| 3000 | 0.30% | 60 | Most pairs |
| 10000 | 1.00% | 200 | Exotic/volatile |
