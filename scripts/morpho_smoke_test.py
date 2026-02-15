#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio

from eth_utils import to_checksum_address

from wayfinder_paths.adapters.morpho_adapter import MorphoAdapter
from wayfinder_paths.core.clients.MorphoClient import MORPHO_CLIENT
from wayfinder_paths.core.config import load_config
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import BASE_USDC, BASE_WETH
from wayfinder_paths.core.constants.hyperlend_abi import WETH_ABI
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.run_strategy import create_signing_callback, get_strategy_config


def _pick_market(markets: list[dict], *, loan: str, collateral: str) -> dict:
    loan_l = loan.lower()
    coll_l = collateral.lower()
    for m in markets:
        la = (m.get("loanAsset") or {}).get("address")
        ca = (m.get("collateralAsset") or {}).get("address")
        if str(la).lower() == loan_l and str(ca).lower() == coll_l:
            return m
    raise ValueError(f"No market found for loan={loan} collateral={collateral}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Morpho Blue on-chain smoke test")
    parser.add_argument("--wallet-label", default="stablecoin_yield_strategy")
    parser.add_argument("--chain-id", type=int, default=CHAIN_ID_BASE)
    parser.add_argument("--lend-usdc", type=float, default=1.0)
    parser.add_argument("--collateral-usdc", type=float, default=5.0)
    parser.add_argument("--borrow-weth", type=float, default=0.0002)
    parser.add_argument(
        "--wrap-weth-buffer-eth",
        type=float,
        default=0.00001,
        help="Wrap a small ETH buffer into WETH before repay_full (helps cover accrued interest).",
    )
    args = parser.parse_args()

    load_config()
    cfg = get_strategy_config("morpho", wallet_label=args.wallet_label)
    strategy_wallet = cfg.get("strategy_wallet") or {}
    addr = strategy_wallet.get("address")
    if not addr:
        raise ValueError(f"No strategy_wallet configured for label={args.wallet_label}")
    addr = to_checksum_address(str(addr))

    signing_cb = create_signing_callback(addr, cfg)
    adapter = MorphoAdapter(config=cfg, strategy_wallet_signing_callback=signing_cb)

    chain_id = int(args.chain_id)

    usdc_bal = await get_token_balance(BASE_USDC, chain_id, addr)
    eth_bal = await get_token_balance(None, chain_id, addr)
    print(f"wallet={addr} chain_id={chain_id} usdc_raw={usdc_bal} eth_wei={eth_bal}")

    markets = await MORPHO_CLIENT.get_all_markets(chain_id=chain_id, listed=True)
    # Lend/unlend market: USDC loan, any collateral. Pick the first.
    lend_market = next(
        m
        for m in markets
        if str((m.get("loanAsset") or {}).get("address", "")).lower()
        == BASE_USDC.lower()
    )
    lend_key = str(lend_market["uniqueKey"])

    # Borrow market: WETH loan, USDC collateral.
    borrow_market = _pick_market(markets, loan=BASE_WETH, collateral=BASE_USDC)
    borrow_key = str(borrow_market["uniqueKey"])

    lend_qty = int(float(args.lend_usdc) * 10**6)
    collateral_qty = int(float(args.collateral_usdc) * 10**6)
    borrow_qty = int(float(args.borrow_weth) * 10**18)

    print(f"lend_market={lend_key} lend_qty={lend_qty}")
    print(f"borrow_market={borrow_key} collateral_qty={collateral_qty} borrow_qty={borrow_qty}")

    ok, tx = await adapter.lend(chain_id=chain_id, market_unique_key=lend_key, qty=lend_qty)
    if not ok:
        raise RuntimeError(f"lend failed: {tx}")
    print("lend_tx", tx)

    ok, tx = await adapter.unlend(chain_id=chain_id, market_unique_key=lend_key, qty=lend_qty)
    if not ok:
        raise RuntimeError(f"unlend failed: {tx}")
    print("unlend_tx", tx)

    ok, tx = await adapter.supply_collateral(
        chain_id=chain_id, market_unique_key=borrow_key, qty=collateral_qty
    )
    if not ok:
        raise RuntimeError(f"supply_collateral failed: {tx}")
    print("supply_collateral_tx", tx)

    ok, tx = await adapter.borrow(chain_id=chain_id, market_unique_key=borrow_key, qty=borrow_qty)
    if not ok:
        raise RuntimeError(f"borrow failed: {tx}")
    print("borrow_tx", tx)

    buffer_eth = float(args.wrap_weth_buffer_eth or 0.0)
    if buffer_eth > 0:
        buffer_wei = int(buffer_eth * 10**18)
        wrap_tx = await encode_call(
            target=BASE_WETH,
            abi=WETH_ABI,
            fn_name="deposit",
            args=[],
            from_address=addr,
            chain_id=chain_id,
            value=buffer_wei,
        )
        wrap_hash = await send_transaction(wrap_tx, signing_cb)
        print("wrap_weth_tx", wrap_hash)

    ok, tx = await adapter.repay(
        chain_id=chain_id,
        market_unique_key=borrow_key,
        qty=0,
        repay_full=True,
    )
    if not ok:
        raise RuntimeError(f"repay_full failed: {tx}")
    print("repay_tx", tx)

    ok, tx = await adapter.withdraw_collateral(
        chain_id=chain_id, market_unique_key=borrow_key, qty=collateral_qty
    )
    if not ok:
        raise RuntimeError(f"withdraw_collateral failed: {tx}")
    print("withdraw_collateral_tx", tx)


if __name__ == "__main__":
    asyncio.run(main())
