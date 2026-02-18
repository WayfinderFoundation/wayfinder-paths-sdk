#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio

from eth_utils import to_checksum_address

from wayfinder_paths.adapters.morpho_adapter import MorphoAdapter
from wayfinder_paths.core.clients.MorphoClient import MORPHO_CLIENT
from wayfinder_paths.core.config import load_config
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.hyperlend_abi import WETH_ABI
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.run_strategy import create_signing_callback, get_strategy_config


def _pick_market_by_symbols(
    markets: list[dict], *, loan_symbol: str, collateral_symbol: str
) -> dict:
    loan_sym = loan_symbol.upper()
    coll_sym = collateral_symbol.upper()

    def _sym(m: dict, key: str) -> str:
        a = m.get(key) or {}
        return str(a.get("symbol") or "").upper()

    for m in markets:
        if _sym(m, "loanAsset") == loan_sym and _sym(m, "collateralAsset") == coll_sym:
            return m
    raise ValueError(
        f"No market found for loan_symbol={loan_symbol} collateral_symbol={collateral_symbol}"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Morpho Blue on-chain smoke test")
    parser.add_argument("--wallet-label", default="stablecoin_yield_strategy")
    parser.add_argument("--chain-id", type=int, default=CHAIN_ID_BASE)
    parser.add_argument("--lend-usdc", type=float, default=1.0)
    parser.add_argument("--collateral-usdc", type=float, default=5.0)
    parser.add_argument("--borrow-weth", type=float, default=0.0002)
    parser.add_argument(
        "--collateral-weth",
        type=float,
        default=0.0005,
        help="Only used if the chain has a USDC-loan/WETH-collateral market (e.g. Arbitrum).",
    )
    parser.add_argument(
        "--borrow-usdc",
        type=float,
        default=1.0,
        help="Only used if the chain has a USDC-loan/WETH-collateral market (e.g. Arbitrum).",
    )
    parser.add_argument("--vault-usdc", type=float, default=0.0)
    parser.add_argument(
        "--wrap-weth-buffer-eth",
        type=float,
        default=0.00001,
        help="Wrap a small ETH buffer into WETH (helps cover accrued interest / rounding).",
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
    adapter = MorphoAdapter(config=cfg, signing_callback=signing_cb)

    chain_id = int(args.chain_id)

    eth_bal = await get_token_balance(None, chain_id, addr)

    markets = await MORPHO_CLIENT.get_all_markets(chain_id=chain_id, listed=True)
    borrow_mode = "WETH_LOAN"
    try:
        borrow_market = _pick_market_by_symbols(
            markets, loan_symbol="WETH", collateral_symbol="USDC"
        )
    except ValueError:
        borrow_market = _pick_market_by_symbols(
            markets, loan_symbol="USDC", collateral_symbol="WETH"
        )
        borrow_mode = "USDC_LOAN"

    borrow_key = str(borrow_market["uniqueKey"])
    if borrow_mode == "WETH_LOAN":
        usdc_addr = str((borrow_market.get("collateralAsset") or {}).get("address"))
        weth_addr = str((borrow_market.get("loanAsset") or {}).get("address"))
    else:
        usdc_addr = str((borrow_market.get("loanAsset") or {}).get("address"))
        weth_addr = str((borrow_market.get("collateralAsset") or {}).get("address"))
    if not usdc_addr or not weth_addr:
        raise ValueError("borrow market missing token addresses")
    usdc_bal = await get_token_balance(usdc_addr, chain_id, addr)
    print(f"wallet={addr} chain_id={chain_id} usdc_raw={usdc_bal} eth_wei={eth_bal}")

    # Lend/withdraw-full market: USDC loan, any collateral. Pick the first.
    lend_market = next(
        m
        for m in markets
        if str((m.get("loanAsset") or {}).get("address", "")).lower()
        == usdc_addr.lower()
    )
    lend_key = str(lend_market["uniqueKey"])

    lend_qty = int(float(args.lend_usdc) * 10**6)
    collateral_usdc_qty = int(float(args.collateral_usdc) * 10**6)
    borrow_weth_qty = int(float(args.borrow_weth) * 10**18)
    collateral_weth_qty = int(float(args.collateral_weth) * 10**18)
    borrow_usdc_qty = int(float(args.borrow_usdc) * 10**6)

    print(f"lend_market={lend_key} lend_qty={lend_qty}")
    print(f"borrow_market={borrow_key} mode={borrow_mode}")

    ok, tx = await adapter.lend(
        chain_id=chain_id, market_unique_key=lend_key, qty=lend_qty
    )
    if not ok:
        raise RuntimeError(f"lend failed: {tx}")
    print("lend_tx", tx)

    ok, tx = await adapter.unlend(
        chain_id=chain_id, market_unique_key=lend_key, qty=0, withdraw_full=True
    )
    if not ok:
        raise RuntimeError(f"unlend failed: {tx}")
    print("unlend_tx", tx)

    buffer_eth = float(args.wrap_weth_buffer_eth or 0.0)
    if borrow_mode == "USDC_LOAN":
        # Need WETH for collateral; wrap from native ETH.
        wrap_total = collateral_weth_qty + int(buffer_eth * 10**18)
        if wrap_total > 0:
            wrap_tx = await encode_call(
                target=weth_addr,
                abi=WETH_ABI,
                fn_name="deposit",
                args=[],
                from_address=addr,
                chain_id=chain_id,
                value=int(wrap_total),
            )
            wrap_hash = await send_transaction(wrap_tx, signing_cb)
            print("wrap_weth_tx", wrap_hash)

        ok, tx = await adapter.supply_collateral(
            chain_id=chain_id, market_unique_key=borrow_key, qty=collateral_weth_qty
        )
        if not ok:
            raise RuntimeError(f"supply_collateral failed: {tx}")
        print("supply_collateral_tx", tx)

        ok, tx = await adapter.borrow(
            chain_id=chain_id, market_unique_key=borrow_key, qty=borrow_usdc_qty
        )
        if not ok:
            raise RuntimeError(f"borrow failed: {tx}")
        print("borrow_tx", tx)

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
            chain_id=chain_id, market_unique_key=borrow_key, qty=collateral_weth_qty
        )
        if not ok:
            raise RuntimeError(f"withdraw_collateral failed: {tx}")
        print("withdraw_collateral_tx", tx)
    else:
        ok, tx = await adapter.supply_collateral(
            chain_id=chain_id, market_unique_key=borrow_key, qty=collateral_usdc_qty
        )
        if not ok:
            raise RuntimeError(f"supply_collateral failed: {tx}")
        print("supply_collateral_tx", tx)

        ok, tx = await adapter.borrow(
            chain_id=chain_id, market_unique_key=borrow_key, qty=borrow_weth_qty
        )
        if not ok:
            raise RuntimeError(f"borrow failed: {tx}")
        print("borrow_tx", tx)

        if buffer_eth > 0:
            buffer_wei = int(buffer_eth * 10**18)
            wrap_tx = await encode_call(
                target=weth_addr,
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
            chain_id=chain_id, market_unique_key=borrow_key, qty=collateral_usdc_qty
        )
        if not ok:
            raise RuntimeError(f"withdraw_collateral failed: {tx}")
        print("withdraw_collateral_tx", tx)

    vault_usdc = float(args.vault_usdc or 0.0)
    if vault_usdc > 0:
        ok, vaults = await adapter.get_all_vaults(
            chain_id=chain_id, listed=True, include_v2=True
        )
        if not ok:
            raise RuntimeError(f"get_all_vaults failed: {vaults}")
        usdc_vaults = [
            v
            for v in vaults
            if str((v.get("asset") or {}).get("address") or "").lower()
            == usdc_addr.lower()
        ]
        if not usdc_vaults:
            raise RuntimeError("No USDC vaults found on this chain")
        vault = usdc_vaults[0]
        vault_addr = str(vault.get("address"))
        if not vault_addr:
            raise RuntimeError("vault missing address")

        deposit_qty = int(vault_usdc * 10**6)
        ok, tx = await adapter.vault_deposit(
            chain_id=chain_id, vault_address=vault_addr, assets=deposit_qty
        )
        if not ok:
            raise RuntimeError(f"vault_deposit failed: {tx}")
        print("vault_deposit_tx", tx)

        ok, tx = await adapter.vault_withdraw(
            chain_id=chain_id, vault_address=vault_addr, assets=deposit_qty
        )
        if not ok:
            raise RuntimeError(f"vault_withdraw failed: {tx}")
        print("vault_withdraw_tx", tx)


if __name__ == "__main__":
    asyncio.run(main())
