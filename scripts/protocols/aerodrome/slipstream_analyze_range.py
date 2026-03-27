#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio

from eth_utils import to_checksum_address

from scripts.protocols.aerodrome._common import fmt_amount, ticks_for_percent_range
from wayfinder_paths.adapters.aerodrome_slipstream_adapter import (
    AerodromeSlipstreamAdapter,
)
from wayfinder_paths.core.config import load_config


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze an Aerodrome Slipstream range on Base",
    )
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--pool", required=True)
    parser.add_argument("--tick-lower", type=int)
    parser.add_argument("--tick-upper", type=int)
    parser.add_argument("--range-pct", type=float, default=5.0)
    parser.add_argument("--deposit-usdc", type=float, default=100.0)
    parser.add_argument("--amount0", type=float)
    parser.add_argument("--amount1", type=float)
    parser.add_argument("--lookback-blocks", type=int, default=2000)
    parser.add_argument("--max-logs", type=int, default=2000)
    parser.add_argument("--sigma-lookback-blocks", type=int, default=20_000)
    args = parser.parse_args()

    load_config(args.config, require_exists=True)
    adapter = AerodromeSlipstreamAdapter(config={})
    pool = to_checksum_address(args.pool)

    ok, state = await adapter.slipstream_pool_state(pool=pool)
    if not ok:
        raise SystemExit(state)

    symbol0, symbol1 = await asyncio.gather(
        adapter.token_symbol(state["token0"]),
        adapter.token_symbol(state["token1"]),
    )
    decimals0, decimals1 = await asyncio.gather(
        adapter.token_decimals(state["token0"]),
        adapter.token_decimals(state["token1"]),
    )

    print(
        f"pool={pool} {symbol0}/{symbol1} tick={state['tick']} "
        f"tickSpacing={state['tick_spacing']} price={state['price_token1_per_token0']:.8f}"
    )
    print(
        f"fee={state['fee_pips']} unstakedFee={state['unstaked_fee_pips']} "
        f"activeL={state['liquidity']}"
    )

    if args.tick_lower is not None and args.tick_upper is not None:
        tick_lower = int(args.tick_lower)
        tick_upper = int(args.tick_upper)
    else:
        tick_lower, tick_upper = ticks_for_percent_range(
            int(state["tick"]),
            int(state["tick_spacing"]),
            float(args.range_pct),
        )
    if tick_lower >= tick_upper:
        raise SystemExit("Computed invalid tick bounds")

    price0, price1 = await asyncio.gather(
        adapter.token_price_usdc(state["token0"]),
        adapter.token_price_usdc(state["token1"]),
    )
    if price0 is None or price1 is None:
        raise SystemExit("Unable to price token0/token1 to USDC")

    if args.amount0 is not None and args.amount1 is not None:
        amount0_raw = int(float(args.amount0) * (10**decimals0))
        amount1_raw = int(float(args.amount1) * (10**decimals1))
    else:
        budget = float(args.deposit_usdc)
        if budget <= 0:
            raise SystemExit("--deposit-usdc must be > 0")
        amount0_raw = int(((budget / 2.0) / float(price0)) * (10**decimals0))
        amount1_raw = int(((budget / 2.0) / float(price1)) * (10**decimals1))

    print(
        f"bounds=[{tick_lower}, {tick_upper}) deposit="
        f"{fmt_amount(amount0_raw, decimals0)} {symbol0} + "
        f"{fmt_amount(amount1_raw, decimals1)} {symbol1}"
    )

    ok, metrics = await adapter.slipstream_range_metrics(
        pool=pool,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        amount0_raw=amount0_raw,
        amount1_raw=amount1_raw,
    )
    if not ok:
        raise SystemExit(metrics)

    position_value = (metrics["amount0_now"] / (10**decimals0)) * float(price0) + (
        metrics["amount1_now"] / (10**decimals1)
    ) * float(price1)
    print(
        f"inRange={metrics['in_range']} L_pos={metrics['liquidity_position']} "
        f"share={metrics['share_of_active_liquidity']:.8f} value≈${position_value:,.2f}"
    )
    print(
        f"composition(now): {fmt_amount(metrics['amount0_now'], decimals0)} {symbol0} + "
        f"{fmt_amount(metrics['amount1_now'], decimals1)} {symbol1}"
    )

    ok, volume = await adapter.slipstream_volume_usdc_per_day(
        pool=pool,
        lookback_blocks=int(args.lookback_blocks),
        max_logs=int(args.max_logs),
    )
    if not ok:
        raise SystemExit(volume)
    print(f"volume_usdc_per_day≈${float(volume['volume_usdc_per_day'] or 0.0):,.2f}")

    ok, sigma = await adapter.slipstream_sigma_annual_from_swaps(
        pool=pool,
        lookback_blocks=int(args.sigma_lookback_blocks),
        max_logs=int(args.max_logs),
    )
    if not ok:
        raise SystemExit(sigma)

    prob = None
    if sigma["sigma_annual"] is not None:
        ok, prob = await adapter.slipstream_prob_in_range_week(
            pool=pool,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            sigma_annual=float(sigma["sigma_annual"]),
        )
        if not ok:
            raise SystemExit(prob)
        print(
            f"sigma_annual≈{sigma['sigma_annual']:.4f} "
            f"p_in_range_1w≈{prob['prob_in_range_week']:.4f}"
        )

    ok, fee_apr = await adapter.slipstream_fee_apr_percent(
        metrics=metrics,
        volume_usdc_per_day=float(volume["volume_usdc_per_day"] or 0.0),
        expected_in_range_fraction=float(
            prob["prob_in_range_week"]
            if prob and prob["prob_in_range_week"] is not None
            else 1.0
        ),
    )
    if not ok:
        raise SystemExit(fee_apr)
    fee_apr_value = fee_apr["fee_apr_percent"]
    print("fee_apr: n/a" if fee_apr_value is None else f"fee_apr≈{fee_apr_value:,.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
