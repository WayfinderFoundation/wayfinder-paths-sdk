#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import datetime

from eth_utils import to_checksum_address

from scripts.protocols.aerodrome._common import (
    erc20_balance,
    fmt_amount,
    swap_via_brap,
)
from wayfinder_paths.adapters.aerodrome_adapter import AerodromeAdapter
from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter
from wayfinder_paths.core.config import load_config
from wayfinder_paths.core.constants.aerodrome_contracts import AERODROME_BY_CHAIN
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import BASE_USDC
from wayfinder_paths.core.utils.etherscan import get_etherscan_transaction_link
from wayfinder_paths.mcp.scripting import get_adapter

AERO = AERODROME_BY_CHAIN[CHAIN_ID_BASE]["aero"]


async def _safe_symbol(adapter: AerodromeAdapter, token: str | None) -> str:
    if not token:
        return "?"
    try:
        return await adapter.token_symbol(token)
    except Exception:
        checksum = to_checksum_address(token)
        return f"{checksum[:6]}...{checksum[-4:]}"


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rank Aerodrome classic vote pools by fees+bribes per veAERO",
    )
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--wallet-label", default="main")
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--require-all-prices", action="store_true")
    parser.add_argument("--token-id", type=int)
    parser.add_argument("--lock-aero", type=float, default=2.0)
    parser.add_argument("--lock-weeks", type=int, default=4)
    parser.add_argument("--usdc-swap", type=float, default=0.0)
    parser.add_argument("--pick", type=int, default=0)
    parser.add_argument("--vote-weight", type=int, default=10_000)
    parser.add_argument("--vote", action="store_true")
    parser.add_argument("--create-lock", action="store_true")
    parser.add_argument("--slippage-bps", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_config(args.config, require_exists=True)
    adapter = get_adapter(AerodromeAdapter, args.wallet_label, config_path=args.config)
    brap = get_adapter(BRAPAdapter, args.wallet_label, config_path=args.config)
    wallet = adapter.wallet_address
    if not wallet:
        raise SystemExit(f"Wallet '{args.wallet_label}' missing address in config")

    usdc_decimals = await adapter.token_decimals(BASE_USDC)
    aero_decimals = await adapter.token_decimals(AERO)

    usdc_balance = await erc20_balance(CHAIN_ID_BASE, BASE_USDC, wallet)
    aero_balance = await erc20_balance(CHAIN_ID_BASE, AERO, wallet)
    print(
        f"wallet={wallet} USDC={fmt_amount(usdc_balance, usdc_decimals)} "
        f"AERO={fmt_amount(aero_balance, aero_decimals)}"
    )

    ranked = await adapter.rank_pools_by_usdc_per_ve(
        top_n=max(args.top_n, args.pick + 1),
        limit=args.limit,
        require_all_prices=args.require_all_prices,
    )
    if not ranked:
        raise SystemExit("No pools ranked")

    pools_by_lp = await adapter.pools_by_lp()
    print("\nTop pools (fees+bribes per veAERO vote):")
    for i, (usdc_per_ve, epoch, total_usdc) in enumerate(ranked[: max(1, args.top_n)]):
        pool = pools_by_lp.get(epoch.lp)
        symbol = pool.symbol if pool else f"{epoch.lp[:6]}...{epoch.lp[-4:]}"
        symbol0 = await _safe_symbol(adapter, pool.token0 if pool else None)
        symbol1 = await _safe_symbol(adapter, pool.token1 if pool else None)

        if args.token_id is not None:
            ok, votes_raw = await adapter.ve_balance_of_nft(token_id=args.token_id)
            if not ok:
                raise SystemExit(votes_raw)
            ok, locked = await adapter.ve_locked(token_id=args.token_id)
            if not ok:
                raise SystemExit(locked)
            aero_locked_raw = abs(locked["amount"])
        else:
            aero_locked_raw = int(args.lock_aero * (10**aero_decimals))
            ok, votes_raw = await adapter.estimate_votes_for_lock(
                aero_amount_raw=aero_locked_raw,
                lock_duration=args.lock_weeks * 7 * 24 * 60 * 60,
            )
            if not ok:
                raise SystemExit(votes_raw)

        ok, apr = await adapter.estimate_ve_apr_percent(
            usdc_per_ve=usdc_per_ve,
            votes_raw=votes_raw,
            aero_locked_raw=aero_locked_raw,
        )
        if not ok:
            raise SystemExit(apr)
        apr_str = f"{apr:,.2f}%" if apr is not None else "n/a"
        print(
            f"[{i:02d}] usdc_per_ve={usdc_per_ve:,.6f} veAPR≈{apr_str:>10} "
            f"incentives=${total_usdc:,.2f} {symbol:28} {symbol0}/{symbol1} lp={epoch.lp}"
        )

    if args.dry_run or not args.vote:
        return 0

    if args.token_id is None and not args.create_lock:
        raise SystemExit("--vote requires --token-id or --create-lock")

    token_id = args.token_id

    usdc_swap_raw = int(args.usdc_swap * (10**usdc_decimals))
    if args.create_lock and usdc_swap_raw > 0:
        res = await swap_via_brap(
            brap=brap,
            from_token=BASE_USDC,
            to_token=AERO,
            chain_id=CHAIN_ID_BASE,
            from_address=wallet,
            amount_raw=usdc_swap_raw,
            slippage_bps=args.slippage_bps,
        )
        print(
            "swap tx",
            res["tx"],
            get_etherscan_transaction_link(CHAIN_ID_BASE, res["tx"]),
        )

    if args.create_lock and token_id is None:
        aero_now = await erc20_balance(CHAIN_ID_BASE, AERO, wallet)
        lock_raw = min(int(args.lock_aero * (10**aero_decimals)), aero_now)
        if lock_raw <= 0:
            raise SystemExit("No AERO available to lock")
        ok, res = await adapter.create_lock(
            amount=lock_raw,
            lock_duration=args.lock_weeks * 7 * 24 * 60 * 60,
        )
        if not ok:
            raise SystemExit(res)
        token_id = res["token_id"]
        print(
            "createLock tx",
            res["tx"],
            get_etherscan_transaction_link(CHAIN_ID_BASE, res["tx"]),
        )
        print("created veNFT tokenId", token_id)

    if token_id is None:
        raise SystemExit("No token_id available to vote with")

    pick = args.pick
    if pick < 0 or pick >= len(ranked):
        raise SystemExit(f"--pick out of range (0..{len(ranked) - 1})")

    ok, vote_window = await adapter.can_vote_now(token_id=token_id)
    if not ok:
        raise SystemExit(vote_window)
    if not vote_window["can_vote"]:
        next_epoch = datetime.datetime.fromtimestamp(
            vote_window["next_epoch_start"],
            datetime.UTC,
        ).isoformat()
        raise SystemExit(
            f"tokenId {token_id} already voted this epoch; next epoch starts {next_epoch}"
        )

    _score, epoch, _total = ranked[pick]
    ok, tx_hash = await adapter.vote(
        token_id=token_id,
        pools=[epoch.lp],
        weights=[args.vote_weight],
    )
    if not ok:
        raise SystemExit(tx_hash)
    print("vote tx", tx_hash, get_etherscan_transaction_link(CHAIN_ID_BASE, tx_hash))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
