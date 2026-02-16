#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio

from eth_utils import to_checksum_address

from wayfinder_paths.adapters.aave_v3_adapter import AaveV3Adapter
from wayfinder_paths.core.config import load_config
from wayfinder_paths.core.constants.chains import CHAIN_ID_ARBITRUM
from wayfinder_paths.core.constants.contracts import ARBITRUM_USDC
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.run_strategy import create_signing_callback, get_strategy_config


async def main() -> None:
    parser = argparse.ArgumentParser(description="Aave v3 on-chain smoke test")
    parser.add_argument("--wallet-label", default="stablecoin_yield_strategy")
    parser.add_argument("--chain-id", type=int, default=CHAIN_ID_ARBITRUM)
    parser.add_argument(
        "--usdc-address",
        default=None,
        help="Override USDC address (defaults to Arbitrum USDC when chain_id=42161).",
    )
    parser.add_argument("--lend-usdc", type=float, default=1.0)
    parser.add_argument("--borrow-usdc", type=float, default=1.0)
    parser.add_argument(
        "--collateral-eth",
        type=float,
        default=0.001,
        help="Native ETH to wrap+deposit as WETH collateral.",
    )
    parser.add_argument("--claim-rewards", action="store_true")
    args = parser.parse_args()

    load_config()

    cfg = get_strategy_config("aave_v3", wallet_label=args.wallet_label)
    strategy_wallet = cfg.get("strategy_wallet") or {}
    addr = strategy_wallet.get("address")
    if not addr:
        raise ValueError(f"No strategy_wallet configured for label={args.wallet_label}")
    addr = to_checksum_address(str(addr))

    signing_cb = create_signing_callback(addr, cfg)
    adapter = AaveV3Adapter(config=cfg, strategy_wallet_signing_callback=signing_cb)

    chain_id = int(args.chain_id)
    usdc_addr = (
        to_checksum_address(str(args.usdc_address))
        if args.usdc_address
        else (ARBITRUM_USDC if chain_id == CHAIN_ID_ARBITRUM else None)
    )
    if not usdc_addr:
        raise ValueError("USDC address missing; pass --usdc-address")

    weth_addr = await adapter._wrapped_native(chain_id=chain_id)

    eth_bal = await get_token_balance(None, chain_id, addr)
    usdc_bal = await get_token_balance(usdc_addr, chain_id, addr)
    print(f"wallet={addr} chain_id={chain_id} usdc_raw={usdc_bal} eth_wei={eth_bal}")

    lend_qty = int(float(args.lend_usdc) * 10**6)
    borrow_qty = int(float(args.borrow_usdc) * 10**6)
    collateral_wei = int(float(args.collateral_eth) * 10**18)

    if usdc_bal < lend_qty:
        raise RuntimeError(
            f"insufficient usdc balance: have={usdc_bal} need={lend_qty}"
        )
    if eth_bal < collateral_wei:
        raise RuntimeError(
            f"insufficient native balance: have={eth_bal} need={collateral_wei}"
        )

    ok, tx = await adapter.lend(
        chain_id=chain_id, underlying_token=usdc_addr, qty=lend_qty
    )
    if not ok:
        raise RuntimeError(f"lend(usdc) failed: {tx}")
    print("lend_usdc_tx", tx)

    ok, tx = await adapter.unlend(
        chain_id=chain_id,
        underlying_token=usdc_addr,
        qty=0,
        withdraw_full=True,
    )
    if not ok:
        raise RuntimeError(f"unlend(usdc) failed: {tx}")
    print("unlend_usdc_tx", tx)

    ok, tx = await adapter.lend(
        chain_id=chain_id,
        underlying_token=weth_addr,
        qty=collateral_wei,
        native=True,
    )
    if not ok:
        raise RuntimeError(f"lend(native->weth) failed: {tx}")
    print("lend_weth_tx", tx)

    ok, tx = await adapter.set_collateral(
        chain_id=chain_id, underlying_token=weth_addr, use_as_collateral=True
    )
    if not ok:
        raise RuntimeError(f"set_collateral(weth) failed: {tx}")
    print("set_collateral_tx", tx)

    ok, tx = await adapter.borrow(
        chain_id=chain_id, underlying_token=usdc_addr, qty=borrow_qty
    )
    if not ok:
        raise RuntimeError(f"borrow(usdc) failed: {tx}")
    print("borrow_tx", tx)

    ok, tx = await adapter.repay(
        chain_id=chain_id,
        underlying_token=usdc_addr,
        qty=0,
        repay_full=True,
    )
    if not ok:
        raise RuntimeError(f"repay_full(usdc) failed: {tx}")
    print("repay_tx", tx)

    ok, tx = await adapter.unlend(
        chain_id=chain_id,
        underlying_token=weth_addr,
        qty=0,
        native=True,
        withdraw_full=True,
    )
    if not ok:
        raise RuntimeError(f"unlend_full(weth) failed: {tx}")
    print("unlend_weth_tx", tx)

    if args.claim_rewards:
        ok, tx = await adapter.claim_all_rewards(chain_id=chain_id)
        if not ok:
            raise RuntimeError(f"claim_all_rewards failed: {tx}")
        print("claim_rewards_tx", tx)


if __name__ == "__main__":
    asyncio.run(main())
