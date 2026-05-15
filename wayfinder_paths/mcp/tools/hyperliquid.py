from __future__ import annotations

import asyncio
import difflib
import re
from decimal import Decimal
from typing import Any, Literal

from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter
from wayfinder_paths.adapters.hyperliquid_adapter.adapter import (
    decode_outcome_encoding,
    outcome_asset_id,
)
from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.constants.hyperliquid import (
    ARBITRUM_USDC_ADDRESS,
    DEFAULT_HYPERLIQUID_BUILDER_FEE,
    HYPERLIQUID_BRIDGE_ADDRESS,
    MARKET_SEARCH_ALIASES,
    MARKET_SEARCH_MIN_MATCH_SCORE,
    MARKET_TYPE_HIP4,
    MIN_ORDER_USD_NOTIONAL,
)
from wayfinder_paths.core.utils.tokens import build_send_transaction
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    catch_errors,
    err,
    ok,
    parse_amount_to_raw,
    resolve_wallet_address,
    throw_if_empty_str,
    throw_if_none,
    throw_if_not_int,
    throw_if_not_number,
)


def _annotate_hl_profile(
    *,
    address: str,
    label: str,
    action: str,
    status: str,
    details: dict[str, Any] | None = None,
) -> None:
    store = WalletProfileStore.default()
    store.annotate_safe(
        address=address,
        label=label,
        protocol="hyperliquid",
        action=action,
        tool=f"hyperliquid_{action}",
        status=status,
        chain_id=999,
        details=details,
    )


async def _ensure_builder_fee_approval(
    adapter: HyperliquidAdapter,
    *,
    sender: str,
    effects: list[dict[str, Any]],
) -> None:
    builder_addr = DEFAULT_HYPERLIQUID_BUILDER_FEE["b"]
    desired = DEFAULT_HYPERLIQUID_BUILDER_FEE["f"]
    ok_fee, current = await adapter.get_max_builder_fee(
        user=sender, builder=builder_addr
    )
    effects.append(
        {
            "type": "hl",
            "label": "get_max_builder_fee",
            "ok": ok_fee,
            "result": {
                "current_tenths_bp": int(current),
                "desired_tenths_bp": desired,
            },
        }
    )
    if ok_fee and int(current) >= desired:
        return

    ok_appr, appr = await adapter.approve_builder_fee(
        builder=builder_addr,
        max_fee_rate=f"{desired / 1000:.3f}%",
        address=sender,
    )
    effects.append(
        {
            "type": "hl",
            "label": "approve_builder_fee",
            "ok": ok_appr,
            "result": appr,
        }
    )
    if not ok_appr:
        raise ValueError(f"Failed to approve Wayfinder builder fee: {appr}")


async def _make_hl_adapter(wallet_label: str) -> tuple[HyperliquidAdapter, str]:
    strategy_raw = CONFIG.get("strategy")
    strategy_cfg = strategy_raw if isinstance(strategy_raw, dict) else {}
    adapter = await get_adapter(
        HyperliquidAdapter, wallet_label, config_overrides=dict(strategy_cfg)
    )
    return adapter, adapter.wallet_address


async def _resolve_asset(
    adapter: HyperliquidAdapter, asset_name: str
) -> tuple[int, str]:
    """Returns (asset_id, market_type) for a canonical HL asset_name or raises."""
    resolved_asset_id = await adapter.get_asset_id(asset_name)
    if resolved_asset_id is None:
        raise ValueError(
            f"Invalid asset_name {asset_name!r}. Expected 'BTC-USDC' (core perp), "
            "'xyz:SP500' (HIP-3 perp), 'BTC/USDC' (spot), or '#40' (HIP-4 outcome). "
            "Call hyperliquid_search_market to look up the canonical name."
        )
    return resolved_asset_id, adapter.get_market_type(asset_name)


async def _resolve_perp_or_spot_size(
    *,
    adapter: HyperliquidAdapter,
    asset_name: str,
    resolved_asset_id: int,
    market_type: str,
    size: float | None,
    usd_amount: float | None,
    px_for_sizing: float | None,
) -> tuple[float, dict[str, Any], float | None]:
    """Resolve a perp/spot order's raw asset-unit size from `size` or `usd_amount`.

    `usd_amount` is always treated as USD notional. Returns the raw size,
    a `sizing` audit dict for the response, and the resolved price used (mid or
    the caller's limit price).
    """
    if size is not None and usd_amount is not None:
        raise ValueError(
            "Provide either size (asset units) or usd_amount (USD notional), not both"
        )

    if size is not None:
        sz = throw_if_not_number("size must be a number", size)
        if sz <= 0:
            raise ValueError("size must be positive")
        return float(sz), {"source": "size"}, px_for_sizing

    throw_if_none("Provide either size (asset units) or usd_amount", usd_amount)
    usd_amt = throw_if_not_number("usd_amount must be a number", usd_amount)
    if usd_amt <= 0:
        raise ValueError("usd_amount must be positive")

    if px_for_sizing is None:
        ok_mids, mids = await adapter.get_all_mid_prices()
        if not ok_mids or not isinstance(mids, dict):
            raise ValueError("Failed to fetch mid prices")
        mid: float | None = None
        for key in adapter.get_mid_price_key(asset_name, resolved_asset_id):
            v = mids.get(key)
            if v is None:
                continue
            try:
                mid = float(v)
                break
            except (TypeError, ValueError):
                continue
        if mid is None or mid <= 0:
            raise ValueError(f"Could not resolve mid price for {asset_name}")
        px_for_sizing = mid

    sz = float(usd_amt) / float(px_for_sizing)
    sizing: dict[str, Any] = {
        "source": "usd_amount",
        "usd_amount": float(usd_amt),
        "notional_usd": float(usd_amt),
        "price_used": float(px_for_sizing),
        "market_type": market_type,
    }
    return sz, sizing, px_for_sizing


def _validate_size_and_notional(
    *,
    adapter: HyperliquidAdapter,
    asset_id: int,
    size_requested: float,
    sz_valid: float,
    sizing: dict[str, Any],
    px_for_sizing: float | None,
) -> None:
    if sz_valid <= 0:
        sz_decimals = adapter.get_sz_decimals(asset_id)
        min_tick = float(Decimal(10) ** (-sz_decimals))
        raise ValueError(
            f"size {size_requested} rounds down to 0 — asset has szDecimals={sz_decimals} "
            f"(lot size = {min_tick}). Try size={min_tick}."
        )
    if sizing["source"] == "usd_amount" and px_for_sizing is not None:
        final_notional = float(sz_valid) * float(px_for_sizing)
        if final_notional < MIN_ORDER_USD_NOTIONAL:
            sz_decimals = adapter.get_sz_decimals(asset_id)
            tick = float(Decimal(10) ** (-sz_decimals))
            # Smallest lot whose notional clears the floor.
            ticks_needed = -(-MIN_ORDER_USD_NOTIONAL // (tick * px_for_sizing))
            suggested_usd = ticks_needed * tick * px_for_sizing
            raise ValueError(
                f"After lot-size rounding, notional is ${final_notional:.4f} — HL "
                f"requires >= ${MIN_ORDER_USD_NOTIONAL:.2f}. Try usd_amount={suggested_usd:.2f}."
            )


def _validate_price(
    *,
    adapter: HyperliquidAdapter,
    asset_id: int,
    price: float,
) -> None:
    """Apply HL's price rounding (5 sig figs → max decimals) and reject divergence."""
    price_decimals = adapter.get_price_decimals(asset_id)
    rounded = round(float(f"{price:.5g}"), price_decimals)
    if rounded != float(price):
        raise ValueError(
            f"price {price} is invalid — HL allows ≤ 5 sig figs and ≤ {price_decimals} decimals. "
            f"Try price={rounded}."
        )


async def _place_outcome_order(
    *,
    adapter: HyperliquidAdapter,
    sender: str,
    wallet_label: str,
    asset_name: str,
    is_buy: bool,
    order_type: Literal["market", "limit"],
    size: float | int | None,
    usd_amount: float | None,
    price: float | None,
    slippage: float,
    reduce_only: bool,
    cloid: str | None,
) -> dict[str, Any]:
    """HIP-4 outcome leg of hyperliquid_place_{market,limit}_order.

    Outcomes settle in USDH (token 360), trade as integer contracts, and are
    zero-fee (no builder fee). `usd_amount` sizing is market-only — limit
    outcome orders require explicit integer `size`.
    """
    if order_type == "limit":
        throw_if_none("price is required for limit orders", price)

    outcome_id_v, side_v = decode_outcome_encoding(int(asset_name[1:]))
    asset_id = outcome_asset_id(outcome_id_v, side_v)
    if price is not None:
        _validate_price(adapter=adapter, asset_id=asset_id, price=float(price))

    size_i: int | None = None
    if size is not None:
        as_int = int(size)
        if float(size) != as_int:
            raise ValueError(
                f"size {size} must be an integer (HIP-4 outcomes use integer contracts). "
                f"Try size={as_int}."
            )
        if as_int <= 0:
            raise ValueError("size must be a positive integer")
        size_i = as_int

    sizing: dict[str, Any] = {"source": "size", "market_type": MARKET_TYPE_HIP4}
    if size_i is None:
        throw_if_none("size or usd_amount is required for outcome orders", usd_amount)
        if order_type != "market":
            raise ValueError(
                "usd_amount sizing is only supported for market outcome orders"
            )
        ok_mids, mids = await adapter.get_all_mid_prices()
        if not ok_mids or not isinstance(mids, dict):
            return err("price_error", "Failed to fetch mid prices")
        mid = mids.get(asset_name)
        if mid is None or float(mid) <= 0:
            return err("price_error", f"Could not resolve mid price for {asset_name}")
        size_i = max(1, round(float(usd_amount) / float(mid)))
        sizing = {
            "source": "usd_amount",
            "usd_amount": float(usd_amount),
            "price_used": float(mid),
            "market_type": MARKET_TYPE_HIP4,
        }

    effects: list[dict[str, Any]] = []
    ok_order, res = await adapter.place_outcome_order(
        outcome_id=outcome_id_v,
        side=side_v,
        is_buy=bool(is_buy),
        size=size_i,
        price=None if price is None else float(price),
        slippage=float(slippage),
        tif="Ioc" if order_type == "market" else "Gtc",
        reduce_only=bool(reduce_only),
        cloid=cloid,
        address=sender,
    )
    effects.append(
        {"type": "hl", "label": "place_outcome_order", "ok": ok_order, "result": res}
    )
    status = "confirmed" if ok_order else "failed"
    _annotate_hl_profile(
        address=sender,
        label=wallet_label,
        action="place_outcome_order",
        status=status,
        details={
            "asset_id": asset_id,
            "asset_name": asset_name,
            "outcome_id": outcome_id_v,
            "side": side_v,
            "is_buy": bool(is_buy),
            "size": size_i,
        },
    )
    return ok(
        {
            "status": status,
            "wallet_label": wallet_label,
            "address": sender,
            "asset_id": asset_id,
            "asset_name": asset_name,
            "outcome_id": outcome_id_v,
            "side": side_v,
            "order": {
                "order_type": order_type,
                "is_buy": bool(is_buy),
                "size": size_i,
                "price": float(price) if price is not None else None,
                "slippage": float(slippage) if order_type == "market" else None,
                "reduce_only": bool(reduce_only),
                "cloid": cloid,
                "sizing": sizing,
            },
            "effects": effects,
        }
    )


@catch_errors
async def hyperliquid_deposit(
    *,
    wallet_label: str,
    amount_usdc: float,
) -> dict[str, Any]:
    """Bridge USDC from Arbitrum into the Hyperliquid clearinghouse.

    Deposits below 5 USDC are **permanently lost** by the bridge. Auto-waits for
    the credit on Hyperliquid before returning.

    Args:
        wallet_label: Wallet to send Arbitrum USDC from.
        amount_usdc: USDC to deposit (must be >= 5).
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    amt = throw_if_not_number("amount_usdc must be a number", amount_usdc)
    if amt < 5:
        raise ValueError("amount_usdc must be >= 5 USDC (HL deposits below are lost)")

    adapter, deposit_sender = await _make_hl_adapter(wallet_label)

    effects: list[dict[str, Any]] = []
    transaction = await build_send_transaction(
        from_address=deposit_sender,
        to_address=HYPERLIQUID_BRIDGE_ADDRESS,
        token_address=ARBITRUM_USDC_ADDRESS,
        chain_id=42161,
        amount=int(parse_amount_to_raw(str(amt), 6)),
    )
    try:
        tx_hash = await send_transaction(
            transaction, adapter.sign_callback, wait_for_receipt=True
        )
        sent_ok = True
        sent_result: dict[str, Any] = {"txn_hash": tx_hash, "chain_id": 42161}
    except Exception as exc:  # noqa: BLE001
        sent_ok = False
        sent_result = {"error": str(exc), "chain_id": 42161}
    effects.append(
        {"type": "hl", "label": "deposit", "ok": sent_ok, "result": sent_result}
    )

    if sent_ok:
        ok_landed, final_balance = await adapter.wait_for_deposit(deposit_sender, amt)
        effects.append(
            {
                "type": "hl",
                "label": "wait_for_credit",
                "ok": ok_landed,
                "result": {
                    "confirmed": bool(ok_landed),
                    "final_balance_usd": float(final_balance),
                },
            }
        )

    status = "confirmed" if all(e["ok"] for e in effects) else "failed"
    _annotate_hl_profile(
        address=deposit_sender,
        label=wallet_label,
        action="deposit",
        status=status,
        details={"amount_usdc": amt, "chain_id": 42161},
    )
    return ok(
        {
            "status": status,
            "wallet_label": wallet_label,
            "address": deposit_sender,
            "amount_usdc": amt,
            "effects": effects,
        }
    )


@catch_errors
async def hyperliquid_withdraw(
    *,
    wallet_label: str,
    amount_usdc: float,
) -> dict[str, Any]:
    """Withdraw USDC from Hyperliquid back to Arbitrum.

    The Bridge2 withdrawal fee is $1 USDC, deducted from the amount sent.

    Args:
        wallet_label: Wallet receiving the withdrawal on Arbitrum.
        amount_usdc: USDC to withdraw (must be positive).
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    amt = throw_if_not_number("amount_usdc must be a number", amount_usdc)
    if amt <= 0:
        raise ValueError("amount_usdc must be positive")

    adapter, sender = await _make_hl_adapter(wallet_label)

    effects: list[dict[str, Any]] = []
    ok_wd, res = await adapter.withdraw(amount=amt, address=sender)
    effects.append({"type": "hl", "label": "withdraw", "ok": ok_wd, "result": res})

    if ok_wd:
        ok_landed, withdrawals = await adapter.wait_for_withdrawal(sender)
        effects.append(
            {
                "type": "hl",
                "label": "wait_for_withdrawal",
                "ok": ok_landed,
                "result": withdrawals,
            }
        )

    status = "confirmed" if all(e["ok"] for e in effects) else "failed"
    _annotate_hl_profile(
        address=sender,
        label=wallet_label,
        action="withdraw",
        status=status,
        details={"amount_usdc": amt},
    )
    return ok(
        {
            "status": status,
            "wallet_label": wallet_label,
            "address": sender,
            "amount_usdc": amt,
            "effects": effects,
        }
    )


@catch_errors
async def hyperliquid_update_leverage(
    *,
    wallet_label: str,
    asset_name: str,
    leverage: int,
    is_cross: bool = True,
) -> dict[str, Any]:
    """Set leverage and margin mode for a perp asset.

    Leverage applies per-asset on Hyperliquid — setting it on BTC doesn't touch ETH.

    Args:
        wallet_label: Wallet to update.
        asset_name: Canonical perp identifier (`BTC-USDC`, `xyz:SP500`). Not for spot.
        leverage: Positive integer; HL enforces a per-asset maximum.
        is_cross: True for cross margin (default), False for isolated.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    asset_name = throw_if_empty_str("asset_name is required", asset_name)
    lev = throw_if_not_int("leverage must be an int", leverage)
    if lev <= 0:
        raise ValueError("leverage must be positive")

    try:
        adapter, sender = await _make_hl_adapter(wallet_label)
        resolved_asset_id, _ = await _resolve_asset(adapter, asset_name)
    except ValueError as exc:
        return err("invalid_coin", str(exc))

    effects: list[dict[str, Any]] = []
    ok_lev, res = await adapter.update_leverage(
        resolved_asset_id, lev, bool(is_cross), sender
    )
    effects.append(
        {"type": "hl", "label": "update_leverage", "ok": ok_lev, "result": res}
    )
    status = "confirmed" if ok_lev else "failed"
    _annotate_hl_profile(
        address=sender,
        label=wallet_label,
        action="update_leverage",
        status=status,
        details={
            "asset_id": resolved_asset_id,
            "asset_name": asset_name,
            "leverage": lev,
        },
    )
    return ok(
        {
            "status": status,
            "wallet_label": wallet_label,
            "address": sender,
            "asset_id": resolved_asset_id,
            "asset_name": asset_name,
            "effects": effects,
        }
    )


@catch_errors
async def hyperliquid_cancel_order(
    *,
    wallet_label: str,
    asset_name: str,
    order_id: int | None = None,
    cancel_cloid: str | None = None,
) -> dict[str, Any]:
    """Cancel a resting Hyperliquid order by `order_id` or by `cancel_cloid`.

    Provide exactly one of `order_id` or `cancel_cloid`.

    Args:
        wallet_label: Wallet that owns the order.
        asset_name: Canonical market the order lives on (`BTC-USDC`, `BTC/USDC`, `#40`, …).
        order_id: Numeric on-chain order id.
        cancel_cloid: Client-side order id that was supplied at placement.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    asset_name = throw_if_empty_str("asset_name is required", asset_name)
    if (cancel_cloid is None) == (order_id is None):
        raise ValueError("Provide exactly one of order_id or cancel_cloid")

    try:
        adapter, sender = await _make_hl_adapter(wallet_label)
        resolved_asset_id, _ = await _resolve_asset(adapter, asset_name)
    except ValueError as exc:
        return err("invalid_coin", str(exc))

    effects: list[dict[str, Any]] = []

    if cancel_cloid:
        ok_cancel, res = await adapter.cancel_order_by_cloid(
            resolved_asset_id, str(cancel_cloid), sender
        )
        effects.append(
            {
                "type": "hl",
                "label": "cancel_order_by_cloid",
                "ok": ok_cancel,
                "result": res,
            }
        )
    else:
        ok_cancel, res = await adapter.cancel_order(
            resolved_asset_id, int(order_id), sender
        )
        effects.append(
            {"type": "hl", "label": "cancel_order", "ok": ok_cancel, "result": res}
        )

    ok_all = all(e["ok"] for e in effects)
    status = "confirmed" if ok_all else "failed"
    _annotate_hl_profile(
        address=sender,
        label=wallet_label,
        action="cancel_order",
        status=status,
        details={
            "asset_id": resolved_asset_id,
            "asset_name": asset_name,
            "order_id": order_id,
            "cancel_cloid": cancel_cloid,
        },
    )
    return ok(
        {
            "status": status,
            "wallet_label": wallet_label,
            "address": sender,
            "asset_id": resolved_asset_id,
            "asset_name": asset_name,
            "effects": effects,
        }
    )


@catch_errors
async def hyperliquid_place_trigger_order(
    *,
    wallet_label: str,
    asset_name: str,
    tpsl: Literal["tp", "sl"],
    trigger_price: float,
    is_buy: bool,
    size: float,
    is_market_trigger: bool = True,
    price: float | None = None,
) -> dict[str, Any]:
    """Place a perp take-profit / stop-loss trigger order.

    Set `is_buy` to the side that **closes** your position (long → False, short → True).
    A market trigger fills at market on touch; a limit trigger needs `price`.

    Args:
        wallet_label: Wallet owning the position.
        asset_name: Perp identifier (`BTC-USDC`, `xyz:SP500`). Spot has no trigger surface.
        tpsl: `"tp"` for take-profit, `"sl"` for stop-loss.
        trigger_price: Mark price at which the order activates. Positive.
        is_buy: Direction of the close — opposite of the open position's side.
        size: Asset units to close. Rounded to the asset's lot size; rejects if it
            rounds to zero.
        is_market_trigger: Default True (market on touch). False = limit-on-touch.
        price: Limit price; required only when `is_market_trigger=False`.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    asset_name = throw_if_empty_str("asset_name is required", asset_name)
    if tpsl not in ("tp", "sl"):
        raise ValueError("tpsl must be 'tp' (take-profit) or 'sl' (stop-loss)")
    tpx = throw_if_not_number("trigger_price must be a number", trigger_price)
    if tpx <= 0:
        raise ValueError("trigger_price must be positive")
    sz = throw_if_not_number("size must be a number", size)
    if sz <= 0:
        raise ValueError("size must be positive")

    limit_px: float | None = None
    if not is_market_trigger:
        throw_if_none(
            "price is required for limit trigger orders (is_market_trigger=False)",
            price,
        )
        limit_px = throw_if_not_number("price must be a number", price)
        if limit_px <= 0:
            raise ValueError("price must be positive")

    try:
        adapter, sender = await _make_hl_adapter(wallet_label)
        resolved_asset_id, _ = await _resolve_asset(adapter, asset_name)
    except ValueError as exc:
        return err("invalid_coin", str(exc))

    effects: list[dict[str, Any]] = []
    await _ensure_builder_fee_approval(adapter, sender=sender, effects=effects)

    sz_valid = adapter.get_valid_order_size(resolved_asset_id, sz)
    if sz_valid <= 0:
        sz_decimals = adapter.get_sz_decimals(resolved_asset_id)
        min_tick = float(Decimal(10) ** (-sz_decimals))
        raise ValueError(
            f"size {sz} rounds down to 0 — asset has szDecimals={sz_decimals} "
            f"(lot size = {min_tick}). Try size={min_tick}."
        )

    if limit_px is not None:
        _validate_price(adapter=adapter, asset_id=resolved_asset_id, price=limit_px)
    _validate_price(adapter=adapter, asset_id=resolved_asset_id, price=tpx)

    ok_order, res = await adapter.place_trigger_order(
        resolved_asset_id,
        bool(is_buy),
        tpx,
        float(sz_valid),
        sender,
        tpsl=tpsl,
        is_market=bool(is_market_trigger),
        limit_price=limit_px,
        builder=DEFAULT_HYPERLIQUID_BUILDER_FEE,
    )
    effects.append(
        {
            "type": "hl",
            "label": "place_trigger_order",
            "ok": ok_order,
            "result": res,
        }
    )
    status = "confirmed" if all(e["ok"] for e in effects) else "failed"
    _annotate_hl_profile(
        address=sender,
        label=wallet_label,
        action="place_trigger_order",
        status=status,
        details={
            "asset_id": resolved_asset_id,
            "asset_name": asset_name,
            "tpsl": tpsl,
            "is_buy": bool(is_buy),
            "trigger_price": tpx,
            "size": float(sz_valid),
        },
    )
    return ok(
        {
            "status": status,
            "wallet_label": wallet_label,
            "address": sender,
            "asset_id": resolved_asset_id,
            "asset_name": asset_name,
            "trigger_order": {
                "tpsl": tpsl,
                "is_buy": bool(is_buy),
                "trigger_price": tpx,
                "is_market_trigger": bool(is_market_trigger),
                "limit_price": limit_px,
                "size_requested": float(sz),
                "size_valid": float(sz_valid),
                "builder": DEFAULT_HYPERLIQUID_BUILDER_FEE,
            },
            "effects": effects,
        }
    )


@catch_errors
async def hyperliquid_place_market_order(
    *,
    wallet_label: str,
    asset_name: str,
    is_buy: bool,
    size: float | None = None,
    usd_amount: float | None = None,
    slippage: float = 0.01,
    reduce_only: bool = False,
    cloid: str | None = None,
) -> dict[str, Any]:
    """Place an IOC market order on a Hyperliquid perp / spot / HIP-4 market.

    HIP-4 outcome markets (`#N` asset names) trade as integer contracts with
    no builder fee and no $10 notional floor — `usd_amount` is converted to
    contracts at mid.

    For leverage / margin mode, call `hyperliquid_update_leverage` first.

    Args:
        wallet_label: Wallet placing the order.
        asset_name: Canonical perp/spot/outcome identifier (`BTC-USDC`, `xyz:SP500`, `BTC/USDC`, `#N`).
        is_buy: True to buy, False to sell.
        size: Order size in asset units (or integer contracts for HIP-4).
        usd_amount: USD notional alternative to `size`.
        slippage: Slippage cap as a fraction (default 0.01 = 1%, max 0.25).
        reduce_only: True to close-only (perp). Ignored for spot.
        cloid: Client order id for later cancellation.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    asset_name = throw_if_empty_str("asset_name is required", asset_name)
    throw_if_none("is_buy is required", is_buy)
    slip = throw_if_not_number("slippage must be a number", slippage)
    if slip < 0:
        raise ValueError("slippage must be >= 0")
    if slip > 0.25:
        raise ValueError("slippage > 0.25 is too risky")

    try:
        adapter, sender = await _make_hl_adapter(wallet_label)
        resolved_asset_id, market_type = await _resolve_asset(adapter, asset_name)
    except ValueError as exc:
        return err("invalid_coin", str(exc))

    if market_type == MARKET_TYPE_HIP4:
        return await _place_outcome_order(
            adapter=adapter,
            sender=sender,
            wallet_label=wallet_label,
            asset_name=asset_name,
            is_buy=bool(is_buy),
            order_type="market",
            size=size,
            usd_amount=usd_amount,
            price=None,
            slippage=float(slip),
            reduce_only=bool(reduce_only),
            cloid=cloid,
        )

    effects: list[dict[str, Any]] = []
    await _ensure_builder_fee_approval(adapter, sender=sender, effects=effects)

    sz, sizing, px_for_sizing = await _resolve_perp_or_spot_size(
        adapter=adapter,
        asset_name=asset_name,
        resolved_asset_id=resolved_asset_id,
        market_type=market_type,
        size=size,
        usd_amount=usd_amount,
        px_for_sizing=None,
    )
    sz_valid = adapter.get_valid_order_size(resolved_asset_id, sz)
    _validate_size_and_notional(
        adapter=adapter,
        asset_id=resolved_asset_id,
        size_requested=float(sz),
        sz_valid=sz_valid,
        sizing=sizing,
        px_for_sizing=px_for_sizing,
    )

    ok_order, res = await adapter.place_market_order(
        resolved_asset_id,
        bool(is_buy),
        float(slip),
        float(sz_valid),
        sender,
        reduce_only=bool(reduce_only),
        cloid=cloid,
        builder=DEFAULT_HYPERLIQUID_BUILDER_FEE,
    )
    effects.append(
        {"type": "hl", "label": "place_market_order", "ok": ok_order, "result": res}
    )

    status = "confirmed" if all(e["ok"] for e in effects) else "failed"
    _annotate_hl_profile(
        address=sender,
        label=wallet_label,
        action="place_market_order",
        status=status,
        details={
            "asset_id": resolved_asset_id,
            "asset_name": asset_name,
            "is_buy": bool(is_buy),
            "size": float(sz_valid),
            "slippage": float(slip),
        },
    )
    return ok(
        {
            "status": status,
            "wallet_label": wallet_label,
            "address": sender,
            "asset_id": resolved_asset_id,
            "asset_name": asset_name,
            "order": {
                "order_type": "market",
                "is_buy": bool(is_buy),
                "size_requested": float(sz),
                "size_valid": float(sz_valid),
                "slippage": float(slip),
                "reduce_only": bool(reduce_only),
                "cloid": cloid,
                "builder": DEFAULT_HYPERLIQUID_BUILDER_FEE,
                "sizing": sizing,
            },
            "effects": effects,
        }
    )


@catch_errors
async def hyperliquid_place_limit_order(
    *,
    wallet_label: str,
    asset_name: str,
    is_buy: bool,
    price: float,
    size: float | None = None,
    usd_amount: float | None = None,
    reduce_only: bool = False,
    cloid: str | None = None,
) -> dict[str, Any]:
    """Place a GTC limit order on a Hyperliquid perp / spot / HIP-4 market.

    HIP-4 outcome markets (`#N` asset names) trade as integer contracts with
    no builder fee. `usd_amount` sizing is not supported for limit outcomes —
    pass an integer `size`.

    For leverage / margin mode, call `hyperliquid_update_leverage` first.

    Args:
        wallet_label: Wallet placing the order.
        asset_name: Canonical perp/spot/outcome identifier (`BTC-USDC`, `xyz:SP500`, `BTC/USDC`, `#N`).
        is_buy: True to buy, False to sell.
        price: Limit price (positive).
        size: Order size in asset units (or integer contracts for HIP-4).
        usd_amount: USD notional alternative to `size`; converted to size at `price`. Not supported for HIP-4.
        reduce_only: True to close-only (perp). Ignored for spot.
        cloid: Client order id for later cancellation.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    asset_name = throw_if_empty_str("asset_name is required", asset_name)
    throw_if_none("is_buy is required", is_buy)
    px = throw_if_not_number("price must be a number", price)
    if px <= 0:
        raise ValueError("price must be positive")

    try:
        adapter, sender = await _make_hl_adapter(wallet_label)
        resolved_asset_id, market_type = await _resolve_asset(adapter, asset_name)
    except ValueError as exc:
        return err("invalid_coin", str(exc))

    if market_type == MARKET_TYPE_HIP4:
        return await _place_outcome_order(
            adapter=adapter,
            sender=sender,
            wallet_label=wallet_label,
            asset_name=asset_name,
            is_buy=bool(is_buy),
            order_type="limit",
            size=size,
            usd_amount=None,
            price=float(px),
            slippage=0.0,
            reduce_only=bool(reduce_only),
            cloid=cloid,
        )

    _validate_price(adapter=adapter, asset_id=resolved_asset_id, price=float(px))

    effects: list[dict[str, Any]] = []
    await _ensure_builder_fee_approval(adapter, sender=sender, effects=effects)

    sz, sizing, _ = await _resolve_perp_or_spot_size(
        adapter=adapter,
        asset_name=asset_name,
        resolved_asset_id=resolved_asset_id,
        market_type=market_type,
        size=size,
        usd_amount=usd_amount,
        px_for_sizing=float(px),
    )
    sz_valid = adapter.get_valid_order_size(resolved_asset_id, sz)
    _validate_size_and_notional(
        adapter=adapter,
        asset_id=resolved_asset_id,
        size_requested=float(sz),
        sz_valid=sz_valid,
        sizing=sizing,
        px_for_sizing=float(px),
    )

    ok_order, res = await adapter.place_limit_order(
        resolved_asset_id,
        bool(is_buy),
        float(px),
        float(sz_valid),
        sender,
        reduce_only=bool(reduce_only),
        builder=DEFAULT_HYPERLIQUID_BUILDER_FEE,
    )
    effects.append(
        {"type": "hl", "label": "place_limit_order", "ok": ok_order, "result": res}
    )

    status = "confirmed" if all(e["ok"] for e in effects) else "failed"
    _annotate_hl_profile(
        address=sender,
        label=wallet_label,
        action="place_limit_order",
        status=status,
        details={
            "asset_id": resolved_asset_id,
            "asset_name": asset_name,
            "is_buy": bool(is_buy),
            "price": float(px),
            "size": float(sz_valid),
        },
    )
    return ok(
        {
            "status": status,
            "wallet_label": wallet_label,
            "address": sender,
            "asset_id": resolved_asset_id,
            "asset_name": asset_name,
            "order": {
                "order_type": "limit",
                "is_buy": bool(is_buy),
                "size_requested": float(sz),
                "size_valid": float(sz_valid),
                "price": float(px),
                "reduce_only": bool(reduce_only),
                "cloid": cloid,
                "builder": DEFAULT_HYPERLIQUID_BUILDER_FEE,
                "sizing": sizing,
            },
            "effects": effects,
        }
    )


@catch_errors
async def hyperliquid_get_state(label: str) -> dict[str, Any]:
    """Return perp + spot + outcome state for a Hyperliquid wallet in one shot."""
    addr, _ = await resolve_wallet_address(wallet_label=label)
    if not addr:
        return err("not_found", f"Wallet not found: {label}")

    adapter = HyperliquidAdapter()
    perp_ok, perp = await adapter.get_user_state(addr)
    spot_ok, spot = await adapter.get_spot_user_state(addr)

    spot_balances: list[dict[str, Any]] = []
    outcome_positions: list[dict[str, Any]] = []
    if spot_ok and isinstance(spot, dict):
        for bal in spot.get("balances", []):
            coin = str(bal.get("coin") or "")
            if coin.startswith("+"):
                if float(bal.get("total") or 0) == 0:
                    continue
                encoding = int(coin[1:])
                outcome_positions.append(
                    {
                        "coin": coin,
                        "outcome_id": encoding // 10,
                        "side": encoding % 10,
                        "total": bal.get("total"),
                        "hold": bal.get("hold"),
                        "entryNtl": bal.get("entryNtl"),
                    }
                )
            else:
                spot_balances.append(bal)
        spot["balances"] = spot_balances

    return ok(
        {
            "label": label,
            "address": addr,
            "perp": {"success": perp_ok, "state": perp},
            "spot": {"success": spot_ok, "state": spot},
            "outcomes": {"success": spot_ok, "positions": outcome_positions},
        }
    )


@catch_errors
async def hyperliquid_search_mid_prices(
    asset_names: list[str] | None = None,
) -> dict[str, Any]:
    """
    Search Hyperliquid perpetual, spot, hip3 perpetual and hip4 outcome markets for current mid prices.

    asset_names: Canonical market paths to filter mid prices (e.g. "BTC-USDC", "xyz:NVDA",
        "KNTQ/USDH", "#40"), get these from hyperliquid_search_market(). If omitted, returns every market's mid price. Prefer non empty asset_names for efficiency.
    """
    adapter = HyperliquidAdapter()
    success, prices = await adapter.get_all_mid_prices()
    if not asset_names:
        return ok({"success": success, "prices": prices})

    filtered: dict[str, str] = {}
    for name in asset_names:
        asset_id = await adapter.get_asset_id(name)
        if asset_id is None:
            continue
        for key in adapter.get_mid_price_key(name, asset_id):
            if (mid := prices.get(key)) is not None:
                filtered[name] = mid
                break
    return ok({"prices": filtered})


@catch_errors
async def hyperliquid_search_market(query: str, limit: int = 10) -> dict[str, Any]:
    """
    Search Hyperliquid perpetual, spot, hip3 perpetual and hip4 outcome markets by a simple query string. An empty
    query returns the first `limit` items from each bucket unfiltered.

    query: A simple string containing asset names, for example: btc, eth, oil. Prefer non empty queries for efficiency.
    limit: Max number of results to return per category

    Returns a list of asset names to be used when executing Hyperliquid orders.
    """
    adapter = HyperliquidAdapter()
    (
        (perp_ok, perp_data),
        (spot_ok, spot_data),
        (outcome_ok, outcome_data),
    ) = await asyncio.gather(
        adapter.get_meta_and_asset_ctxs(),
        adapter.get_spot_assets(),
        adapter.get_outcome_markets(),
    )
    if not perp_ok:
        perp_data = {"universe": []}
    if not spot_ok:
        spot_data = []
    if not outcome_ok:
        outcome_data = []

    # HIP-3 builder dexes carry a `<dex>:<base>` prefix; core perps don't have
    # a quote suffix, so tack on `-USDC` to render the canonical coin path.
    perps = [
        name if ":" in (name := entry["name"]) else f"{name}-USDC"
        for entry in perp_data[0]["universe"]
    ]
    spots = list(spot_data)

    if not query.strip():
        perp_hits = [{"name": p} for p in perps[:limit]]
        spot_hits = [{"name": s} for s in spots[:limit]]
        outcome_hits = outcome_data[:limit]
    else:
        terms = {
            a
            for token in query.lower().split()
            for a in MARKET_SEARCH_ALIASES.get(token, {token})
        }

        def score(text: str) -> float:
            # matches / min(len_a, len_b) — rewards covering the shorter string
            # fully. HL token symbols are short and often vowel-stripped (KNTQ
            # for kinetiq, kBONK for bonk), so subsequence-style matching is the
            # natural fit. We prefer false positives over false negatives:
            # missed matches are invisible to the LLM consumer, while noise
            # candidates can be ranked-out downstream.
            candidate_tokens = [c for c in re.split(r"[^a-z0-9]+", text.lower()) if c]
            best = 0.0
            for term in terms:
                for ct in candidate_tokens:
                    sm = difflib.SequenceMatcher(None, term, ct)
                    matches = sum(b.size for b in sm.get_matching_blocks())
                    denom = min(len(term), len(ct))
                    if denom:
                        best = max(best, matches / denom)
            return best

        def top(items, text_of):
            scored = ((item, score(text_of(item))) for item in items)
            kept = sorted(
                ((it, s) for it, s in scored if s >= MARKET_SEARCH_MIN_MATCH_SCORE),
                key=lambda r: r[1],
                reverse=True,
            )
            return [it for it, _ in kept[:limit]]

        def outcome_text(market: dict[str, Any]) -> str:
            sides = (
                market["sides"]
                if market["class"] == "priceBinary"
                else [s for o in market["outcomes"] for s in o["sides"]]
            )
            text = " ".join(side["description"] for side in sides)
            # Side descriptions use math operators (>=, <, <=); the candidate
            # tokenizer strips non-alphanumerics so those would be invisible
            # to MARKET_SEARCH_ALIASES. Rewrite to natural-language words so
            # queries like "btc above 80k" / "below 78k" / "between" land.
            return (
                text.replace(">=", " above ")
                .replace("<=", " below ")
                .replace(">", " above ")
                .replace("<", " below ")
            )

        perp_hits = [{"name": p} for p in top(perps, lambda p: p)][:limit]
        spot_hits = [{"name": s} for s in top(spots, lambda s: s)][:limit]
        outcome_hits = top(outcome_data, outcome_text)[:limit]

    return ok(
        {
            "perps": perp_hits,
            "spots": spot_hits,
            "outcomes": outcome_hits,
        }
    )
