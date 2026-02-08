#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from eth_account import Account
from eth_utils import to_checksum_address

from wayfinder_paths.adapters.aerodrome_adapter.adapter import (
    AerodromeAdapter,
    SugarPool,
)
from wayfinder_paths.core.config import load_config
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import BASE_USDC
from wayfinder_paths.core.utils.etherscan import get_etherscan_transaction_link
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.core.utils.web3 import web3_from_chain_id


def _load_config(path: Path) -> dict:
    return json.loads(path.read_text())


def _wallet_from_label(cfg: dict, wallet_label: str) -> tuple[str, str]:
    wallets = cfg.get("wallets") or []
    for wallet in wallets:
        if wallet.get("label") != wallet_label:
            continue
        addr = wallet.get("address")
        pk = wallet.get("private_key") or wallet.get("private_key_hex")
        if not addr or not pk:
            raise SystemExit(f"Wallet '{wallet_label}' missing address/private_key")
        return to_checksum_address(addr), str(pk)
    raise SystemExit(f"Wallet label '{wallet_label}' not found in config.json")


async def _native_balance(wallet: str) -> int:
    async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
        return int(await web3.eth.get_balance(to_checksum_address(wallet)))


async def _erc20_balance(token: str, wallet: str) -> int:
    return int(
        await get_token_balance(
            token_address=to_checksum_address(token),
            chain_id=CHAIN_ID_BASE,
            wallet_address=to_checksum_address(wallet),
        )
    )


def _fmt(amount_raw: int, decimals: int) -> str:
    return f"{amount_raw / (10**decimals):,.6f}"


async def _safe_symbol(adapter: AerodromeAdapter, token: str) -> str:
    try:
        return await adapter.token_symbol(token)
    except Exception:
        t = to_checksum_address(token)
        return f"{t[:6]}…{t[-4:]}"


async def _print_ranked(
    adapter: AerodromeAdapter, ranked: list[tuple[float, SugarPool]], max_rows: int
) -> None:
    for i, (apr, p) in enumerate(ranked[:max_rows]):
        s0 = await _safe_symbol(adapter, p.token0)
        s1 = await _safe_symbol(adapter, p.token1)
        staked_tvl = await adapter.v2_staked_tvl_usdc(p)
        tvl_str = f"${staked_tvl:,.2f}" if staked_tvl is not None else "n/a"
        print(
            f"[{i:02d}] {apr * 100:8.2f}%  {p.symbol:28}  {s0}/{s1}  staked={tvl_str}"
        )


async def main() -> int:
    p = argparse.ArgumentParser(
        description="Find best Aerodrome v2 gauge emissions APR (onchain) and deploy.",
    )
    p.add_argument("--config", default="config.json")
    p.add_argument("--wallet-label", default="main")
    p.add_argument(
        "--deposit-usdc",
        type=float,
        default=2.0,
        help="USDC budget to deploy (swaps + adds liquidity + stakes LP)",
    )
    p.add_argument("--candidate-count", type=int, default=100)
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument(
        "--min-staked-tvl-usdc",
        type=float,
        default=5_000.0,
        help="Skip tiny pools when picking the best",
    )
    p.add_argument("--pick", type=int, default=0, help="Pick index from ranked list")
    p.add_argument(
        "--slippage-bps",
        type=int,
        default=100,
        help="Swap slippage in bps (default 100 = 1 percent)",
    )
    p.add_argument("--dry-run", action="store_true", help="Only print ranking")
    args = p.parse_args()

    load_config(args.config, require_exists=True)
    cfg = _load_config(Path(args.config))
    wallet_addr, pk = _wallet_from_label(cfg, args.wallet_label)

    account = Account.from_key(pk)

    async def sign_callback(tx: dict) -> bytes:
        signed = account.sign_transaction(tx)
        return signed.raw_transaction

    adapter = AerodromeAdapter(
        config={"strategy_wallet": {"address": wallet_addr}},
        strategy_wallet_signing_callback=sign_callback,
    )

    usdc_dec = await adapter.token_decimals(BASE_USDC)

    eth_bal = await _native_balance(wallet_addr)
    usdc_bal = await _erc20_balance(BASE_USDC, wallet_addr)
    print(
        f"wallet={wallet_addr}  ETH={eth_bal / 1e18:.6f}  USDC={_fmt(usdc_bal, usdc_dec)}"
    )

    deposit_usdc_raw = int(args.deposit_usdc * (10**usdc_dec))
    if deposit_usdc_raw <= 0:
        raise SystemExit("--deposit-usdc must be > 0")
    if usdc_bal < deposit_usdc_raw:
        raise SystemExit(
            f"Insufficient USDC: have {_fmt(usdc_bal, usdc_dec)} need "
            f"{_fmt(deposit_usdc_raw, usdc_dec)}"
        )

    ranked = await adapter.rank_v2_pools_by_emissions_apr(
        top_n=max(int(args.top_n), int(args.pick) + 1),
        candidate_count=int(args.candidate_count),
    )
    if not ranked:
        raise SystemExit("No v2 pools ranked (pricing may have failed)")

    print("\nTop pools (emissions APR):")
    await _print_ranked(
        adapter, ranked, max_rows=max(int(args.top_n), int(args.pick) + 1)
    )

    if args.dry_run:
        return 0

    pick = int(args.pick)
    if pick < 0 or pick >= len(ranked):
        raise SystemExit(f"--pick out of range (0..{len(ranked) - 1})")

    apr, pool = ranked[pick]
    staked_tvl = await adapter.v2_staked_tvl_usdc(pool)
    if staked_tvl is not None and staked_tvl < float(args.min_staked_tvl_usdc):
        print(
            f"\nWARNING: picked pool staked TVL ${staked_tvl:,.2f} < "
            f"${float(args.min_staked_tvl_usdc):,.2f}"
        )

    print("\nDeploying to:")
    print(
        f"  pool={pool.lp} symbol={pool.symbol} stable={pool.stable} "
        f"gauge={pool.gauge} apr={apr * 100:.2f}%"
    )

    async def swap_usdc_to(token_out: str, amount_in_usdc: int) -> int:
        if to_checksum_address(token_out) == BASE_USDC:
            return int(amount_in_usdc)
        pre = await _erc20_balance(token_out, wallet_addr)
        (
            tx_hash,
            routes,
            out_min,
        ) = await adapter.swap_exact_tokens_for_tokens_best_route(
            token_in=BASE_USDC,
            token_out=token_out,
            amount_in=int(amount_in_usdc),
            slippage_bps=int(args.slippage_bps),
        )
        out_dec = await adapter.token_decimals(token_out)
        path = " -> ".join(
            [f"{await _safe_symbol(adapter, r.from_token)}({r.stable})" for r in routes]
            + [await _safe_symbol(adapter, routes[-1].to_token)]
        )
        print(
            "swap tx",
            tx_hash,
            get_etherscan_transaction_link(CHAIN_ID_BASE, tx_hash),
        )
        print(f"swap path: {path}  outMin={_fmt(out_min, out_dec)}")
        post = await _erc20_balance(token_out, wallet_addr)
        return int(post - pre)

    half = deposit_usdc_raw // 2
    amt0 = await swap_usdc_to(pool.token0, half)
    amt1 = await swap_usdc_to(pool.token1, deposit_usdc_raw - half)

    d0 = await adapter.token_decimals(pool.token0)
    d1 = await adapter.token_decimals(pool.token1)
    print(f"amount0={_fmt(amt0, d0)} amount1={_fmt(amt1, d1)}")

    pre_lp = await adapter.lp_balance(pool.lp)
    add_tx = await adapter.add_liquidity(
        token_a=pool.token0,
        token_b=pool.token1,
        stable=pool.stable,
        amount_a_desired=int(amt0),
        amount_b_desired=int(amt1),
        amount_a_min=0,
        amount_b_min=0,
    )
    print(
        "addLiquidity tx", add_tx, get_etherscan_transaction_link(CHAIN_ID_BASE, add_tx)
    )

    post_lp = await adapter.lp_balance(pool.lp)
    minted = int(post_lp - pre_lp)
    print("LP minted", minted)
    if minted <= 0:
        raise SystemExit("No LP minted; aborting before gauge deposit")

    dep_tx = await adapter.deposit_gauge(
        gauge=pool.gauge, lp_token=pool.lp, amount=minted
    )
    print(
        "gauge deposit tx",
        dep_tx,
        get_etherscan_transaction_link(CHAIN_ID_BASE, dep_tx),
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
