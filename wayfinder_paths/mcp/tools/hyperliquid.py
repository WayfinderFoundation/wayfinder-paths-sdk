from __future__ import annotations

import asyncio
import difflib
import re
from typing import Any, Literal

from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter
from wayfinder_paths.adapters.hyperliquid_adapter.adapter import decode_outcome_encoding
from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.constants.hyperliquid import (
    ARBITRUM_USDC_ADDRESS,
    DEFAULT_HYPERLIQUID_BUILDER_FEE_TENTHS_BP,
    HYPE_FEE_WALLET,
    HYPERLIQUID_BRIDGE_ADDRESS,
    MARKET_SEARCH_ALIASES,
    MARKET_SEARCH_MIN_MATCH_SCORE,
    MIN_ORDER_USD_NOTIONAL,
)
from wayfinder_paths.core.utils.tokens import build_send_transaction
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.core.utils.wallets import get_wallet_signing_callback
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
    throw_if_not_number,
)


def _resolve_builder_fee(
    *,
    config: dict[str, Any],
    builder_fee_tenths_bp: int | None,
) -> dict[str, Any]:
    """
    Resolve builder fee config for Hyperliquid orders.

    Builder attribution is **mandatory** and always uses the Wayfinder builder wallet.
    Fee priority:
      1) explicit builder_fee_tenths_bp
      2) config["builder_fee"]["f"] (typically config.json["strategy"]["builder_fee"])
      3) DEFAULT_HYPERLIQUID_BUILDER_FEE_TENTHS_BP
    """
    expected_builder = HYPE_FEE_WALLET.lower()
    fee = builder_fee_tenths_bp
    if fee is None:
        cfg = config.get("builder_fee") if isinstance(config, dict) else None
        if isinstance(cfg, dict):
            cfg_builder = str(cfg.get("b") or "").strip()
            if cfg_builder and cfg_builder.lower() != expected_builder:
                raise ValueError(
                    f"config builder_fee.b must be {expected_builder} (got {cfg_builder})"
                )
            if cfg.get("f") is not None:
                fee = cfg.get("f")
    if fee is None:
        fee = DEFAULT_HYPERLIQUID_BUILDER_FEE_TENTHS_BP

    try:
        fee_i = int(fee)
    except (TypeError, ValueError) as exc:
        raise ValueError("builder_fee_tenths_bp must be an int") from exc
    if fee_i <= 0:
        raise ValueError("builder_fee_tenths_bp must be > 0")

    return {"b": expected_builder, "f": fee_i}


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
        tool="hyperliquid_execute",
        status=status,
        chain_id=999,  # Hyperliquid chain ID
        details=details,
    )


@catch_errors
async def hyperliquid_execute(
    action: Literal[
        "place_order",
        "place_trigger_order",
        "cancel_order",
        "update_leverage",
        "deposit",
        "withdraw",
        "spot_to_perp_transfer",
        "perp_to_spot_transfer",
    ],
    *,
    wallet_label: str,
    asset_name: str | None = None,
    order_type: Literal["market", "limit"] = "market",
    is_buy: bool | None = None,
    size: float | None = None,
    usd_amount: float | None = None,
    usd_amount_kind: Literal["notional", "margin"] | None = None,
    price: float | None = None,
    slippage: float = 0.01,
    reduce_only: bool = False,
    cloid: str | None = None,
    order_id: int | None = None,
    cancel_cloid: str | None = None,
    leverage: int | None = None,
    is_cross: bool = True,
    amount_usdc: float | None = None,
    builder_fee_tenths_bp: int | None = None,
    # place_trigger_order params
    trigger_price: float | None = None,
    tpsl: Literal["tp", "sl"] | None = None,
    is_market_trigger: bool = True,
) -> dict[str, Any]:
    """Place orders, transfer collateral, or adjust leverage on Hyperliquid.

    `asset_name` is the canonical market path returned by `hyperliquid_search_market`:
      * Core perp:   `"BTC-USDC"`, `"ETH-USDC"`
      * HIP-3 perp:  `"xyz:SP500"`
      * Spot pair:   `"BTC/USDC"`, `"USDC/USDH"`
      * HIP-4 outcome: `"#0"`, `"#1"`, `"#40"` (encoding = `outcome_id*10 + side`)

    Builder attribution is mandatory — every order routes through the Wayfinder builder wallet
    and the tool auto-approves the builder fee on first use.

    Actions:
      - `place_order`: spot / perp / HIP-4 outcome market or limit.
        Size via `size` (asset units) or `usd_amount` (with `usd_amount_kind="notional"|"margin"` for perps).
      - `place_trigger_order`: TP/SL trigger. `tpsl="tp"|"sl"`, `trigger_price`, `is_buy` set to
        the side that closes the position (long → False, short → True).
      - `cancel_order`: by `order_id` or `cancel_cloid`.
      - `update_leverage`: set `leverage` and `is_cross` for an asset.
      - `deposit`: bridge `amount_usdc` from Arbitrum USDC into the HL perp account
        (≥ 5 USDC; below is lost). Auto-waits for the perp clearinghouse credit before returning.
      - `withdraw`: bridge `amount_usdc` from perp account back to Arbitrum.
      - `spot_to_perp_transfer` / `perp_to_spot_transfer`: shift `usd_amount` between sub-accounts.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)

    strategy_raw = CONFIG.get("strategy")
    strategy_cfg = strategy_raw if isinstance(strategy_raw, dict) else {}
    config: dict[str, Any] = dict(strategy_cfg)

    effects: list[dict[str, Any]] = []

    try:
        adapter = await get_adapter(
            HyperliquidAdapter, wallet_label, config_overrides=config
        )
    except ValueError as e:
        return err("invalid_wallet", str(e))
    sender = adapter.wallet_address

    match action:
        case "deposit":
            throw_if_none("amount_usdc is required for deposit", amount_usdc)
            amt = throw_if_not_number("amount_usdc must be a number", amount_usdc)
            if amt < 5:
                raise ValueError(
                    "amount_usdc must be >= 5 USDC (HL deposits below are lost)"
                )

            try:
                sign_callback, deposit_sender = await get_wallet_signing_callback(
                    wallet_label
                )
            except ValueError as exc:
                return err("invalid_wallet", str(exc))

            transaction = await build_send_transaction(
                from_address=deposit_sender,
                to_address=HYPERLIQUID_BRIDGE_ADDRESS,
                token_address=ARBITRUM_USDC_ADDRESS,
                chain_id=42161,
                amount=int(parse_amount_to_raw(str(amt), 6)),
            )
            try:
                tx_hash = await send_transaction(
                    transaction, sign_callback, wait_for_receipt=True
                )
                sent_ok = True
                sent_result: dict[str, Any] = {
                    "txn_hash": tx_hash,
                    "chain_id": 42161,
                }
            except Exception as exc:  # noqa: BLE001
                sent_ok = False
                sent_result = {"error": str(exc), "chain_id": 42161}
            effects.append(
                {"type": "hl", "label": "deposit", "ok": sent_ok, "result": sent_result}
            )

            if sent_ok:
                ok_landed, final_balance = await adapter.wait_for_deposit(
                    deposit_sender, amt
                )
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
            response = ok(
                {
                    "status": status,
                    "action": action,
                    "wallet_label": wallet_label,
                    "address": deposit_sender,
                    "amount_usdc": amt,
                    "effects": effects,
                }
            )
            _annotate_hl_profile(
                address=deposit_sender,
                label=wallet_label,
                action="deposit",
                status=status,
                details={"amount_usdc": amt, "chain_id": 42161},
            )
            return response

        case "withdraw":
            throw_if_none("amount_usdc is required for withdraw", amount_usdc)
            amt = throw_if_not_number("amount_usdc must be a number", amount_usdc)
            if amt <= 0:
                raise ValueError("amount_usdc must be positive")

            ok_wd, res = await adapter.withdraw(amount=amt, address=sender)
            effects.append(
                {"type": "hl", "label": "withdraw", "ok": ok_wd, "result": res}
            )

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
            response = ok(
                {
                    "status": status,
                    "action": action,
                    "wallet_label": wallet_label,
                    "address": sender,
                    "amount_usdc": amt,
                    "effects": effects,
                }
            )
            _annotate_hl_profile(
                address=sender,
                label=wallet_label,
                action="withdraw",
                status=status,
                details={"amount_usdc": amt},
            )

            return response

        case "spot_to_perp_transfer" | "perp_to_spot_transfer":
            throw_if_none(f"usd_amount is required for {action}", usd_amount)
            amt = throw_if_not_number("usd_amount must be a number", usd_amount)
            if amt <= 0:
                raise ValueError("usd_amount must be positive")

            to_perp = action == "spot_to_perp_transfer"
            if to_perp:
                ok_transfer, res = await adapter.transfer_spot_to_perp(
                    amount=amt, address=sender
                )
            else:
                ok_transfer, res = await adapter.transfer_perp_to_spot(
                    amount=amt, address=sender
                )
            effects.append(
                {"type": "hl", "label": action, "ok": ok_transfer, "result": res}
            )
            status = "confirmed" if ok_transfer else "failed"
            response = ok(
                {
                    "status": status,
                    "action": action,
                    "wallet_label": wallet_label,
                    "address": sender,
                    "usd_amount": amt,
                    "to_perp": to_perp,
                    "effects": effects,
                }
            )
            _annotate_hl_profile(
                address=sender,
                label=wallet_label,
                action=action,
                status=status,
                details={"usd_amount": amt, "to_perp": to_perp},
            )

            return response

    asset_name = throw_if_empty_str("asset_name is required", asset_name)
    resolved_asset_id = await adapter.get_asset_id(asset_name)
    if resolved_asset_id is None:
        return err(
            "invalid_coin",
            f"Invalid asset_name {asset_name!r}. Expected 'BTC-USDC' (core perp), "
            "'xyz:SP500' (HIP-3 perp), 'BTC/USDC' (spot), or '#40' (HIP-4 outcome). "
            "Call hyperliquid_search_market to look up the canonical name.",
        )
    market_type = adapter.get_market_type(asset_name)

    # HIP-4 outcome orders use a dedicated execution path (not perp/spot wire).
    match action:
        case "place_order" if market_type == "hip4":
            outcome_id_v, side_v = decode_outcome_encoding(int(asset_name[1:]))
            throw_if_none("is_buy is required for outcome orders", is_buy)
            if order_type == "limit" and price is None:
                raise ValueError("price is required for limit orders")

            # Outcomes are integer contracts (szDecimals=0) with no $10 floor;
            # accept either explicit `size` or `usd_amount` for market orders.
            size_i: int | None = None if size is None else int(size)
            if size_i is None:
                if usd_amount is None:
                    raise ValueError(
                        "size or usd_amount is required for outcome orders"
                    )
                if order_type != "market":
                    raise ValueError(
                        "usd_amount sizing is only supported for market outcome orders"
                    )
                ok_mids, mids = await adapter.get_all_mid_prices()
                if not ok_mids or not isinstance(mids, dict):
                    return err("price_error", "Failed to fetch mid prices")
                mid = mids.get(asset_name)
                if mid is None or float(mid) <= 0:
                    return err(
                        "price_error",
                        f"Could not resolve mid price for {asset_name}",
                    )
                size_i = max(1, round(float(usd_amount) / float(mid)))

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
                {"type": "hl", "label": "place_order", "ok": ok_order, "result": res}
            )
            status = "confirmed" if ok_order else "failed"
            _annotate_hl_profile(
                address=sender,
                label=wallet_label,
                action="place_order",
                status=status,
                details={
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
                    "action": action,
                    "wallet_label": wallet_label,
                    "address": sender,
                    "asset_name": asset_name,
                    "outcome_id": outcome_id_v,
                    "side": side_v,
                    "order": {
                        "order_type": order_type,
                        "is_buy": bool(is_buy),
                        "size": size_i,
                        "price": float(price) if price is not None else None,
                        "slippage": float(slippage),
                        "reduce_only": bool(reduce_only),
                        "cloid": cloid,
                    },
                    "effects": effects,
                }
            )

        case "update_leverage":
            throw_if_none("leverage is required for update_leverage", leverage)
            lev = int(throw_if_not_number("leverage must be an int", leverage))
            if lev <= 0:
                raise ValueError("leverage must be positive")

            ok_lev, res = await adapter.update_leverage(
                resolved_asset_id, lev, bool(is_cross), sender
            )
            effects.append(
                {"type": "hl", "label": "update_leverage", "ok": ok_lev, "result": res}
            )
            status = "confirmed" if ok_lev else "failed"
            response = ok(
                {
                    "status": status,
                    "action": action,
                    "wallet_label": wallet_label,
                    "address": sender,
                    "asset_id": resolved_asset_id,
                    "asset_name": asset_name,
                    "effects": effects,
                }
            )
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

            return response

        case "cancel_order":
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
                if order_id is None:
                    raise ValueError(
                        "order_id or cancel_cloid is required for cancel_order"
                    )
                ok_cancel, res = await adapter.cancel_order(
                    resolved_asset_id, int(order_id), sender
                )
                effects.append(
                    {
                        "type": "hl",
                        "label": "cancel_order",
                        "ok": ok_cancel,
                        "result": res,
                    }
                )

            ok_all = all(bool(e.get("ok")) for e in effects) if effects else False
            status = "confirmed" if ok_all else "failed"
            response = ok(
                {
                    "status": status,
                    "action": action,
                    "wallet_label": wallet_label,
                    "address": sender,
                    "asset_id": resolved_asset_id,
                    "asset_name": asset_name,
                    "effects": effects,
                }
            )
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

            return response

        case "place_trigger_order":
            if tpsl not in ("tp", "sl"):
                raise ValueError("tpsl must be 'tp' (take-profit) or 'sl' (stop-loss)")
            throw_if_none(
                "trigger_price is required for place_trigger_order", trigger_price
            )
            tpx = throw_if_not_number("trigger_price must be a number", trigger_price)
            if tpx <= 0:
                raise ValueError("trigger_price must be positive")
            if is_buy is None:
                raise ValueError(
                    "is_buy is required for place_trigger_order — set to opposite of your position "
                    "(long position → is_buy=False to sell; short position → is_buy=True to buy back)"
                )
            throw_if_none(
                "size is required for place_trigger_order (asset units)", size
            )
            sz = throw_if_not_number("size must be a number", size)
            if sz <= 0:
                raise ValueError("size must be positive")

            limit_px: float | None = None
            if not is_market_trigger:
                if price is None:
                    raise ValueError(
                        "price is required for limit trigger orders (is_market_trigger=False)"
                    )
                limit_px = throw_if_not_number("price must be a number", price)
                if limit_px <= 0:
                    raise ValueError("price must be positive")

            builder = _resolve_builder_fee(
                config=config, builder_fee_tenths_bp=builder_fee_tenths_bp
            )

            sz_valid = adapter.get_valid_order_size(resolved_asset_id, sz)
            if sz_valid <= 0:
                raise ValueError("size is too small after lot-size rounding")

            ok_order, res = await adapter.place_trigger_order(
                resolved_asset_id,
                bool(is_buy),
                tpx,
                float(sz_valid),
                sender,
                tpsl=tpsl,
                is_market=bool(is_market_trigger),
                limit_price=limit_px,
                builder=builder,
            )
            effects.append(
                {
                    "type": "hl",
                    "label": "place_trigger_order",
                    "ok": ok_order,
                    "result": res,
                }
            )

            ok_all = all(bool(e.get("ok")) for e in effects) if effects else False
            status = "confirmed" if ok_all else "failed"
            response = ok(
                {
                    "status": status,
                    "action": action,
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
                        "builder": builder,
                    },
                    "effects": effects,
                }
            )
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
            return response

        case "place_order":
            if size is not None and usd_amount is not None:
                raise ValueError(
                    "Provide either size (asset units) or usd_amount (USD notional/margin), not both"
                )
            if usd_amount_kind is not None and usd_amount is None:
                raise ValueError(
                    "usd_amount_kind is only valid when usd_amount is provided"
                )

            throw_if_none("is_buy is required for place_order", is_buy)

            if order_type == "limit":
                throw_if_none("price is required for limit orders", price)
                px_for_sizing = throw_if_not_number("price must be a number", price)
                if px_for_sizing <= 0:
                    raise ValueError("price must be positive")
            else:
                slip = throw_if_not_number("slippage must be a number", slippage)
                if slip < 0:
                    raise ValueError("slippage must be >= 0")
                if slip > 0.25:
                    raise ValueError("slippage > 0.25 is too risky")
                px_for_sizing = None

            sizing: dict[str, Any] = {"source": "size"}
            if size is not None:
                sz = throw_if_not_number("size must be a number", size)
                if sz <= 0:
                    raise ValueError("size must be positive")
            else:
                if usd_amount is None:
                    raise ValueError(
                        "Provide either size (asset units) or usd_amount for place_order"
                    )
                usd_amt = throw_if_not_number("usd_amount must be a number", usd_amount)
                if usd_amt <= 0:
                    raise ValueError("usd_amount must be positive")

                # Spot: usd_amount is always notional (no leverage)
                if market_type == "spot":
                    notional_usd = usd_amt
                    margin_usd = None
                elif usd_amount_kind is None:
                    raise ValueError(
                        "usd_amount_kind is required for perp: 'notional' or 'margin'"
                    )
                elif usd_amount_kind == "margin":
                    if leverage is None:
                        raise ValueError(
                            "leverage is required when usd_amount_kind='margin'"
                        )
                    lev = int(throw_if_not_number("leverage must be an int", leverage))
                    if lev <= 0:
                        raise ValueError("leverage must be positive")
                    notional_usd = usd_amt * float(lev)
                    margin_usd = usd_amt
                else:
                    notional_usd = usd_amt
                    margin_usd = None
                    if leverage is not None:
                        try:
                            lev = int(leverage)
                            if lev > 0:
                                margin_usd = notional_usd / float(lev)
                        except Exception:
                            margin_usd = None

                if px_for_sizing is None:
                    ok_mids, mids = await adapter.get_all_mid_prices()
                    if not ok_mids or not isinstance(mids, dict):
                        response = err("price_error", "Failed to fetch mid prices")
                        return response
                    mid = None
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
                        response = err(
                            "price_error",
                            f"Could not resolve mid price for {asset_name}",
                        )
                        return response
                    px_for_sizing = mid

                sz = notional_usd / float(px_for_sizing)
                sizing = {
                    "source": "usd_amount",
                    "usd_amount": float(usd_amt),
                    "usd_amount_kind": usd_amount_kind,
                    "notional_usd": float(notional_usd),
                    "margin_usd_estimate": float(margin_usd)
                    if margin_usd is not None
                    else None,
                    "price_used": float(px_for_sizing),
                }

            sz_valid = adapter.get_valid_order_size(resolved_asset_id, sz)
            if sz_valid <= 0:
                raise ValueError("size is too small after lot-size rounding")

            # HL rejects spot/perp orders below $10 notional. Lot-size rounding
            # of `usd_amount`-derived sizes can dip just under that floor (e.g.
            # usd_amount=10.20 / price=0.18 → sz=56.66 → 56.66 × 0.18 = 10.1988).
            # Surface the actual notional so the caller can bump usd_amount.
            if sizing["source"] == "usd_amount" and px_for_sizing is not None:
                final_notional = float(sz_valid) * float(px_for_sizing)
                if final_notional < MIN_ORDER_USD_NOTIONAL:
                    raise ValueError(
                        f"After lot-size rounding, notional is ${final_notional:.4f} — HL "
                        f"requires >= ${MIN_ORDER_USD_NOTIONAL:.2f}. Bump usd_amount or pass size directly."
                    )

            builder = _resolve_builder_fee(
                config=config,
                builder_fee_tenths_bp=builder_fee_tenths_bp,
            )

            if leverage is not None:
                lev = int(throw_if_not_number("leverage must be an int", leverage))
                if lev <= 0:
                    raise ValueError("leverage must be positive")
                ok_lev, res = await adapter.update_leverage(
                    resolved_asset_id, lev, bool(is_cross), sender
                )
                effects.append(
                    {
                        "type": "hl",
                        "label": "update_leverage",
                        "ok": ok_lev,
                        "result": res,
                    }
                )
                if not ok_lev:
                    response = ok(
                        {
                            "status": "failed",
                            "action": action,
                            "wallet_label": wallet_label,
                            "address": sender,
                            "asset_id": resolved_asset_id,
                            "asset_name": asset_name,
                            "effects": effects,
                        }
                    )
                    return response

            # Builder attribution is mandatory; ensure approval before placing orders.
            desired = int(builder.get("f") or 0)
            builder_addr = str(builder.get("b") or "").strip()
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
            if not ok_fee or int(current) < desired:
                max_fee_rate = f"{desired / 1000:.3f}%"
                ok_appr, appr = await adapter.approve_builder_fee(
                    builder=builder_addr,
                    max_fee_rate=max_fee_rate,
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
                    response = ok(
                        {
                            "status": "failed",
                            "action": action,
                            "wallet_label": wallet_label,
                            "address": sender,
                            "asset_id": resolved_asset_id,
                            "asset_name": asset_name,
                            "effects": effects,
                        }
                    )
                    return response

            if order_type == "limit":
                ok_order, res = await adapter.place_limit_order(
                    resolved_asset_id,
                    bool(is_buy),
                    float(price),
                    float(sz_valid),
                    sender,
                    reduce_only=bool(reduce_only),
                    builder=builder,
                )
                effects.append(
                    {
                        "type": "hl",
                        "label": "place_limit_order",
                        "ok": ok_order,
                        "result": res,
                    }
                )
            else:
                ok_order, res = await adapter.place_market_order(
                    resolved_asset_id,
                    bool(is_buy),
                    float(slippage),
                    float(sz_valid),
                    sender,
                    reduce_only=bool(reduce_only),
                    cloid=cloid,
                    builder=builder,
                )
                effects.append(
                    {
                        "type": "hl",
                        "label": "place_market_order",
                        "ok": ok_order,
                        "result": res,
                    }
                )

            ok_all = all(bool(e.get("ok")) for e in effects) if effects else False
            status = "confirmed" if ok_all else "failed"
            response = ok(
                {
                    "status": status,
                    "action": action,
                    "wallet_label": wallet_label,
                    "address": sender,
                    "asset_id": resolved_asset_id,
                    "asset_name": asset_name,
                    "order": {
                        "order_type": order_type,
                        "is_buy": bool(is_buy),
                        "size_requested": float(sz),
                        "size_valid": float(sz_valid),
                        "price": float(price) if price is not None else None,
                        "slippage": float(slippage),
                        "reduce_only": bool(reduce_only),
                        "cloid": cloid,
                        "builder": builder,
                        "sizing": sizing,
                    },
                    "effects": effects,
                }
            )
            _annotate_hl_profile(
                address=sender,
                label=wallet_label,
                action="place_order",
                status=status,
                details={
                    "asset_id": resolved_asset_id,
                    "asset_name": asset_name,
                    "order_type": order_type,
                    "is_buy": bool(is_buy),
                    "size": float(sz_valid),
                },
            )

            return response

        case _:
            return err("invalid_request", f"Unknown action: {action}")


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
    outcome_sides = [
        (s["book_coin"], market["description"])
        for market in outcome_data
        for s in market["sides"]
    ]

    if not query.strip():
        perp_hits = [{"name": p} for p in perps[:limit]]
        spot_hits = [{"name": s} for s in spots[:limit]]
        outcome_hits = [
            {"name": coin, "description": desc} for coin, desc in outcome_sides[:limit]
        ]
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

        perp_hits = [{"name": p} for p in top(perps, lambda p: p)]
        spot_hits = [{"name": s} for s in top(spots, lambda s: s)]
        outcome_hits = [
            {"name": coin, "description": desc}
            for coin, desc in top(outcome_sides, lambda row: row[1])
        ]

    return ok(
        {
            "perps": perp_hits,
            "spots": spot_hits,
            "outcomes": outcome_hits,
        }
    )
