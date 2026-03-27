#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio

from eth_utils import to_checksum_address

from wayfinder_paths.adapters.aerodrome_adapter import AerodromeAdapter
from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.config import load_config
from wayfinder_paths.core.constants.aerodrome_contracts import AERODROME_BY_CHAIN
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import BASE_USDC
from wayfinder_paths.core.utils.etherscan import get_etherscan_transaction_link
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.mcp.scripting import get_adapter

AERO = AERODROME_BY_CHAIN[CHAIN_ID_BASE]["aero"]


def _fmt_amount(amount_raw: int, decimals: int) -> str:
    return f"{amount_raw / (10**decimals):,.6f}"


async def _erc20_balance(token: str, wallet: str) -> int:
    return int(
        await get_token_balance(
            token_address=to_checksum_address(token),
            chain_id=CHAIN_ID_BASE,
            wallet_address=to_checksum_address(wallet),
        )
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


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live Aerodrome classic smoke test on Base",
    )
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--wallet-label", default="main")
    parser.add_argument("--usdc-swap", type=float, default=2.0)
    parser.add_argument("--lock-aero", type=float, default=2.0)
    parser.add_argument("--lock-weeks", type=int, default=4)
    parser.add_argument("--usdc-liquidity", type=float, default=1.0)
    parser.add_argument("--pool-stable", action="store_true")
    parser.add_argument("--slippage-bps", type=int, default=100)
    args = parser.parse_args()

    load_config(args.config, require_exists=True)
    adapter = get_adapter(AerodromeAdapter, args.wallet_label, config_path=args.config)
    brap = get_adapter(BRAPAdapter, args.wallet_label, config_path=args.config)
    wallet = adapter.wallet_address
    if not wallet:
        raise SystemExit(f"Wallet '{args.wallet_label}' missing address in config")

    usdc_decimals = await adapter.token_decimals(BASE_USDC)
    aero_decimals = await adapter.token_decimals(AERO)

    usdc_before = await _erc20_balance(BASE_USDC, wallet)
    aero_before = await _erc20_balance(AERO, wallet)
    print(f"wallet={wallet}")
    print(
        f"USDC(before)={_fmt_amount(usdc_before, usdc_decimals)} "
        f"AERO(before)={_fmt_amount(aero_before, aero_decimals)}"
    )

    usdc_swap_raw = int(args.usdc_swap * (10**usdc_decimals))
    if usdc_swap_raw > 0:
        res = await _swap_via_brap(
            brap=brap,
            from_token=BASE_USDC,
            to_token=AERO,
            from_address=wallet,
            amount_raw=usdc_swap_raw,
            slippage_bps=args.slippage_bps,
        )
        print(
            "swap tx",
            res["tx"],
            get_etherscan_transaction_link(CHAIN_ID_BASE, res["tx"]),
        )

    aero_after_swap = await _erc20_balance(AERO, wallet)
    print(f"AERO(after swap)={_fmt_amount(aero_after_swap, aero_decimals)}")

    lock_duration = args.lock_weeks * 7 * 24 * 60 * 60
    requested_lock_raw = int(args.lock_aero * (10**aero_decimals))
    min_left_for_lp_raw = int(0.1 * (10**aero_decimals)) if args.usdc_liquidity > 0 else 0
    lock_raw = min(requested_lock_raw, max(aero_after_swap - min_left_for_lp_raw, 0))
    if lock_raw <= 0:
        raise SystemExit("Not enough AERO available after swap to create a lock")

    ok, res = await adapter.create_lock(amount=lock_raw, lock_duration=lock_duration)
    if not ok:
        raise SystemExit(res)
    token_id = res["token_id"]
    print(
        "lock tx",
        res["tx"],
        get_etherscan_transaction_link(CHAIN_ID_BASE, res["tx"]),
    )
    print("veNFT tokenId", token_id)

    ok, pool = await adapter.get_pool(
        tokenA=BASE_USDC,
        tokenB=AERO,
        stable=bool(args.pool_stable),
    )
    if not ok:
        raise SystemExit(pool)

    ok, gauge = await adapter.get_gauge(pool=pool)
    if not ok:
        raise SystemExit(gauge)

    usdc_liq_raw = int(args.usdc_liquidity * (10**usdc_decimals))
    usdc_now = await _erc20_balance(BASE_USDC, wallet)
    aero_now = await _erc20_balance(AERO, wallet)
    if usdc_liq_raw <= 0 or usdc_now < usdc_liq_raw:
        raise SystemExit("Insufficient USDC for LP step")
    if aero_now <= 0:
        raise SystemExit("No AERO available for LP step")

    ok, tx_hash = await adapter.add_liquidity(
        tokenA=BASE_USDC,
        tokenB=AERO,
        stable=bool(args.pool_stable),
        amountA_desired=usdc_liq_raw,
        amountB_desired=aero_now,
    )
    if not ok:
        raise SystemExit(tx_hash)
    print(
        "addLiquidity tx",
        tx_hash,
        get_etherscan_transaction_link(CHAIN_ID_BASE, tx_hash),
    )

    await asyncio.sleep(1.0)
    lp_balance = await _erc20_balance(pool, wallet)
    print("LP balance", lp_balance)

    if lp_balance > 0:
        ok, tx_hash = await adapter.stake_lp(gauge=gauge, amount=lp_balance)
        if not ok:
            raise SystemExit(tx_hash)
        print(
            "gauge deposit tx",
            tx_hash,
            get_etherscan_transaction_link(CHAIN_ID_BASE, tx_hash),
        )

    ok, tx_hash = await adapter.vote(
        token_id=int(token_id),
        pools=[pool],
        weights=[10_000],
    )
    if not ok:
        raise SystemExit(tx_hash)
    print(
        "vote tx",
        tx_hash,
        get_etherscan_transaction_link(CHAIN_ID_BASE, tx_hash),
    )

    usdc_final = await _erc20_balance(BASE_USDC, wallet)
    aero_final = await _erc20_balance(AERO, wallet)
    print(
        f"USDC(final)={_fmt_amount(usdc_final, usdc_decimals)} "
        f"AERO(final)={_fmt_amount(aero_final, aero_decimals)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
