#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import datetime

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

    usdc_balance = await _erc20_balance(BASE_USDC, wallet)
    aero_balance = await _erc20_balance(AERO, wallet)
    print(
        f"wallet={wallet} USDC={_fmt_amount(usdc_balance, usdc_decimals)} "
        f"AERO={_fmt_amount(aero_balance, aero_decimals)}"
    )

    ranked = await adapter.rank_pools_by_usdc_per_ve(
        top_n=max(args.top_n, args.pick + 1),
        limit=args.limit,
        require_all_prices=bool(args.require_all_prices),
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
            ok, votes_raw = await adapter.ve_balance_of_nft(token_id=int(args.token_id))
            if not ok:
                raise SystemExit(votes_raw)
            ok, locked = await adapter.ve_locked(token_id=int(args.token_id))
            if not ok:
                raise SystemExit(locked)
            aero_locked_raw = abs(int(locked["amount"]))
        else:
            aero_locked_raw = int(args.lock_aero * (10**aero_decimals))
            ok, votes_raw = await adapter.estimate_votes_for_lock(
                aero_amount_raw=aero_locked_raw,
                lock_duration=args.lock_weeks * 7 * 24 * 60 * 60,
            )
            if not ok:
                raise SystemExit(votes_raw)

        ok, apr = await adapter.estimate_ve_apr_percent(
            usdc_per_ve=float(usdc_per_ve),
            votes_raw=int(votes_raw),
            aero_locked_raw=int(aero_locked_raw),
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

    token_id = int(args.token_id) if args.token_id is not None else None

    usdc_swap_raw = int(args.usdc_swap * (10**usdc_decimals))
    if args.create_lock and usdc_swap_raw > 0:
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

    if args.create_lock and token_id is None:
        aero_now = await _erc20_balance(AERO, wallet)
        lock_raw = min(int(args.lock_aero * (10**aero_decimals)), aero_now)
        if lock_raw <= 0:
            raise SystemExit("No AERO available to lock")
        ok, res = await adapter.create_lock(
            amount=lock_raw,
            lock_duration=args.lock_weeks * 7 * 24 * 60 * 60,
        )
        if not ok:
            raise SystemExit(res)
        token_id = int(res["token_id"])
        print(
            "createLock tx",
            res["tx"],
            get_etherscan_transaction_link(CHAIN_ID_BASE, res["tx"]),
        )
        print("created veNFT tokenId", token_id)

    if token_id is None:
        raise SystemExit("No token_id available to vote with")

    pick = int(args.pick)
    if pick < 0 or pick >= len(ranked):
        raise SystemExit(f"--pick out of range (0..{len(ranked) - 1})")

    ok, vote_window = await adapter.can_vote_now(token_id=int(token_id))
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
        token_id=int(token_id),
        pools=[epoch.lp],
        weights=[int(args.vote_weight)],
    )
    if not ok:
        raise SystemExit(tx_hash)
    print("vote tx", tx_hash, get_etherscan_transaction_link(CHAIN_ID_BASE, tx_hash))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
