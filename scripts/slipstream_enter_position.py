#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path

from eth_account import Account
from eth_utils import to_checksum_address

from wayfinder_paths.adapters.aerodrome_adapter.adapter import AerodromeAdapter
from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter
from wayfinder_paths.core.config import load_config
from wayfinder_paths.core.constants.aerodrome import (
    AERODROME_SLIPSTREAM_FACTORY,
    BASE_AERO,
)
from wayfinder_paths.core.constants.aerodrome_abi import SLIPSTREAM_FACTORY_ABI
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
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

# Base cbBTC / uBTC (Universal BTC) — resolved via TokenClient fuzzy search.
BASE_CBBTC = to_checksum_address("0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf")
BASE_UBTC = to_checksum_address("0xf1143f3a8d76f1ca740d29d5671d365f66c44ed1")


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


async def _erc20_balance(chain_id: int, token: str, wallet: str) -> int:
    return int(
        await get_token_balance(
            token_address=to_checksum_address(token),
            chain_id=int(chain_id),
            wallet_address=to_checksum_address(wallet),
        )
    )


async def _erc20_decimals(chain_id: int, token: str) -> int:
    async with web3_from_chain_id(int(chain_id)) as web3:
        c = web3.eth.contract(address=to_checksum_address(token), abi=ERC20_ABI)
        return int(await c.functions.decimals().call())


def _floor_to_spacing(tick: int, spacing: int) -> int:
    return (int(tick) // int(spacing)) * int(spacing)


def _ceil_to_spacing(tick: int, spacing: int) -> int:
    spacing = int(spacing)
    return int((-(-int(tick) // spacing)) * spacing)


async def _best_slipstream_pool_for_pair(
    adapter: AerodromeAdapter, token_a: str, token_b: str
) -> str:
    token_a = to_checksum_address(token_a)
    token_b = to_checksum_address(token_b)
    tick_spacings = await adapter._slipstream_tick_spacings_for_pair(
        token_a=token_a, token_b=token_b
    )
    if not tick_spacings:
        raise RuntimeError("No Slipstream pool found for pair (no tick spacings)")

    best_pool = None
    best_liq = 0

    async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
        factory = web3.eth.contract(
            address=AERODROME_SLIPSTREAM_FACTORY, abi=SLIPSTREAM_FACTORY_ABI
        )
        for ts in tick_spacings:
            pool = await factory.functions.getPool(token_a, token_b, int(ts)).call()
            pool = to_checksum_address(pool)
            if int(pool, 16) == 0:
                continue
            try:
                st = await adapter.slipstream_pool_state(pool=pool)
            except Exception:
                continue
            if int(st.liquidity) <= 0:
                continue
            if int(st.liquidity) > best_liq:
                best_liq = int(st.liquidity)
                best_pool = pool

    if best_pool is None:
        raise RuntimeError("Slipstream pools exist but none have liquidity > 0")
    return to_checksum_address(best_pool)


async def _maybe_bridge_arb_usdc_to_base(
    *,
    wallet: str,
    pk: str,
    amount_usdc: float,
    timeout_s: int = 900,
) -> None:
    if amount_usdc <= 0:
        return

    usdc_dec = await _erc20_decimals(CHAIN_ID_ARBITRUM, ARBITRUM_USDC)
    amount_raw = int(amount_usdc * (10**usdc_dec))
    if amount_raw <= 0:
        return

    arb_before = await _erc20_balance(CHAIN_ID_ARBITRUM, ARBITRUM_USDC, wallet)
    if arb_before < amount_raw:
        raise SystemExit(
            f"Insufficient Arbitrum USDC to bridge: have {arb_before / 10**usdc_dec:.6f}, "
            f"need {amount_raw / 10**usdc_dec:.6f}"
        )

    base_before = await _erc20_balance(CHAIN_ID_BASE, BASE_USDC, wallet)

    account = Account.from_key(pk)

    async def sign_callback(tx: dict) -> bytes:
        signed = account.sign_transaction(tx)
        return signed.raw_transaction

    brap = BRAPAdapter(strategy_wallet_signing_callback=sign_callback)

    ok, res = await brap.swap_from_token_ids(
        from_token_id=TOKEN_ID_USDC_ARBITRUM,
        to_token_id=TOKEN_ID_USDC_BASE,
        from_address=wallet,
        amount=str(amount_raw),
        preferred_providers=["lifi"],
    )
    if not ok:
        raise SystemExit(f"BRAP bridge failed: {res}")

    tx_hash = (res or {}).get("tx_hash") if isinstance(res, dict) else None
    if tx_hash:
        print(
            "bridge tx",
            tx_hash,
            get_etherscan_transaction_link(CHAIN_ID_ARBITRUM, tx_hash),
        )

    # Wait for Base USDC to increase (best-effort).
    start = asyncio.get_event_loop().time()
    while True:
        now = asyncio.get_event_loop().time()
        if now - start > float(timeout_s):
            raise SystemExit("Timed out waiting for bridged USDC to arrive on Base")
        cur = await _erc20_balance(CHAIN_ID_BASE, BASE_USDC, wallet)
        if cur > base_before:
            print(
                f"bridge arrived: base_usdc_before={base_before / 10**usdc_dec:.6f} "
                f"base_usdc_now={cur / 10**usdc_dec:.6f}"
            )
            return
        await asyncio.sleep(10)


async def main() -> int:
    p = argparse.ArgumentParser(
        description="Enter a simple Aerodrome Slipstream CL position on Base"
    )
    p.add_argument("--config", default="config.json")
    p.add_argument("--wallet-label", default="main")
    p.add_argument("--pair", choices=["eth", "btc"], default="eth")
    p.add_argument("--deposit-usdc", type=float, default=4.0)
    p.add_argument("--range-pct", type=float, default=5.0)
    p.add_argument("--slippage-bps", type=int, default=150)
    p.add_argument("--bridge-arb-usdc", type=float, default=0.0)
    p.add_argument("--bridge-timeout-s", type=int, default=900)
    p.add_argument(
        "--stake", action="store_true", help="Stake the NFT into the Slipstream gauge"
    )
    args = p.parse_args()

    load_config(args.config, require_exists=True)
    cfg = _load_config(Path(args.config))
    wallet_addr, pk = _wallet_from_label(cfg, args.wallet_label)

    # Optional funding (Arbitrum -> Base)
    if float(args.bridge_arb_usdc) > 0:
        await _maybe_bridge_arb_usdc_to_base(
            wallet=wallet_addr,
            pk=pk,
            amount_usdc=float(args.bridge_arb_usdc),
            timeout_s=int(args.bridge_timeout_s),
        )

    account = Account.from_key(pk)

    async def sign_callback(tx: dict) -> bytes:
        signed = account.sign_transaction(tx)
        return signed.raw_transaction

    adapter = AerodromeAdapter(
        config={"strategy_wallet": {"address": wallet_addr}},
        strategy_wallet_signing_callback=sign_callback,
    )

    if args.pair == "eth":
        token_a, token_b = BASE_WETH, BASE_WSTETH
    else:
        token_a, token_b = BASE_CBBTC, BASE_UBTC

    pool = await _best_slipstream_pool_for_pair(adapter, token_a, token_b)
    state = await adapter.slipstream_pool_state(pool=pool)
    s0 = await adapter.token_symbol(state.token0)
    s1 = await adapter.token_symbol(state.token1)
    print(
        f"selected pool={pool}  {s0}/{s1}  tick={state.tick}  tickSpacing={state.tick_spacing}  "
        f"fee={state.fee_pips}  unstakedFee={state.unstaked_fee_pips}  activeL={state.liquidity}"
    )

    usdc_dec = await _erc20_decimals(CHAIN_ID_BASE, BASE_USDC)
    usdc_raw = await _erc20_balance(CHAIN_ID_BASE, BASE_USDC, wallet_addr)
    deposit_usdc = min(float(args.deposit_usdc), usdc_raw / (10**usdc_dec))
    if deposit_usdc <= 0:
        raise SystemExit("No USDC on Base to deploy")
    print(f"base_usdc={usdc_raw / 10**usdc_dec:.6f}  deploying≈{deposit_usdc:.6f} USDC")

    # Swap half the budget into token0 and token1 (USDC -> token0/token1).
    half_raw = int((deposit_usdc / 2.0) * (10**usdc_dec))
    if half_raw <= 0:
        raise SystemExit("deposit-usdc too small after splitting")

    if state.token0 != BASE_USDC:
        (
            tx_hash,
            routes,
            out_min,
        ) = await adapter.swap_exact_tokens_for_tokens_best_route(
            token_in=BASE_USDC,
            token_out=state.token0,
            amount_in=half_raw,
            slippage_bps=int(args.slippage_bps),
            intermediates=[BASE_WETH],
        )
        print(
            "swap0 tx", tx_hash, get_etherscan_transaction_link(CHAIN_ID_BASE, tx_hash)
        )
        print(
            "swap0 routes",
            [(r.from_token, r.to_token, r.stable) for r in routes],
            "outMin",
            out_min,
        )

    if state.token1 != BASE_USDC:
        (
            tx_hash,
            routes,
            out_min,
        ) = await adapter.swap_exact_tokens_for_tokens_best_route(
            token_in=BASE_USDC,
            token_out=state.token1,
            amount_in=half_raw,
            slippage_bps=int(args.slippage_bps),
            intermediates=[BASE_WETH],
        )
        print(
            "swap1 tx", tx_hash, get_etherscan_transaction_link(CHAIN_ID_BASE, tx_hash)
        )
        print(
            "swap1 routes",
            [(r.from_token, r.to_token, r.stable) for r in routes],
            "outMin",
            out_min,
        )

    bal0 = await _erc20_balance(CHAIN_ID_BASE, state.token0, wallet_addr)
    bal1 = await _erc20_balance(CHAIN_ID_BASE, state.token1, wallet_addr)
    d0 = await _erc20_decimals(CHAIN_ID_BASE, state.token0)
    d1 = await _erc20_decimals(CHAIN_ID_BASE, state.token1)
    print(f"balances after swaps: {s0}={bal0 / 10**d0:.8f}  {s1}={bal1 / 10**d1:.8f}")

    # Choose a symmetric tick range around the current tick (approx +/- range_pct in price).
    pct = float(args.range_pct) / 100.0
    if pct <= 0 or pct >= 1.0:
        raise SystemExit("--range-pct must be in (0, 100)")
    tick_lower = int(state.tick + math.floor(math.log(1.0 - pct) / math.log(1.0001)))
    tick_upper = int(state.tick + math.ceil(math.log(1.0 + pct) / math.log(1.0001)))
    tick_lower = _floor_to_spacing(tick_lower, state.tick_spacing)
    tick_upper = _ceil_to_spacing(tick_upper, state.tick_spacing)
    if tick_lower >= tick_upper:
        raise SystemExit("Computed invalid tick bounds")

    # Mint.
    mint_tx, token_id, _receipt = await adapter.slipstream_mint_position(
        pool=pool,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        amount0_desired=bal0,
        amount1_desired=bal1,
        amount0_min=0,
        amount1_min=0,
        recipient=wallet_addr,
        sqrt_price_x96=0,
        wait_for_receipt=True,
    )
    print("mint tx", mint_tx, get_etherscan_transaction_link(CHAIN_ID_BASE, mint_tx))
    print("position tokenId", token_id)

    if args.stake and token_id is not None:
        gauge = await adapter.gauge_for_pool(pool)
        if int(gauge, 16) == 0:
            raise SystemExit("No gauge found for pool")

        # Approve + deposit.
        approve_tx = await adapter.slipstream_approve_position(
            spender=gauge, token_id=int(token_id)
        )
        print(
            "gauge approve tx",
            approve_tx,
            get_etherscan_transaction_link(CHAIN_ID_BASE, approve_tx),
        )

        deposit_tx = await adapter.slipstream_gauge_deposit(
            gauge=gauge, token_id=int(token_id), approve=False
        )
        print(
            "gauge deposit tx",
            deposit_tx,
            get_etherscan_transaction_link(CHAIN_ID_BASE, deposit_tx),
        )

    # Quick FYI: rebase/emissions token is AERO.
    aero_px = await adapter.token_price_usdc(BASE_AERO)
    print(
        f"AERO price(usdc)≈{aero_px:.4f}"
        if math.isfinite(aero_px)
        else "AERO price(usdc)=n/a"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
