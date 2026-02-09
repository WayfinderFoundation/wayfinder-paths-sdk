from __future__ import annotations

import argparse
import asyncio
import time
from typing import Any

from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.core.constants.polymarket import (
    POLYGON_CHAIN_ID,
    POLYGON_USDC_ADDRESS,
    POLYGON_USDC_E_ADDRESS,
)
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.core.utils.transaction import wait_for_transaction_receipt
from wayfinder_paths.mcp.scripting import get_adapter


async def _wait_for_balance(
    *,
    address: str,
    token_address: str,
    chain_id: int,
    min_increase: int,
    initial: int,
    timeout_s: int = 180,
    poll_s: int = 5,
) -> int:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        cur = await get_token_balance(token_address, chain_id, address)
        if cur - initial >= min_increase:
            return cur
        await asyncio.sleep(poll_s)
    return await get_token_balance(token_address, chain_id, address)

async def _pick_tradable(
    adapter: PolymarketAdapter,
    markets: list[dict[str, Any]],
    *,
    outcome: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    # Try open markets first, but fall back to any market whose token_id resolves on CLOB.
    for attempt_open_only in (True, False):
        for m in markets:
            if not m.get("enableOrderBook") or not m.get("clobTokenIds"):
                continue
            if attempt_open_only and (m.get("closed") is True or m.get("active") is False):
                continue

            ok_tid, token_id = adapter.resolve_clob_token_id(market=m, outcome=outcome)
            if not ok_tid:
                # Fallback: pick first outcome when the query isn't a YES/NO market.
                ok_tid, token_id = adapter.resolve_clob_token_id(market=m, outcome=0)
                if not ok_tid:
                    continue

            ok_price, price = await adapter.get_price(token_id=token_id, side="BUY")
            if ok_price and isinstance(price, dict):
                return m, token_id, price

    raise RuntimeError("No tradable market found for query")


async def main() -> int:
    p = argparse.ArgumentParser(description="Polymarket end-to-end smoketest")
    p.add_argument("--wallet-label", default="main")
    p.add_argument("--query", default="super bowl")
    p.add_argument("--outcome", default="YES")
    p.add_argument("--deposit-usdc", type=float, default=5.0)
    p.add_argument("--trade-usdce", type=float, default=2.0)
    p.add_argument("--withdraw-usdce", type=float, default=2.0)
    p.add_argument(
        "--execute",
        action="store_true",
        help="Broadcast on-chain transactions (deposit/approve/trade/withdraw).",
    )
    args = p.parse_args()

    adapter: PolymarketAdapter
    addr: str | None = None
    if args.execute:
        adapter = get_adapter(PolymarketAdapter, args.wallet_label)
        addr = adapter._resolve_funder()
    else:
        adapter = PolymarketAdapter()

    try:
        if addr:
            usdc0 = await get_token_balance(
                POLYGON_USDC_ADDRESS, POLYGON_CHAIN_ID, addr
            )
            usdce0 = await get_token_balance(
                POLYGON_USDC_E_ADDRESS, POLYGON_CHAIN_ID, addr
            )
            print(f"Wallet: {addr}")
            print(f"USDC:   {usdc0/1e6:.6f}")
            print(f"USDC.e: {usdce0/1e6:.6f}")

        ok, markets = await adapter.search_markets_fuzzy(query=args.query, limit=20)
        if not ok:
            raise RuntimeError(f"search_markets_fuzzy failed: {markets}")
        try:
            market, token_id, price = await _pick_tradable(
                adapter, markets, outcome=str(args.outcome)
            )
        except RuntimeError as err:
            print("No tradable market in search results; falling back to trending...")
            ok2, trending = await adapter.list_markets(
                closed=False, limit=50, order="volume24hr", ascending=False
            )
            if not ok2:
                raise RuntimeError(
                    f"list_markets trending failed: {trending}"
                ) from err
            market, token_id, price = await _pick_tradable(
                adapter, trending, outcome=str(args.outcome)
            )
        slug = market.get("slug")
        print(f"Selected market: {slug}")
        print(f"Token: {token_id}")
        print(f"Price(BUY): {price.get('price')}")

        ok, hist = await adapter.get_prices_history(token_id=token_id, interval="1d", fidelity=5)
        if not ok:
            raise RuntimeError(f"get_prices_history failed: {hist}")
        print(f"History points: {len(hist.get('history') or [])}")

        if not args.execute:
            print("Dry-run complete. Re-run with --execute to deposit/approve/trade/withdraw.")
            return 0
        if not addr:
            raise RuntimeError("Internal error: missing wallet address in execute mode")

        # Convert USDC -> USDC.e if needed
        usdce_before = await get_token_balance(
            POLYGON_USDC_E_ADDRESS, POLYGON_CHAIN_ID, addr
        )
        if (usdce_before / 1e6) < float(args.trade_usdce):
            print(f"Converting {args.deposit_usdc} USDC -> USDC.e (BRAP preferred; bridge fallback)...")
            ok, dep = await adapter.bridge_deposit(
                from_chain_id=POLYGON_CHAIN_ID,
                from_token_address=POLYGON_USDC_ADDRESS,
                amount=float(args.deposit_usdc),
                recipient_address=addr,
                token_decimals=6,
            )
            if not ok:
                raise RuntimeError(f"bridge_deposit failed: {dep}")
            method = dep.get("method") if isinstance(dep, dict) else None
            print(f"Convert tx (method={method or 'unknown'}): {dep.get('tx_hash') if isinstance(dep, dict) else None}")

            if method == "polymarket_bridge":
                # Bridge settlement is async; wait for USDC.e to arrive.
                min_increase = int(float(args.trade_usdce) * 1_000_000)
                usdce_after = await _wait_for_balance(
                    address=addr,
                    token_address=POLYGON_USDC_E_ADDRESS,
                    chain_id=POLYGON_CHAIN_ID,
                    min_increase=min_increase,
                    initial=usdce_before,
                    timeout_s=180,
                    poll_s=5,
                )
                print(f"USDC.e after deposit: {usdce_after/1e6:.6f}")

        print("Ensuring on-chain approvals (USDC.e + ConditionalTokens)...")
        ok, appr = await adapter.ensure_onchain_approvals()
        if not ok:
            raise RuntimeError(f"ensure_onchain_approvals failed: {appr}")
        print(f"Approval txs: {appr.get('tx_hashes')}")

        print(f"Placing BUY market order for ${args.trade_usdce}...")
        ok, buy = await adapter.place_market_order(
            token_id=token_id, side="BUY", amount=float(args.trade_usdce)
        )
        if not ok:
            raise RuntimeError(f"place_market_order(BUY) failed: {buy}")
        print(f"BUY response: {buy}")

        tx_hashes = []
        if isinstance(buy, dict):
            tx_hashes = buy.get("transactionsHashes") or []
        if tx_hashes:
            print(f"Waiting for match tx confirmation: {tx_hashes[0]}...")
            await wait_for_transaction_receipt(
                POLYGON_CHAIN_ID, str(tx_hashes[0]), timeout=300
            )

        # Attempt a small sell as a cash-out path.
        ok, sell = await adapter.place_market_order(
            token_id=token_id, side="SELL", amount=1.0
        )
        if ok:
            print(f"SELL response: {sell}")
        else:
            print(f"SELL failed (may not have >=1 share yet): {sell}")

        # Withdraw some USDC.e back to native USDC (bridge)
        usdce_now = await get_token_balance(POLYGON_USDC_E_ADDRESS, POLYGON_CHAIN_ID, addr)
        if (usdce_now / 1e6) >= float(args.withdraw_usdce):
            usdc_before = await get_token_balance(POLYGON_USDC_ADDRESS, POLYGON_CHAIN_ID, addr)
            print(f"Converting {args.withdraw_usdce} USDC.e -> USDC (BRAP preferred; bridge fallback)...")
            ok, wd = await adapter.bridge_withdraw(
                amount_usdce=float(args.withdraw_usdce),
                to_chain_id=str(POLYGON_CHAIN_ID),
                to_token_address=POLYGON_USDC_ADDRESS,
                recipient_addr=addr,
                token_decimals=6,
            )
            if not ok:
                raise RuntimeError(f"bridge_withdraw failed: {wd}")
            method = wd.get("method") if isinstance(wd, dict) else None
            print(f"Convert tx (method={method or 'unknown'}): {wd.get('tx_hash') if isinstance(wd, dict) else None}")
            if wd.get("method") == "polymarket_bridge":
                # Bridge settlement is async; wait for any USDC increase.
                await _wait_for_balance(
                    address=addr,
                    token_address=POLYGON_USDC_ADDRESS,
                    chain_id=POLYGON_CHAIN_ID,
                    min_increase=1,
                    initial=int(usdc_before),
                    timeout_s=180,
                    poll_s=5,
                )

        usdc1 = await get_token_balance(POLYGON_USDC_ADDRESS, POLYGON_CHAIN_ID, addr)
        usdce1 = await get_token_balance(POLYGON_USDC_E_ADDRESS, POLYGON_CHAIN_ID, addr)
        print("Final balances:")
        print(f"USDC:   {usdc1/1e6:.6f}")
        print(f"USDC.e: {usdce1/1e6:.6f}")
        return 0
    finally:
        await adapter.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
