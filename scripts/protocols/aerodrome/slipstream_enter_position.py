#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import math

from eth_utils import to_checksum_address

from wayfinder_paths.adapters.aerodrome_slipstream_adapter import (
    AerodromeSlipstreamAdapter,
)
from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.config import load_config
from wayfinder_paths.core.constants.aerodrome_contracts import AERODROME_BY_CHAIN
from wayfinder_paths.core.constants.chains import CHAIN_ID_ARBITRUM, CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import (
    ARBITRUM_USDC,
    BASE_USDC,
    BASE_WETH,
    BASE_WSTETH,
)
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.constants.tokens import (
    TOKEN_ID_USDC_ARBITRUM,
    TOKEN_ID_USDC_BASE,
)
from wayfinder_paths.core.utils.etherscan import get_etherscan_transaction_link
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.core.utils.uniswap_v3_math import ceil_tick_to_spacing, round_tick_to_spacing
from wayfinder_paths.core.utils.web3 import web3_from_chain_id
from wayfinder_paths.mcp.scripting import get_adapter

BASE_AERO = AERODROME_BY_CHAIN[CHAIN_ID_BASE]["aero"]
BASE_CBBTC = to_checksum_address("0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf")
BASE_UBTC = to_checksum_address("0xf1143f3a8d76f1ca740d29d5671d365f66c44ed1")


async def _erc20_balance(chain_id: int, token: str, wallet: str) -> int:
    return int(
        await get_token_balance(
            token_address=to_checksum_address(token),
            chain_id=chain_id,
            wallet_address=to_checksum_address(wallet),
        )
    )


async def _erc20_decimals(chain_id: int, token: str) -> int:
    async with web3_from_chain_id(chain_id) as web3:
        contract = web3.eth.contract(address=to_checksum_address(token), abi=ERC20_ABI)
        return int(await contract.functions.decimals().call())


def _select_pair_tokens(pair: str) -> tuple[str, str]:
    if pair == "eth":
        return BASE_WETH, BASE_WSTETH
    if pair == "btc":
        return BASE_CBBTC, BASE_UBTC
    raise ValueError(f"Unsupported pair: {pair}")


def _ticks_for_percent_range(current_tick: int, tick_spacing: int, range_pct: float) -> tuple[int, int]:
    pct = range_pct / 100.0
    if pct <= 0 or pct >= 1.0:
        raise ValueError("range_pct must be in (0, 100)")
    tick_lower = int(current_tick + math.floor(math.log(1.0 - pct) / math.log(1.0001)))
    tick_upper = int(current_tick + math.ceil(math.log(1.0 + pct) / math.log(1.0001)))
    return (
        round_tick_to_spacing(tick_lower, tick_spacing),
        ceil_tick_to_spacing(tick_upper, tick_spacing),
    )


async def _swap_via_brap(
    *,
    brap: BRAPAdapter,
    from_token: str,
    to_token: str,
    from_address: str,
    amount_raw: int,
    slippage_bps: int,
) -> dict:
    from_meta, to_meta = await asyncio.gather(
        TOKEN_CLIENT.get_token_details(from_token, chain_id=CHAIN_ID_BASE),
        TOKEN_CLIENT.get_token_details(to_token, chain_id=CHAIN_ID_BASE),
    )
    if not from_meta or not to_meta:
        raise SystemExit("Unable to resolve token metadata for BRAP swap")

    ok, quote = await brap.best_quote(
        from_token_address=from_token,
        to_token_address=to_token,
        from_chain_id=CHAIN_ID_BASE,
        to_chain_id=CHAIN_ID_BASE,
        from_address=from_address,
        amount=str(amount_raw),
        slippage=slippage_bps / 10_000,
    )
    if not ok:
        raise SystemExit(quote)

    ok, result = await brap.swap_from_quote(
        from_token=from_meta,
        to_token=to_meta,
        from_address=from_address,
        quote=quote,
    )
    if not ok:
        raise SystemExit(result)
    return result


async def _maybe_bridge_arb_usdc_to_base(
    *,
    brap: BRAPAdapter,
    wallet: str,
    amount_usdc: float,
    timeout_s: int,
) -> None:
    if amount_usdc <= 0:
        return

    usdc_decimals = await _erc20_decimals(CHAIN_ID_ARBITRUM, ARBITRUM_USDC)
    amount_raw = int(amount_usdc * (10**usdc_decimals))
    if amount_raw <= 0:
        return

    arb_before = await _erc20_balance(CHAIN_ID_ARBITRUM, ARBITRUM_USDC, wallet)
    if arb_before < amount_raw:
        raise SystemExit("Insufficient Arbitrum USDC to bridge")

    base_before = await _erc20_balance(CHAIN_ID_BASE, BASE_USDC, wallet)
    ok, res = await brap.swap_from_token_ids(
        from_token_id=TOKEN_ID_USDC_ARBITRUM,
        to_token_id=TOKEN_ID_USDC_BASE,
        from_address=wallet,
        amount=str(amount_raw),
        preferred_providers=["lifi"],
    )
    if not ok:
        raise SystemExit(res)

    tx_hash = res.get("tx_hash") if isinstance(res, dict) else None
    if tx_hash:
        print(
            "bridge tx",
            tx_hash,
            get_etherscan_transaction_link(CHAIN_ID_ARBITRUM, tx_hash),
        )

    start = asyncio.get_event_loop().time()
    while True:
        if asyncio.get_event_loop().time() - start > float(timeout_s):
            raise SystemExit("Timed out waiting for bridged USDC to arrive on Base")
        current = await _erc20_balance(CHAIN_ID_BASE, BASE_USDC, wallet)
        if current > base_before:
            return
        await asyncio.sleep(10)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enter a simple Aerodrome Slipstream position on Base",
    )
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--wallet-label", default="main")
    parser.add_argument("--pair", choices=["eth", "btc"], default="eth")
    parser.add_argument("--deposit-usdc", type=float, default=4.0)
    parser.add_argument("--range-pct", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=int, default=150)
    parser.add_argument("--bridge-arb-usdc", type=float, default=0.0)
    parser.add_argument("--bridge-timeout-s", type=int, default=900)
    parser.add_argument("--stake", action="store_true")
    args = parser.parse_args()

    load_config(args.config, require_exists=True)
    slipstream = get_adapter(
        AerodromeSlipstreamAdapter,
        args.wallet_label,
        config_path=args.config,
    )
    brap = get_adapter(BRAPAdapter, args.wallet_label, config_path=args.config)

    wallet = slipstream.wallet_address
    if not wallet:
        raise SystemExit(f"Wallet '{args.wallet_label}' missing address in config")

    if float(args.bridge_arb_usdc) > 0:
        await _maybe_bridge_arb_usdc_to_base(
            brap=brap,
            wallet=wallet,
            amount_usdc=float(args.bridge_arb_usdc),
            timeout_s=int(args.bridge_timeout_s),
        )

    token_a, token_b = _select_pair_tokens(args.pair)
    ok, best_market = await slipstream.slipstream_best_pool_for_pair(
        tokenA=token_a,
        tokenB=token_b,
    )
    if not ok:
        raise SystemExit(best_market)
    pool = best_market["pool"]

    ok, state = await slipstream.slipstream_pool_state(pool=pool)
    if not ok:
        raise SystemExit(state)
    symbol0, symbol1 = await asyncio.gather(
        slipstream.token_symbol(state["token0"]),
        slipstream.token_symbol(state["token1"]),
    )
    print(
        f"selected pool={pool} {symbol0}/{symbol1} tick={state['tick']} "
        f"tickSpacing={state['tick_spacing']} fee={state['fee_pips']} "
        f"unstakedFee={state['unstaked_fee_pips']} activeL={state['liquidity']}"
    )

    usdc_decimals = await slipstream.token_decimals(BASE_USDC)
    usdc_raw = await _erc20_balance(CHAIN_ID_BASE, BASE_USDC, wallet)
    deposit_usdc = min(float(args.deposit_usdc), usdc_raw / (10**usdc_decimals))
    if deposit_usdc <= 0:
        raise SystemExit("No USDC on Base to deploy")

    half_raw = int((deposit_usdc / 2.0) * (10**usdc_decimals))
    if half_raw <= 0:
        raise SystemExit("deposit-usdc too small after splitting")

    if state["token0"].lower() != BASE_USDC.lower():
        res = await _swap_via_brap(
            brap=brap,
            from_token=BASE_USDC,
            to_token=state["token0"],
            from_address=wallet,
            amount_raw=half_raw,
            slippage_bps=int(args.slippage_bps),
        )
        print(
            "swap0 tx",
            res["tx"],
            get_etherscan_transaction_link(CHAIN_ID_BASE, res["tx"]),
        )

    if state["token1"].lower() != BASE_USDC.lower():
        res = await _swap_via_brap(
            brap=brap,
            from_token=BASE_USDC,
            to_token=state["token1"],
            from_address=wallet,
            amount_raw=half_raw,
            slippage_bps=int(args.slippage_bps),
        )
        print(
            "swap1 tx",
            res["tx"],
            get_etherscan_transaction_link(CHAIN_ID_BASE, res["tx"]),
        )

    balance0 = await _erc20_balance(CHAIN_ID_BASE, state["token0"], wallet)
    balance1 = await _erc20_balance(CHAIN_ID_BASE, state["token1"], wallet)
    decimals0, decimals1 = await asyncio.gather(
        slipstream.token_decimals(state["token0"]),
        slipstream.token_decimals(state["token1"]),
    )
    print(
        f"balances after swaps: {symbol0}={balance0 / 10**decimals0:.8f} "
        f"{symbol1}={balance1 / 10**decimals1:.8f}"
    )

    tick_lower, tick_upper = _ticks_for_percent_range(
        int(state["tick"]),
        int(state["tick_spacing"]),
        float(args.range_pct),
    )
    if tick_lower >= tick_upper:
        raise SystemExit("Computed invalid tick bounds")

    ok, minted = await slipstream.mint_position(
        token0=state["token0"],
        token1=state["token1"],
        tick_spacing=int(state["tick_spacing"]),
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        amount0_desired=balance0,
        amount1_desired=balance1,
        deployment_variant=str(state["deployment_variant"]),
    )
    if not ok:
        raise SystemExit(minted)
    token_id = minted["token_id"]
    print(
        "mint tx",
        minted["tx"],
        get_etherscan_transaction_link(CHAIN_ID_BASE, minted["tx"]),
    )
    print("position tokenId", token_id)

    if args.stake and token_id is not None:
        ok, gauge = await slipstream.get_gauge(pool=pool)
        if not ok:
            raise SystemExit(gauge)
        ok, tx_hash = await slipstream.stake_position(gauge=gauge, token_id=int(token_id))
        if not ok:
            raise SystemExit(tx_hash)
        print(
            "gauge deposit tx",
            tx_hash,
            get_etherscan_transaction_link(CHAIN_ID_BASE, tx_hash),
        )

    aero_price = await slipstream.token_price_usdc(BASE_AERO)
    print(
        f"AERO price(usdc)≈{aero_price:.4f}"
        if aero_price is not None and math.isfinite(aero_price)
        else "AERO price(usdc)=n/a"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
