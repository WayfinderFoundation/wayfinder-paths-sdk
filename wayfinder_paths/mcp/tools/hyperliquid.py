from __future__ import annotations

import re
from typing import Any, Literal

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.constants.hyperliquid import (
    DEFAULT_HYPERLIQUID_BUILDER_FEE_TENTHS_BP,
    HYPE_FEE_WALLET,
)
from wayfinder_paths.mcp.preview import build_hyperliquid_execute_preview
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    err,
    find_wallet_by_label,
    normalize_address,
    ok,
    resolve_wallet_address,
)

_PERP_SUFFIX_RE = re.compile(r"[-_ ]?perp$", re.IGNORECASE)


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


def _resolve_perp_asset_id(
    adapter: HyperliquidAdapter, *, coin: str | None, asset_id: int | None
) -> tuple[bool, int | dict[str, Any]]:
    if asset_id is not None:
        try:
            return True, int(asset_id)
        except (TypeError, ValueError):
            return False, {"code": "invalid_request", "message": "asset_id must be int"}

    c = (coin or "").strip()
    if not c:
        return False, {
            "code": "invalid_request",
            "message": "coin or asset_id is required",
        }

    c = _PERP_SUFFIX_RE.sub("", c).strip()
    if not c:
        return False, {"code": "invalid_request", "message": "coin is required"}

    mapping = adapter.coin_to_asset or {}
    lower = {str(k).lower(): int(v) for k, v in mapping.items()}
    aid = lower.get(c.lower())
    if aid is None:
        return (
            False,
            {
                "code": "not_found",
                "message": f"Unknown perp coin: {c}",
                "details": {"coin": c},
            },
        )
    return True, aid


async def _resolve_spot_asset_id(
    adapter: HyperliquidAdapter, *, coin: str | None
) -> tuple[bool, int | dict[str, Any]]:
    c = _PERP_SUFFIX_RE.sub("", (coin or "").strip()).strip().upper()
    if not c:
        return False, {
            "code": "invalid_request",
            "message": "coin is required for spot orders",
        }

    # get_spot_assets populates cache, then we look up
    ok, assets = await adapter.get_spot_assets()
    if not ok:
        return False, {"code": "error", "message": "Failed to fetch spot assets"}

    pair_name = f"{c}/USDC"
    spot_aid = assets.get(pair_name)
    if spot_aid is None:
        return False, {
            "code": "not_found",
            "message": f"Unknown spot pair: {pair_name}",
        }
    return True, spot_aid


async def hyperliquid(
    action: Literal["wait_for_deposit", "wait_for_withdrawal"],
    *,
    wallet_label: str | None = None,
    wallet_address: str | None = None,
    expected_increase: float | None = None,
    timeout_s: int = 120,
    poll_interval_s: int = 5,
    lookback_s: int = 5,
    max_poll_time_s: int = 15 * 60,
) -> dict[str, Any]:
    adapter = HyperliquidAdapter()

    addr, _ = resolve_wallet_address(
        wallet_label=wallet_label, wallet_address=wallet_address
    )
    if not addr:
        return err(
            "invalid_request",
            "wallet_label or wallet_address is required",
            {"wallet_label": wallet_label, "wallet_address": wallet_address},
        )

    if action == "wait_for_deposit":
        if expected_increase is None:
            return err(
                "invalid_request",
                "expected_increase is required for wait_for_deposit",
                {"expected_increase": expected_increase},
            )
        try:
            inc = float(expected_increase)
        except (TypeError, ValueError):
            return err("invalid_request", "expected_increase must be a number")
        if inc <= 0:
            return err("invalid_request", "expected_increase must be positive")

        ok_dep, final_bal = await adapter.wait_for_deposit(
            addr,
            inc,
            timeout_s=int(timeout_s),
            poll_interval_s=int(poll_interval_s),
        )
        return ok(
            {
                "wallet_address": addr,
                "action": action,
                "expected_increase": inc,
                "confirmed": bool(ok_dep),
                "final_balance_usd": float(final_bal),
                "timeout_s": int(timeout_s),
                "poll_interval_s": int(poll_interval_s),
            }
        )

    if action == "wait_for_withdrawal":
        ok_wd, withdrawals = await adapter.wait_for_withdrawal(
            addr,
            lookback_s=int(lookback_s),
            max_poll_time_s=int(max_poll_time_s),
            poll_interval_s=int(poll_interval_s),
        )
        return ok(
            {
                "wallet_address": addr,
                "action": action,
                "confirmed": bool(ok_wd),
                "withdrawals": withdrawals,
                "lookback_s": int(lookback_s),
                "max_poll_time_s": int(max_poll_time_s),
                "poll_interval_s": int(poll_interval_s),
            }
        )

    return err("invalid_request", f"Unknown hyperliquid action: {action}")


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


async def hyperliquid_execute(
    action: Literal[
        "place_order",
        "cancel_order",
        "update_leverage",
        "withdraw",
        "spot_to_perp_transfer",
        "perp_to_spot_transfer",
    ],
    *,
    wallet_label: str,
    coin: str | None = None,
    asset_id: int | None = None,
    is_spot: bool | None = None,
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
    force: bool = False,
) -> dict[str, Any]:
    want = str(wallet_label or "").strip()
    if not want:
        return err("invalid_request", "wallet_label is required")

    key_input = {
        "action": action,
        "wallet_label": want,
        "coin": coin,
        "asset_id": asset_id,
        "is_spot": is_spot,
        "order_type": order_type,
        "is_buy": is_buy,
        "size": size,
        "usd_amount": usd_amount,
        "usd_amount_kind": usd_amount_kind,
        "price": price,
        "slippage": slippage,
        "reduce_only": reduce_only,
        "cloid": cloid,
        "order_id": order_id,
        "cancel_cloid": cancel_cloid,
        "leverage": leverage,
        "is_cross": is_cross,
        "amount_usdc": amount_usdc,
        "builder_fee_tenths_bp": builder_fee_tenths_bp,
    }
    tool_input = {"request": key_input}
    preview_obj = build_hyperliquid_execute_preview(tool_input)
    preview_text = str(preview_obj.get("summary") or "").strip()

    w = find_wallet_by_label(want)
    if not w:
        return err("not_found", f"Unknown wallet_label: {want}")

    sender = normalize_address(w.get("address"))
    pk = (
        (w.get("private_key") or w.get("private_key_hex"))
        if isinstance(w, dict)
        else None
    )
    if not sender or not pk:
        response = err(
            "invalid_wallet",
            "Wallet must include address and private_key_hex in config.json (local dev only)",
            {"wallet_label": want},
        )
        return response

    strategy_raw = CONFIG.get("strategy")
    strategy_cfg = strategy_raw if isinstance(strategy_raw, dict) else {}
    config: dict[str, Any] = dict(strategy_cfg)
    config["main_wallet"] = {"address": sender, "private_key_hex": pk}
    config["strategy_wallet"] = {"address": sender, "private_key_hex": pk}

    effects: list[dict[str, Any]] = []

    adapter = HyperliquidAdapter(config=config)

    if action == "withdraw":
        if amount_usdc is None:
            response = err("invalid_request", "amount_usdc is required for withdraw")
            return response
        try:
            amt = float(amount_usdc)
        except (TypeError, ValueError):
            response = err("invalid_request", "amount_usdc must be a number")
            return response
        if amt <= 0:
            response = err("invalid_request", "amount_usdc must be positive")
            return response

        ok_wd, res = await adapter.withdraw(amount=amt, address=sender)
        effects.append({"type": "hl", "label": "withdraw", "ok": ok_wd, "result": res})
        status = "confirmed" if ok_wd else "failed"
        response = ok(
            {
                "status": status,
                "action": action,
                "wallet_label": want,
                "address": sender,
                "amount_usdc": amt,
                "preview": preview_text,
                "effects": effects,
            }
        )
        _annotate_hl_profile(
            address=sender,
            label=want,
            action="withdraw",
            status=status,
            details={"amount_usdc": amt},
        )

        return response

    if action in ("spot_to_perp_transfer", "perp_to_spot_transfer"):
        if usd_amount is None:
            return err("invalid_request", f"usd_amount is required for {action}")
        try:
            amt = float(usd_amount)
        except (TypeError, ValueError):
            return err("invalid_request", "usd_amount must be a number")
        if amt <= 0:
            return err("invalid_request", "usd_amount must be positive")

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
                "wallet_label": want,
                "address": sender,
                "usd_amount": amt,
                "to_perp": to_perp,
                "preview": preview_text,
                "effects": effects,
            }
        )
        _annotate_hl_profile(
            address=sender,
            label=want,
            action=action,
            status=status,
            details={"usd_amount": amt, "to_perp": to_perp},
        )

        return response

    def _coin_from_asset_id(aid: int) -> str | None:
        for k, v in (adapter.coin_to_asset or {}).items():
            try:
                if v == aid:
                    return str(k)
            except Exception:
                continue
        return None

    if is_spot:
        ok_aid, aid_or_err = await _resolve_spot_asset_id(adapter, coin=coin)
    else:
        ok_aid, aid_or_err = _resolve_perp_asset_id(
            adapter, coin=coin, asset_id=asset_id
        )
    if not ok_aid:
        payload = aid_or_err if isinstance(aid_or_err, dict) else {}
        response = err(
            payload.get("code") or "invalid_request",
            payload.get("message") or "Invalid asset",
            payload.get("details"),
        )
        return response
    resolved_asset_id = int(aid_or_err)

    if action == "update_leverage":
        if leverage is None:
            response = err(
                "invalid_request", "leverage is required for update_leverage"
            )
            return response
        try:
            lev = int(leverage)
        except (TypeError, ValueError):
            response = err("invalid_request", "leverage must be an int")
            return response
        if lev <= 0:
            response = err("invalid_request", "leverage must be positive")
            return response

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
                "wallet_label": want,
                "address": sender,
                "asset_id": resolved_asset_id,
                "coin": coin,
                "preview": preview_text,
                "effects": effects,
            }
        )
        _annotate_hl_profile(
            address=sender,
            label=want,
            action="update_leverage",
            status=status,
            details={"asset_id": resolved_asset_id, "coin": coin, "leverage": lev},
        )

        return response

    if action == "cancel_order":
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
                response = err(
                    "invalid_request",
                    "order_id or cancel_cloid is required for cancel_order",
                )
                return response
            ok_cancel, res = await adapter.cancel_order(
                resolved_asset_id, int(order_id), sender
            )
            effects.append(
                {"type": "hl", "label": "cancel_order", "ok": ok_cancel, "result": res}
            )

        ok_all = all(bool(e.get("ok")) for e in effects) if effects else False
        status = "confirmed" if ok_all else "failed"
        response = ok(
            {
                "status": status,
                "action": action,
                "wallet_label": want,
                "address": sender,
                "asset_id": resolved_asset_id,
                "coin": coin,
                "preview": preview_text,
                "effects": effects,
            }
        )
        _annotate_hl_profile(
            address=sender,
            label=want,
            action="cancel_order",
            status=status,
            details={
                "asset_id": resolved_asset_id,
                "coin": coin,
                "order_id": order_id,
                "cancel_cloid": cancel_cloid,
            },
        )

        return response

    # place_order requires explicit is_spot
    if is_spot is None:
        return err(
            "invalid_request",
            "is_spot must be explicitly set for place_order (True for spot, False for perp)",
        )

    if size is not None and usd_amount is not None:
        response = err(
            "invalid_request",
            "Provide either size (coin units) or usd_amount (USD notional/margin), not both",
        )
        return response
    if usd_amount_kind is not None and usd_amount is None:
        response = err(
            "invalid_request",
            "usd_amount_kind is only valid when usd_amount is provided",
        )
        return response

    if is_buy is None:
        response = err("invalid_request", "is_buy is required for place_order")
        return response

    if order_type == "limit":
        if price is None:
            response = err("invalid_request", "price is required for limit orders")
            return response
        try:
            px_for_sizing = float(price)
        except (TypeError, ValueError):
            response = err("invalid_request", "price must be a number")
            return response
        if px_for_sizing <= 0:
            response = err("invalid_request", "price must be positive")
            return response
    else:
        try:
            slip = float(slippage)
        except (TypeError, ValueError):
            response = err("invalid_request", "slippage must be a number")
            return response
        if slip < 0:
            response = err("invalid_request", "slippage must be >= 0")
            return response
        if slip > 0.25:
            response = err("invalid_request", "slippage > 0.25 is too risky")
            return response
        px_for_sizing = None

    sizing: dict[str, Any] = {"source": "size"}
    if size is not None:
        try:
            sz = float(size)
        except (TypeError, ValueError):
            response = err("invalid_request", "size must be a number")
            return response
        if sz <= 0:
            response = err("invalid_request", "size must be positive")
            return response
    else:
        if usd_amount is None:
            response = err(
                "invalid_request",
                "Provide either size (coin units) or usd_amount for place_order",
            )
            return response
        try:
            usd_amt = float(usd_amount)
        except (TypeError, ValueError):
            response = err("invalid_request", "usd_amount must be a number")
            return response
        if usd_amt <= 0:
            response = err("invalid_request", "usd_amount must be positive")
            return response

        # Spot: usd_amount is always notional (no leverage)
        if is_spot:
            notional_usd = usd_amt
            margin_usd = None
        elif usd_amount_kind is None:
            response = err(
                "invalid_request",
                "usd_amount_kind is required for perp: 'notional' or 'margin'",
            )
            return response
        elif usd_amount_kind == "margin":
            if leverage is None:
                response = err(
                    "invalid_request",
                    "leverage is required when usd_amount_kind='margin'",
                )
                return response
            try:
                lev = int(leverage)
            except (TypeError, ValueError):
                response = err("invalid_request", "leverage must be an int")
                return response
            if lev <= 0:
                response = err("invalid_request", "leverage must be positive")
                return response
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
            coin_name = _PERP_SUFFIX_RE.sub("", str(coin or "").strip()).strip()
            if not coin_name:
                coin_name = _coin_from_asset_id(resolved_asset_id) or ""
            if not coin_name:
                response = err(
                    "invalid_request",
                    "coin is required when computing size from usd_amount for market orders",
                )
                return response
            ok_mids, mids = await adapter.get_all_mid_prices()
            if not ok_mids or not isinstance(mids, dict):
                response = err("price_error", "Failed to fetch mid prices")
                return response
            mid = None
            for k, v in mids.items():
                if str(k).lower() == coin_name.lower():
                    try:
                        mid = float(v)
                    except (TypeError, ValueError):
                        mid = None
                    break
            if mid is None or mid <= 0:
                response = err(
                    "price_error",
                    f"Could not resolve mid price for {coin_name}",
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
        response = err("invalid_request", "size is too small after lot-size rounding")
        return response

    try:
        builder = _resolve_builder_fee(
            config=config,
            builder_fee_tenths_bp=builder_fee_tenths_bp,
        )
    except ValueError as exc:
        response = err("invalid_request", str(exc))
        return response

    if leverage is not None:
        try:
            lev = int(leverage)
        except (TypeError, ValueError):
            response = err("invalid_request", "leverage must be an int")
            return response
        if lev <= 0:
            response = err("invalid_request", "leverage must be positive")
            return response
        ok_lev, res = await adapter.update_leverage(
            resolved_asset_id, lev, bool(is_cross), sender
        )
        effects.append(
            {"type": "hl", "label": "update_leverage", "ok": ok_lev, "result": res}
        )
        if not ok_lev:
            response = ok(
                {
                    "status": "failed",
                    "action": action,
                    "wallet_label": want,
                    "address": sender,
                    "asset_id": resolved_asset_id,
                    "coin": coin,
                    "preview": preview_text,
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
            "result": {"current_tenths_bp": int(current), "desired_tenths_bp": desired},
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
                    "wallet_label": want,
                    "address": sender,
                    "asset_id": resolved_asset_id,
                    "coin": coin,
                    "preview": preview_text,
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
            {"type": "hl", "label": "place_limit_order", "ok": ok_order, "result": res}
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
            {"type": "hl", "label": "place_market_order", "ok": ok_order, "result": res}
        )

    ok_all = all(bool(e.get("ok")) for e in effects) if effects else False
    status = "confirmed" if ok_all else "failed"
    response = ok(
        {
            "status": status,
            "action": action,
            "wallet_label": want,
            "address": sender,
            "asset_id": resolved_asset_id,
            "coin": coin,
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
            "preview": preview_text,
            "effects": effects,
        }
    )
    _annotate_hl_profile(
        address=sender,
        label=want,
        action="place_order",
        status=status,
        details={
            "asset_id": resolved_asset_id,
            "coin": coin,
            "order_type": order_type,
            "is_buy": bool(is_buy),
            "size": float(sz_valid),
        },
    )

    return response
