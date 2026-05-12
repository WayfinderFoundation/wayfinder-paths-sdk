from __future__ import annotations

import argparse
import asyncio
import time
from decimal import ROUND_DOWN, Decimal
from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.core.constants.polymarket import (
    POLYGON_CHAIN_ID,
    POLYGON_P_USDC_PROXY_ADDRESS,
    POLYGON_USDC_ADDRESS,
    POLYGON_USDC_E_ADDRESS,
    POLYMARKET_CONDITIONAL_TOKENS_ADDRESS,
)
from wayfinder_paths.core.constants.polymarket_abi import CONDITIONAL_TOKENS_ABI
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.core.utils.transaction import wait_for_transaction_receipt
from wayfinder_paths.core.utils.web3 import web3_from_chain_id
from wayfinder_paths.mcp.scripting import get_adapter

DEFAULT_TOKEN_ID = (
    "13915689317269078219168496739008737517740566192006337297676041270492637394586"
)
USDC_DECIMALS = Decimal("1000000")
SELL_SHARE_QUANTUM = Decimal("0.01")


def _fmt(raw: int) -> str:
    return f"{Decimal(raw) / USDC_DECIMALS:.6f}"


def _shares_from_raw(raw: int) -> Decimal:
    return (Decimal(raw) / USDC_DECIMALS).quantize(
        Decimal("0.000001"), rounding=ROUND_DOWN
    )


async def _conditional_balance(holder: str, token_id: str) -> int:
    async with web3_from_chain_id(POLYGON_CHAIN_ID) as web3:
        ctf = web3.eth.contract(
            address=to_checksum_address(POLYMARKET_CONDITIONAL_TOKENS_ADDRESS),
            abi=CONDITIONAL_TOKENS_ABI,
        )
        bal = await ctf.functions.balanceOf(
            to_checksum_address(holder), int(token_id)
        ).call(block_identifier="pending")
        return int(bal)


async def _native_balance(address: str) -> int:
    async with web3_from_chain_id(POLYGON_CHAIN_ID) as web3:
        return int(await web3.eth.get_balance(to_checksum_address(address)))


async def _wait_for_position_delta(
    *,
    holder: str,
    token_id: str,
    initial_raw: int,
    min_delta_raw: int,
    timeout_s: int,
    poll_s: float,
) -> int:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        current = await _conditional_balance(holder, token_id)
        if current - initial_raw >= min_delta_raw:
            return current
        await asyncio.sleep(poll_s)
    return await _conditional_balance(holder, token_id)


async def _wait_for_order_transactions(order: dict[str, Any]) -> None:
    tx_hashes = order.get("transactionsHashes") or order.get("transactionHashes") or []
    for tx_hash in tx_hashes:
        print(f"Waiting for order tx receipt: {tx_hash}")
        await wait_for_transaction_receipt(
            POLYGON_CHAIN_ID, str(tx_hash), timeout=300, confirmations=1
        )


async def _print_balances(
    *, owner: str, deposit_wallet: str, token_id: str, label: str
) -> dict[str, int]:
    owner_native = await _native_balance(owner)
    owner_pusd = await get_token_balance(
        POLYGON_P_USDC_PROXY_ADDRESS, POLYGON_CHAIN_ID, owner
    )
    owner_usdc = await get_token_balance(POLYGON_USDC_ADDRESS, POLYGON_CHAIN_ID, owner)
    owner_usdce = await get_token_balance(
        POLYGON_USDC_E_ADDRESS, POLYGON_CHAIN_ID, owner
    )
    deposit_pusd = await get_token_balance(
        POLYGON_P_USDC_PROXY_ADDRESS, POLYGON_CHAIN_ID, deposit_wallet
    )
    deposit_shares = await _conditional_balance(deposit_wallet, token_id)
    print(f"{label} balances")
    print(f"  owner native POL: {Decimal(owner_native) / Decimal(10**18):.6f}")
    print(f"  owner pUSD:       {_fmt(owner_pusd)}")
    print(f"  owner USDC:       {_fmt(owner_usdc)}")
    print(f"  owner USDC.e:     {_fmt(owner_usdce)}")
    print(f"  deposit pUSD:     {_fmt(deposit_pusd)}")
    print(f"  deposit shares:   {_fmt(deposit_shares)}")
    return {
        "owner_native": owner_native,
        "owner_pusd": owner_pusd,
        "owner_usdc": owner_usdc,
        "owner_usdce": owner_usdce,
        "deposit_pusd": deposit_pusd,
        "deposit_shares": deposit_shares,
    }


async def _require_quote(
    adapter: PolymarketAdapter, *, token_id: str, side: str, amount: float
) -> dict[str, Any]:
    ok, quote = await adapter.quote_market_order(
        token_id=token_id,
        side=side,  # type: ignore[arg-type]
        amount=amount,
    )
    if not ok or not isinstance(quote, dict):
        raise RuntimeError(f"{side} quote failed: {quote}")
    if not quote.get("fully_fillable"):
        raise RuntimeError(f"{side} quote is not fully fillable: {quote}")
    print(
        f"{side} quote: amount={amount} avg={quote.get('average_price')} "
        f"shares={quote.get('shares')} notional={quote.get('notional_usdc')}"
    )
    return quote


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live Polymarket v2 deposit-wallet roundtrip using PolymarketAdapter"
    )
    parser.add_argument("--wallet-label", default="main")
    parser.add_argument("--token-id", default=DEFAULT_TOKEN_ID)
    parser.add_argument("--buy-usdc", type=float, default=1.0)
    parser.add_argument("--min-native-pol", type=float, default=0.02)
    parser.add_argument("--position-timeout-s", type=int, default=180)
    parser.add_argument("--poll-s", type=float, default=3.0)
    parser.add_argument("--sell-attempts", type=int, default=3)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    adapter = await get_adapter(PolymarketAdapter, args.wallet_label)
    try:
        owner = adapter._require_wallet_address()
        deposit_wallet = adapter.trading_address()
        print(f"Owner wallet:   {owner}")
        print(f"Deposit wallet: {deposit_wallet}")
        print(f"Token ID:       {args.token_id}")

        before = await _print_balances(
            owner=owner,
            deposit_wallet=deposit_wallet,
            token_id=args.token_id,
            label="Initial",
        )
        min_native_raw = int(Decimal(str(args.min_native_pol)) * Decimal(10**18))
        if before["owner_native"] < min_native_raw:
            raise RuntimeError(
                f"Insufficient Polygon gas: have "
                f"{Decimal(before['owner_native']) / Decimal(10**18):.6f}, "
                f"need at least {args.min_native_pol}"
            )

        buy_quote = await _require_quote(
            adapter, token_id=args.token_id, side="BUY", amount=args.buy_usdc
        )
        if not args.execute:
            print("Dry run complete. Re-run with --execute to place the roundtrip.")
            return 0

        ok_setup, setup = await adapter.ensure_trading_setup(
            token_id=args.token_id,
            required_collateral=Decimal(str(args.buy_usdc)),
        )
        if not ok_setup:
            raise RuntimeError(f"ensure_trading_setup failed: {setup}")
        print(f"Setup: {setup}")

        shares_before = await _conditional_balance(deposit_wallet, args.token_id)
        print(f"Placing BUY market order for {args.buy_usdc} pUSD collateral")
        ok_buy, buy = await adapter.place_market_order(
            token_id=args.token_id,
            side="BUY",
            amount=args.buy_usdc,
        )
        if not ok_buy or not isinstance(buy, dict):
            raise RuntimeError(f"BUY failed: {buy}")
        print(f"BUY response: {buy}")
        await _wait_for_order_transactions(buy)

        expected_raw = int(
            Decimal(str(buy_quote.get("shares") or 0)) * USDC_DECIMALS * Decimal("0.90")
        )
        shares_after_buy = await _wait_for_position_delta(
            holder=deposit_wallet,
            token_id=args.token_id,
            initial_raw=shares_before,
            min_delta_raw=max(1, expected_raw),
            timeout_s=args.position_timeout_s,
            poll_s=args.poll_s,
        )
        bought_raw = max(0, shares_after_buy - shares_before)
        bought_shares = _shares_from_raw(bought_raw)
        if bought_shares <= 0:
            raise RuntimeError(
                f"No sellable shares detected after BUY. before={shares_before}, "
                f"after={shares_after_buy}"
            )
        print(f"Detected bought shares: {bought_shares}")

        for attempt in range(1, args.sell_attempts + 1):
            current_raw = await _conditional_balance(deposit_wallet, args.token_id)
            remaining_raw = max(0, current_raw - shares_before)
            sell_shares = _shares_from_raw(remaining_raw).quantize(
                SELL_SHARE_QUANTUM, rounding=ROUND_DOWN
            )
            if sell_shares < SELL_SHARE_QUANTUM:
                break

            await _require_quote(
                adapter,
                token_id=args.token_id,
                side="SELL",
                amount=float(sell_shares),
            )
            print(f"Placing SELL attempt {attempt} for {sell_shares} shares")
            ok_sell, sell = await adapter.place_market_order(
                token_id=args.token_id,
                side="SELL",
                amount=float(sell_shares),
            )
            if not ok_sell or not isinstance(sell, dict):
                raise RuntimeError(f"SELL failed: {sell}")
            print(f"SELL response: {sell}")
            await _wait_for_order_transactions(sell)

        final = await _print_balances(
            owner=owner,
            deposit_wallet=deposit_wallet,
            token_id=args.token_id,
            label="Final",
        )
        residual_raw = max(0, final["deposit_shares"] - shares_before)
        if residual_raw:
            residual = _shares_from_raw(residual_raw)
            reason = (
                f"< {SELL_SHARE_QUANTUM} minimum tradable size"
                if residual < SELL_SHARE_QUANTUM
                else "remaining after sell attempts"
            )
            print(f"Residual shares: {residual} ({reason})")
        return 0
    finally:
        await adapter.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
