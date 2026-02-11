# Uniswap V3 gotchas

## `price_to_tick_decimal` does NOT match on-chain ticks

`price_to_tick_decimal(price, dec0, dec1)` and `tick_to_price_decimal()` are self-consistent
but produce ticks in a **different convention** from the pool's `slot0().tick`.

Example: ETH/USDC on Base (token0=WETH 18dec, token1=USDC 6dec)
- Pool `slot0().tick` ≈ **-200,200**
- `price_to_tick_decimal(2022, 18, 6)` returns ≈ **+352,600** (wrong for on-chain use)

**Use `ticks_for_range` which works directly with on-chain ticks:**

```python
from wayfinder_paths.core.utils.uniswap_v3_math import ticks_for_range

tick_lower, tick_upper = ticks_for_range(current_tick, bps=500, spacing=10)
```

To convert on-chain ticks to human prices for display:

```python
raw = tick_to_price(tick)  # raw pool price (token1_raw / token0_raw)
human_price = raw * 10 ** (dec0 - dec1)
```

## Slippage: use 300 bps for small positions

The default `slippage_bps=50` (0.5%) is too tight for small positions ($1–$100).
A $2 mint reverted with "Price slippage check" at 50 bps.
Use **300 bps (3%)** for small positions:

```python
await adapter.add_liquidity(..., slippage_bps=300)
```

## Token ordering is automatic

The adapter auto-reorders tokens so `token0 < token1` (by address).
It also swaps `amount0_desired`/`amount1_desired` and negates ticks accordingly.
You can pass tokens in any order — just keep amounts matched to the token you pass them with.

## Token ordering on Base (for manual math)

When doing tick/price math manually, know the on-chain order:

| Pair | token0 | token1 | Price meaning |
|------|--------|--------|---------------|
| ETH/USDC | WETH `0x4200…0006` | USDC `0x8335…2913` | USDC_raw per WETH_raw |

`sqrt_price_x96_to_price(sqrtP, 18, 6)` returns the human ETH price in USD.

## Pool ABI exists — don't inline it

`UNISWAP_V3_POOL_ABI` (with `slot0`) is in `core/constants/uniswap_v3_abi.py`:

```python
from wayfinder_paths.core.constants.uniswap_v3_abi import UNISWAP_V3_POOL_ABI
```

## All amounts are raw ints (wei)

| Token | Decimals | 1 unit | Example |
|-------|----------|--------|---------|
| WETH | 18 | `10**18` | 0.001 WETH = `10**15` |
| USDC | 6 | `10**6` | 1 USDC = `1_000_000` |

## Adapter returns `(bool, data)` tuples

All methods return `(ok, result)`:

```python
_, pool = await adapter.get_pool(BASE_WETH, BASE_USDC, 500)
_, positions = await adapter.get_positions()
_, tx_hash = await adapter.add_liquidity(...)
```

## Tick spacing must match fee tier

| Fee | Spacing | Ticks must be divisible by |
|-----|---------|---------------------------|
| 100 | 1 | 1 |
| 500 | 10 | 10 |
| 3000 | 60 | 60 |
| 10000 | 200 | 200 |

The adapter auto-rounds via `round_tick_to_spacing`, but be aware when computing ranges manually.

## `remove_liquidity` with `burn=True` requires zero liquidity

Only pass `burn=True` when removing ALL liquidity. The NFT can only be burned when liquidity is zero and all fees are collected.

## Script execution

Run scripts via MCP with wallet tracking:

```
mcp__wayfinder__run_script(
    script_path=".wayfinder_runs/uniswap_lp.py",
    wallet_label="main"
)
```
