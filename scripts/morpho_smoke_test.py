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
from wayfinder_paths.core.utils.transaction import (
    encode_call,
    send_transaction,
    wait_for_transaction_receipt,
)
from wayfinder_paths.core.utils.wallets import get_wallet_signing_callback
from wayfinder_paths.run_strategy import get_strategy_config

BASE_WETH_USDC_MARKET_ID = (
    "0x3b3769cfca57be2eaed03fcc5299c25691b77781a1e124e7a8d520eb9a7eabb5"
)


def _market_id(market: dict) -> str:
    market_id = market.get("marketId") or market.get("uniqueKey")
    if not market_id:
        raise ValueError("market missing marketId")
    return str(market_id)


def _liquidity_assets(market: dict) -> int:
    try:
        return int((market.get("state") or {}).get("liquidityAssets") or 0)
    except (TypeError, ValueError):
        return 0


def _pick_market_by_symbols(
    markets: list[dict],
    *,
    loan_symbol: str,
    collateral_symbol: str,
    preferred_market_id: str | None = None,
) -> dict:
    loan_sym = loan_symbol.upper()
    coll_sym = collateral_symbol.upper()

    def _sym(m: dict, key: str) -> str:
        a = m.get(key) or {}
        return str(a.get("symbol") or "").upper()

    candidates = [
        m
        for m in markets
        if _sym(m, "loanAsset") == loan_sym and _sym(m, "collateralAsset") == coll_sym
    ]
    if preferred_market_id:
        preferred = preferred_market_id.lower()
        for m in candidates:
            if _market_id(m).lower() == preferred:
                return m
    if candidates:
        return max(candidates, key=_liquidity_assets)
    raise ValueError(
        f"No market found for loan_symbol={loan_symbol} collateral_symbol={collateral_symbol}"
    )


def _pick_market_by_loan_token(markets: list[dict], *, loan_token: str) -> dict:
    token = loan_token.lower()
    candidates = [
        m
        for m in markets
        if str((m.get("loanAsset") or {}).get("address", "")).lower() == token
    ]
    if not candidates:
        raise ValueError(f"No market found for loan_token={loan_token}")
    return max(candidates, key=_liquidity_assets)


async def _assert_market_reads(
    adapter: MorphoAdapter, *, chain_id: int, market_key: str, label: str
) -> None:
    ok, entry = await adapter.get_market_entry(
        chain_id=chain_id, market_unique_key=market_key
    )
    if not ok or not isinstance(entry, dict):
        raise RuntimeError(f"{label} get_market_entry failed: {entry}")
    if str(entry.get("marketId") or "").lower() != market_key.lower():
        raise RuntimeError(f"{label} marketId mismatch: {entry.get('marketId')}")
    if not (entry.get("loan") or {}).get("address"):
        raise RuntimeError(f"{label} missing loan asset")
    if not (entry.get("collateral") or {}).get("address"):
        raise RuntimeError(f"{label} missing collateral asset")
    if not (entry.get("state") or {}):
        raise RuntimeError(f"{label} missing state")

    ok, state = await adapter.get_market_state(
        chain_id=chain_id, market_unique_key=market_key
    )
    if not ok or not isinstance(state, dict):
        raise RuntimeError(f"{label} get_market_state failed: {state}")

    ok, history = await adapter.get_market_historical_apy(
        chain_id=chain_id,
        market_unique_key=market_key,
        interval="DAY",
    )
    if not ok or not isinstance(history, dict):
        raise RuntimeError(f"{label} get_market_historical_apy failed: {history}")
    series = history.get("series") or {}
    if not isinstance(series, dict) or "supplyApy" not in series:
        raise RuntimeError(f"{label} missing APY history series")

    print(
        f"{label}_market_ok id={market_key} "
        f"loan={(entry.get('loan') or {}).get('symbol')} "
        f"collateral={(entry.get('collateral') or {}).get('symbol')}"
    )


async def _assert_zero_position(
    adapter: MorphoAdapter,
    *,
    chain_id: int,
    market_key: str,
    account: str,
    label: str,
) -> None:
    supply_shares, borrow_shares, collateral = await adapter._position(
        chain_id=chain_id,
        market_unique_key=market_key,
        account=account,
    )
    if supply_shares or borrow_shares or collateral:
        raise RuntimeError(
            f"{label} position not zero for {market_key}: "
            f"supply_shares={supply_shares} borrow_shares={borrow_shares} "
            f"collateral={collateral}"
        )
    print(f"{label}_position_zero market={market_key}")


async def _print_confirmed_tx(chain_id: int, label: str, tx: object) -> None:
    if tx is None:
        print(f"{label}_tx none")
        return
    tx_hash = str(tx)
    if not tx_hash.startswith("0x"):
        raise RuntimeError(f"{label} returned non-transaction hash: {tx_hash}")
    receipt = await wait_for_transaction_receipt(
        chain_id=chain_id,
        txn_hash=tx_hash,
        confirmations=0,
    )
    print(
        f"{label}_tx {tx_hash} status={receipt.get('status')} "
        f"block={receipt.get('blockNumber')}"
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

    signing_cb, signing_addr = await get_wallet_signing_callback(args.wallet_label)
    if to_checksum_address(signing_addr) != addr:
        raise ValueError(
            f"Signing wallet mismatch: config={addr} signer={signing_addr}"
        )
    adapter = MorphoAdapter(config=cfg, sign_callback=signing_cb, wallet_address=addr)

    chain_id = int(args.chain_id)

    eth_bal = await get_token_balance(None, chain_id, addr)

    markets = await MORPHO_CLIENT.get_all_markets(chain_id=chain_id, listed=True)
    if not markets:
        raise RuntimeError(f"No Morpho markets returned for chain_id={chain_id}")

    borrow_mode = "WETH_LOAN"
    try:
        preferred = BASE_WETH_USDC_MARKET_ID if chain_id == CHAIN_ID_BASE else None
        borrow_market = _pick_market_by_symbols(
            markets,
            loan_symbol="WETH",
            collateral_symbol="USDC",
            preferred_market_id=preferred,
        )
    except ValueError:
        borrow_market = _pick_market_by_symbols(
            markets, loan_symbol="USDC", collateral_symbol="WETH"
        )
        borrow_mode = "USDC_LOAN"

    borrow_key = _market_id(borrow_market)
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

    # Lend/withdraw-full market: USDC loan, any collateral. Pick the deepest.
    lend_market = _pick_market_by_loan_token(markets, loan_token=usdc_addr)
    lend_key = _market_id(lend_market)

    lend_qty = int(float(args.lend_usdc) * 10**6)
    collateral_usdc_qty = int(float(args.collateral_usdc) * 10**6)
    borrow_weth_qty = int(float(args.borrow_weth) * 10**18)
    collateral_weth_qty = int(float(args.collateral_weth) * 10**18)
    borrow_usdc_qty = int(float(args.borrow_usdc) * 10**6)
    vault_usdc = float(args.vault_usdc or 0.0)

    if lend_qty <= 0:
        raise ValueError("--lend-usdc must be positive")
    if borrow_mode == "WETH_LOAN" and collateral_usdc_qty <= 0:
        raise ValueError("--collateral-usdc must be positive")
    if borrow_mode == "USDC_LOAN" and collateral_weth_qty <= 0:
        raise ValueError("--collateral-weth must be positive")
    if borrow_mode == "WETH_LOAN" and borrow_weth_qty <= 0:
        raise ValueError("--borrow-weth must be positive")
    if borrow_mode == "USDC_LOAN" and borrow_usdc_qty <= 0:
        raise ValueError("--borrow-usdc must be positive")

    required_usdc = max(
        lend_qty,
        collateral_usdc_qty if borrow_mode == "WETH_LOAN" else 0,
    )
    required_usdc = max(required_usdc, int(vault_usdc * 10**6))
    if usdc_bal < required_usdc:
        raise RuntimeError(
            f"Insufficient USDC: balance={usdc_bal} required={required_usdc}"
        )

    print(f"lend_market={lend_key} lend_qty={lend_qty}")
    print(f"borrow_market={borrow_key} mode={borrow_mode}")

    await _assert_market_reads(
        adapter, chain_id=chain_id, market_key=lend_key, label="lend"
    )
    await _assert_market_reads(
        adapter, chain_id=chain_id, market_key=borrow_key, label="borrow"
    )
    await _assert_zero_position(
        adapter, chain_id=chain_id, market_key=lend_key, account=addr, label="pre_lend"
    )
    if borrow_key.lower() != lend_key.lower():
        await _assert_zero_position(
            adapter,
            chain_id=chain_id,
            market_key=borrow_key,
            account=addr,
            label="pre_borrow",
        )

    ok, tx = await adapter.lend(
        chain_id=chain_id, market_unique_key=lend_key, qty=lend_qty
    )
    if not ok:
        raise RuntimeError(f"lend failed: {tx}")
    await _print_confirmed_tx(chain_id, "lend", tx)

    ok, tx = await adapter.unlend(
        chain_id=chain_id, market_unique_key=lend_key, qty=0, withdraw_full=True
    )
    if not ok:
        raise RuntimeError(f"unlend failed: {tx}")
    await _print_confirmed_tx(chain_id, "unlend", tx)
    await _assert_zero_position(
        adapter, chain_id=chain_id, market_key=lend_key, account=addr, label="post_lend"
    )

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
            await _print_confirmed_tx(chain_id, "wrap_weth", wrap_hash)

        ok, tx = await adapter.supply_collateral(
            chain_id=chain_id, market_unique_key=borrow_key, qty=collateral_weth_qty
        )
        if not ok:
            raise RuntimeError(f"supply_collateral failed: {tx}")
        await _print_confirmed_tx(chain_id, "supply_collateral", tx)

        ok, tx = await adapter.borrow(
            chain_id=chain_id, market_unique_key=borrow_key, qty=borrow_usdc_qty
        )
        if not ok:
            raise RuntimeError(f"borrow failed: {tx}")
        await _print_confirmed_tx(chain_id, "borrow", tx)

        ok, tx = await adapter.repay(
            chain_id=chain_id,
            market_unique_key=borrow_key,
            qty=0,
            repay_full=True,
        )
        if not ok:
            raise RuntimeError(f"repay_full failed: {tx}")
        await _print_confirmed_tx(chain_id, "repay", tx)

        ok, tx = await adapter.withdraw_collateral(
            chain_id=chain_id, market_unique_key=borrow_key, qty=collateral_weth_qty
        )
        if not ok:
            raise RuntimeError(f"withdraw_collateral failed: {tx}")
        await _print_confirmed_tx(chain_id, "withdraw_collateral", tx)
    else:
        ok, tx = await adapter.supply_collateral(
            chain_id=chain_id, market_unique_key=borrow_key, qty=collateral_usdc_qty
        )
        if not ok:
            raise RuntimeError(f"supply_collateral failed: {tx}")
        await _print_confirmed_tx(chain_id, "supply_collateral", tx)

        ok, tx = await adapter.borrow(
            chain_id=chain_id, market_unique_key=borrow_key, qty=borrow_weth_qty
        )
        if not ok:
            raise RuntimeError(f"borrow failed: {tx}")
        await _print_confirmed_tx(chain_id, "borrow", tx)

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
            await _print_confirmed_tx(chain_id, "wrap_weth", wrap_hash)

        ok, tx = await adapter.repay(
            chain_id=chain_id,
            market_unique_key=borrow_key,
            qty=0,
            repay_full=True,
        )
        if not ok:
            raise RuntimeError(f"repay_full failed: {tx}")
        await _print_confirmed_tx(chain_id, "repay", tx)

        ok, tx = await adapter.withdraw_collateral(
            chain_id=chain_id, market_unique_key=borrow_key, qty=collateral_usdc_qty
        )
        if not ok:
            raise RuntimeError(f"withdraw_collateral failed: {tx}")
        await _print_confirmed_tx(chain_id, "withdraw_collateral", tx)

    await _assert_zero_position(
        adapter,
        chain_id=chain_id,
        market_key=borrow_key,
        account=addr,
        label="post_borrow",
    )

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
        await _print_confirmed_tx(chain_id, "vault_deposit", tx)

        ok, tx = await adapter.vault_withdraw(
            chain_id=chain_id, vault_address=vault_addr, assets=deposit_qty
        )
        if not ok:
            raise RuntimeError(f"vault_withdraw failed: {tx}")
        await _print_confirmed_tx(chain_id, "vault_withdraw", tx)


if __name__ == "__main__":
    asyncio.run(main())
