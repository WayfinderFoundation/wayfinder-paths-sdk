#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import math

from eth_utils import to_checksum_address

from wayfinder_paths.adapters.aerodrome_adapter.adapter import AerodromeAdapter
from wayfinder_paths.core.config import load_config
from wayfinder_paths.core.constants.aerodrome import AERODROME_SLIPSTREAM_FACTORY
from wayfinder_paths.core.constants.aerodrome_abi import SLIPSTREAM_FACTORY_ABI
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.utils.web3 import web3_from_chain_id


def _fmt(amount_raw: int, decimals: int) -> str:
    return f"{amount_raw / (10**decimals):,.6f}"


def _floor_to_spacing(tick: int, spacing: int) -> int:
    return (int(tick) // int(spacing)) * int(spacing)


def _ceil_to_spacing(tick: int, spacing: int) -> int:
    spacing = int(spacing)
    return int((-(-int(tick) // spacing)) * spacing)


async def main() -> int:
    p = argparse.ArgumentParser(
        description="Analyze an Aerodrome Slipstream CL pool range (onchain-only).",
    )
    p.add_argument("--config", default="config.json")
    p.add_argument("--pool", default=None, help="Slipstream CL pool address")
    p.add_argument("--token0", default=None, help="If --pool omitted, token0 address")
    p.add_argument("--token1", default=None, help="If --pool omitted, token1 address")
    p.add_argument(
        "--tick-spacing",
        type=int,
        default=None,
        help="If --pool omitted, tickSpacing (e.g. 1/10/100/200)",
    )
    p.add_argument("--tick-lower", type=int, default=None)
    p.add_argument("--tick-upper", type=int, default=None)
    p.add_argument(
        "--range-pct",
        type=float,
        default=5.0,
        help="If ticks not provided, use +/- this percent around current price",
    )
    p.add_argument(
        "--deposit-usdc",
        type=float,
        default=100.0,
        help="Deposit budget (used to approximate amount0/amount1 as 50/50 USD)",
    )
    p.add_argument("--amount0", type=float, default=None, help="Override token0 amount")
    p.add_argument("--amount1", type=float, default=None, help="Override token1 amount")
    p.add_argument("--lookback-blocks", type=int, default=2000)
    p.add_argument("--max-logs", type=int, default=2000)
    p.add_argument("--sigma-lookback-blocks", type=int, default=20_000)
    args = p.parse_args()

    load_config(args.config, require_exists=True)
    adapter = AerodromeAdapter(config={})

    if args.pool:
        pool = to_checksum_address(args.pool)
    else:
        if not (args.token0 and args.token1 and args.tick_spacing is not None):
            raise SystemExit("Provide --pool or (--token0 --token1 --tick-spacing)")
        token0 = to_checksum_address(args.token0)
        token1 = to_checksum_address(args.token1)
        ts = int(args.tick_spacing)
        async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
            factory = web3.eth.contract(
                address=AERODROME_SLIPSTREAM_FACTORY, abi=SLIPSTREAM_FACTORY_ABI
            )
            pool = await factory.functions.getPool(token0, token1, ts).call()
        pool = to_checksum_address(pool)
        print(f"resolved pool={pool}")

    state = await adapter.slipstream_pool_state(pool=pool)
    s0 = await adapter.token_symbol(state.token0)
    s1 = await adapter.token_symbol(state.token1)
    d0 = await adapter.token_decimals(state.token0)
    d1 = await adapter.token_decimals(state.token1)

    price = adapter._q96_to_price_token1_per_token0(
        sqrt_price_x96=state.sqrt_price_x96,
        decimals0=d0,
        decimals1=d1,
    )
    print(
        f"pool={pool}  {s0}/{s1}  tick={state.tick}  tickSpacing={state.tick_spacing}  "
        f"price={price:.8f} {s1}/{s0}"
    )
    print(
        f"fee={state.fee_pips}  unstakedFee={state.unstaked_fee_pips}  "
        f"activeL={state.liquidity}"
    )

    if args.tick_lower is not None and args.tick_upper is not None:
        tick_lower = int(args.tick_lower)
        tick_upper = int(args.tick_upper)
    else:
        pct = float(args.range_pct) / 100.0
        if pct <= 0:
            raise SystemExit("--range-pct must be > 0")
        if pct >= 1.0:
            raise SystemExit("--range-pct must be < 100")
        # Approx tick delta for multiplicative price move.
        tick_lower = int(
            state.tick + math.floor(math.log(1.0 - pct) / math.log(1.0001))
        )
        tick_upper = int(state.tick + math.ceil(math.log(1.0 + pct) / math.log(1.0001)))

    tick_lower = _floor_to_spacing(tick_lower, state.tick_spacing)
    tick_upper = _ceil_to_spacing(tick_upper, state.tick_spacing)
    if tick_lower >= tick_upper:
        raise SystemExit("Computed invalid tick bounds")

    px0 = await adapter.token_price_usdc(state.token0)
    px1 = await adapter.token_price_usdc(state.token1)
    if not (math.isfinite(px0) and math.isfinite(px1)):
        raise SystemExit("Unable to price token0/token1 to USDC (onchain quote failed)")

    if args.amount0 is not None and args.amount1 is not None:
        amount0_raw = int(float(args.amount0) * (10**d0))
        amount1_raw = int(float(args.amount1) * (10**d1))
    else:
        budget = float(args.deposit_usdc)
        if budget <= 0:
            raise SystemExit("--deposit-usdc must be > 0")
        amt0 = (budget / 2.0) / float(px0)
        amt1 = (budget / 2.0) / float(px1)
        amount0_raw = int(amt0 * (10**d0))
        amount1_raw = int(amt1 * (10**d1))

    print(
        f"bounds: [{tick_lower}, {tick_upper})  deposit: "
        f"{_fmt(amount0_raw, d0)} {s0} + {_fmt(amount1_raw, d1)} {s1}"
    )

    metrics = await adapter.slipstream_range_metrics(
        pool=pool,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        amount0_raw=amount0_raw,
        amount1_raw=amount1_raw,
    )

    pos_value = (metrics.amount0_now / (10**d0)) * px0 + (
        metrics.amount1_now / (10**d1)
    ) * px1
    print(
        f"inRange={metrics.in_range}  L_pos={metrics.liquidity_position}  "
        f"share={metrics.share_of_active_liquidity:.8f}  value≈${pos_value:,.2f}"
    )
    print(
        f"composition(now): {_fmt(metrics.amount0_now, d0)} {s0} + "
        f"{_fmt(metrics.amount1_now, d1)} {s1}"
    )

    vol_per_day = await adapter.slipstream_volume_usdc_per_day(
        pool=pool,
        lookback_blocks=int(args.lookback_blocks),
        max_logs=int(args.max_logs),
    )
    print(f"volume_usdc_per_day≈${(vol_per_day or 0.0):,.2f}")

    sigma = await adapter.slipstream_sigma_annual_from_swaps(
        pool=pool,
        lookback_blocks=int(args.sigma_lookback_blocks),
        max_logs=int(args.max_logs),
    )
    p_in_range = None
    if sigma is not None:
        p_in_range = await adapter.slipstream_prob_in_range_week(
            pool=pool,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            sigma_annual=float(sigma),
        )
        print(
            f"sigma_annual≈{sigma:.4f}  p_in_range_1w≈{p_in_range if p_in_range is not None else float('nan'):.4f}"
        )

    fee_apr = await adapter.slipstream_fee_apr_percent(
        metrics=metrics,
        volume_usdc_per_day=float(vol_per_day or 0.0),
        expected_in_range_fraction=float(p_in_range if p_in_range is not None else 1.0),
    )
    if fee_apr is None:
        print("fee_apr: n/a")
    else:
        print(f"fee_apr≈{fee_apr:,.2f}% (unstaked, range-adjusted)")

    # Quick sanity: ensure RPC works for eth_getLogs (some providers restrict).
    async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
        _ = await web3.eth.block_number

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
