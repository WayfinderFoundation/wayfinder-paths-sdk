from __future__ import annotations

from typing import Any, Literal

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
from wayfinder_paths.core.constants.hyperliquid import (
    DEFAULT_HYPERLIQUID_BUILDER_FEE_TENTHS_BP,
    HYPE_FEE_WALLET,
)
from wayfinder_paths.mcp.preview import build_hyperliquid_execute_preview
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    err,
    find_wallet_by_label,
    load_config_json,
    normalize_address,
    ok,
)


def _resolve_wallet_address(
    *, wallet_label: str | None, wallet_address: str | None
) -> str | None:
    waddr = normalize_address(wallet_address)
    if waddr:
        return waddr
    want = (wallet_label or "").strip()
    if not want:
        return None
    w = find_wallet_by_label(want)
    if not w:
        return None
    return normalize_address(w.get("address"))


def _resolve_builder_fee(
    config: dict[str, Any],
    builder_fee_tenths_bp: int | None,
) -> dict[str, Any]:
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
    fee_i = int(fee)
    if fee_i <= 0:
        raise ValueError("builder_fee_tenths_bp must be > 0")
    return {"b": expected_builder, "f": fee_i}


def _annotate(
    address: str, label: str, action: str, status: str, details: dict | None = None
):
    WalletProfileStore.default().annotate_safe(
        address=address,
        label=label,
        protocol="hyperliquid",
        action=action,
        tool="hyperliquid_execute",
        status=status,
        chain_id=999,
        details=details,
    )


# ---------------------------------------------------------------------------
# hyperliquid (wait actions)
# ---------------------------------------------------------------------------


async def _wait_for_deposit(
    adapter: HyperliquidAdapter,
    addr: str,
    expected_increase: float | None,
    timeout_s: int,
    poll_interval_s: int,
) -> dict[str, Any]:
    if expected_increase is None:
        return err(
            "invalid_request", "expected_increase is required for wait_for_deposit"
        )
    inc = float(expected_increase)
    if inc <= 0:
        return err("invalid_request", "expected_increase must be positive")

    ok_dep, final_bal = await adapter.wait_for_deposit(
        addr, inc, timeout_s=timeout_s, poll_interval_s=poll_interval_s
    )
    return ok(
        {
            "wallet_address": addr,
            "action": "wait_for_deposit",
            "expected_increase": inc,
            "confirmed": ok_dep,
            "final_balance_usd": final_bal,
            "timeout_s": timeout_s,
            "poll_interval_s": poll_interval_s,
        }
    )


async def _wait_for_withdrawal(
    adapter: HyperliquidAdapter,
    addr: str,
    lookback_s: int,
    max_poll_time_s: int,
    poll_interval_s: int,
) -> dict[str, Any]:
    ok_wd, withdrawals = await adapter.wait_for_withdrawal(
        addr,
        lookback_s=lookback_s,
        max_poll_time_s=max_poll_time_s,
        poll_interval_s=poll_interval_s,
    )
    return ok(
        {
            "wallet_address": addr,
            "action": "wait_for_withdrawal",
            "confirmed": ok_wd,
            "withdrawals": withdrawals,
            "lookback_s": lookback_s,
            "max_poll_time_s": max_poll_time_s,
            "poll_interval_s": poll_interval_s,
        }
    )


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
    addr = _resolve_wallet_address(
        wallet_label=wallet_label, wallet_address=wallet_address
    )
    if not addr:
        return err("invalid_request", "wallet_label or wallet_address is required")

    adapter = HyperliquidAdapter()

    if action == "wait_for_deposit":
        return await _wait_for_deposit(
            adapter, addr, expected_increase, timeout_s, poll_interval_s
        )
    if action == "wait_for_withdrawal":
        return await _wait_for_withdrawal(
            adapter, addr, lookback_s, max_poll_time_s, poll_interval_s
        )

    return err("invalid_request", f"Unknown action: {action}")


# ---------------------------------------------------------------------------
# hyperliquid_execute handlers
# ---------------------------------------------------------------------------


async def _handle_withdraw(
    adapter: HyperliquidAdapter,
    sender: str,
    label: str,
    amount_usdc: float | None,
    preview: str,
) -> dict[str, Any]:
    if amount_usdc is None:
        return err("invalid_request", "amount_usdc is required for withdraw")
    amt = float(amount_usdc)
    if amt <= 0:
        return err("invalid_request", "amount_usdc must be positive")

    ok_wd, res = await adapter.withdraw(amount=amt, address=sender)
    status = "confirmed" if ok_wd else "failed"
    _annotate(sender, label, "withdraw", status, {"amount_usdc": amt})
    return ok(
        {
            "status": status,
            "action": "withdraw",
            "wallet_label": label,
            "address": sender,
            "amount_usdc": amt,
            "preview": preview,
            "effects": [
                {"type": "hl", "label": "withdraw", "ok": ok_wd, "result": res}
            ],
        }
    )


async def _handle_transfer(
    adapter: HyperliquidAdapter,
    sender: str,
    label: str,
    usd_amount: float | None,
    to_perp: bool,
    preview: str,
) -> dict[str, Any]:
    action = "spot_to_perp_transfer" if to_perp else "perp_to_spot_transfer"
    if usd_amount is None:
        return err("invalid_request", f"usd_amount is required for {action}")
    amt = float(usd_amount)
    if amt <= 0:
        return err("invalid_request", "usd_amount must be positive")

    if to_perp:
        ok_t, res = await adapter.transfer_spot_to_perp(amount=amt, address=sender)
    else:
        ok_t, res = await adapter.transfer_perp_to_spot(amount=amt, address=sender)

    status = "confirmed" if ok_t else "failed"
    _annotate(sender, label, action, status, {"usd_amount": amt, "to_perp": to_perp})
    return ok(
        {
            "status": status,
            "action": action,
            "wallet_label": label,
            "address": sender,
            "usd_amount": amt,
            "to_perp": to_perp,
            "preview": preview,
            "effects": [{"type": "hl", "label": action, "ok": ok_t, "result": res}],
        }
    )


async def _handle_update_leverage(
    adapter: HyperliquidAdapter,
    sender: str,
    label: str,
    asset_id: int,
    coin: str | None,
    leverage: int | None,
    is_cross: bool,
    preview: str,
) -> dict[str, Any]:
    if leverage is None:
        return err("invalid_request", "leverage is required for update_leverage")
    lev = int(leverage)
    if lev <= 0:
        return err("invalid_request", "leverage must be positive")

    ok_lev, res = await adapter.update_leverage(asset_id, lev, is_cross, sender)
    status = "confirmed" if ok_lev else "failed"
    _annotate(
        sender,
        label,
        "update_leverage",
        status,
        {"asset_id": asset_id, "coin": coin, "leverage": lev},
    )
    return ok(
        {
            "status": status,
            "action": "update_leverage",
            "wallet_label": label,
            "address": sender,
            "asset_id": asset_id,
            "coin": coin,
            "preview": preview,
            "effects": [
                {"type": "hl", "label": "update_leverage", "ok": ok_lev, "result": res}
            ],
        }
    )


async def _handle_cancel_order(
    adapter: HyperliquidAdapter,
    sender: str,
    label: str,
    asset_id: int,
    coin: str | None,
    order_id: int | None,
    cancel_cloid: str | None,
    preview: str,
) -> dict[str, Any]:
    if cancel_cloid:
        ok_c, res = await adapter.cancel_order_by_cloid(asset_id, cancel_cloid, sender)
        effect_label = "cancel_order_by_cloid"
    elif order_id is not None:
        ok_c, res = await adapter.cancel_order(asset_id, int(order_id), sender)
        effect_label = "cancel_order"
    else:
        return err(
            "invalid_request", "order_id or cancel_cloid is required for cancel_order"
        )

    status = "confirmed" if ok_c else "failed"
    _annotate(
        sender,
        label,
        "cancel_order",
        status,
        {
            "asset_id": asset_id,
            "coin": coin,
            "order_id": order_id,
            "cancel_cloid": cancel_cloid,
        },
    )
    return ok(
        {
            "status": status,
            "action": "cancel_order",
            "wallet_label": label,
            "address": sender,
            "asset_id": asset_id,
            "coin": coin,
            "preview": preview,
            "effects": [
                {"type": "hl", "label": effect_label, "ok": ok_c, "result": res}
            ],
        }
    )


async def _handle_place_order(
    adapter: HyperliquidAdapter,
    sender: str,
    label: str,
    config: dict[str, Any],
    asset_id: int,
    coin: str | None,
    is_spot: bool,
    is_buy: bool | None,
    order_type: str,
    size: float | None,
    usd_amount: float | None,
    usd_amount_kind: str | None,
    price: float | None,
    slippage: float,
    reduce_only: bool,
    cloid: str | None,
    leverage: int | None,
    is_cross: bool,
    builder_fee_tenths_bp: int | None,
    preview: str,
) -> dict[str, Any]:
    if is_buy is None:
        return err("invalid_request", "is_buy is required for place_order")
    if size is not None and usd_amount is not None:
        return err("invalid_request", "Provide either size or usd_amount, not both")

    effects: list[dict[str, Any]] = []

    # Validate order type params
    if order_type == "limit":
        if price is None:
            return err("invalid_request", "price is required for limit orders")
        px = float(price)
        if px <= 0:
            return err("invalid_request", "price must be positive")
        px_for_sizing = px
    else:
        slip = float(slippage)
        if slip < 0 or slip > 0.25:
            return err("invalid_request", "slippage must be between 0 and 0.25")
        px_for_sizing = None

    # Compute size
    sizing: dict[str, Any] = {"source": "size"}
    if size is not None:
        sz = float(size)
        if sz <= 0:
            return err("invalid_request", "size must be positive")
    else:
        if usd_amount is None:
            return err(
                "invalid_request", "Provide either size or usd_amount for place_order"
            )
        usd_amt = float(usd_amount)
        if usd_amt <= 0:
            return err("invalid_request", "usd_amount must be positive")

        # Compute notional
        if is_spot:
            notional_usd = usd_amt
            margin_usd = None
        elif usd_amount_kind is None:
            return err(
                "invalid_request",
                "usd_amount_kind is required for perp: 'notional' or 'margin'",
            )
        elif usd_amount_kind == "margin":
            if leverage is None:
                return err(
                    "invalid_request",
                    "leverage is required when usd_amount_kind='margin'",
                )
            lev = int(leverage)
            if lev <= 0:
                return err("invalid_request", "leverage must be positive")
            notional_usd = usd_amt * lev
            margin_usd = usd_amt
        else:
            notional_usd = usd_amt
            margin_usd = (
                notional_usd / int(leverage) if leverage and int(leverage) > 0 else None
            )

        # Get price for sizing
        if px_for_sizing is None:
            coin_name = (coin or "").strip()
            if not coin_name:
                # Reverse lookup from asset_id
                for k, v in (adapter.coin_to_asset or {}).items():
                    if int(v) == asset_id:
                        coin_name = str(k)
                        break
            if not coin_name:
                return err(
                    "invalid_request",
                    "coin is required for market orders with usd_amount",
                )

            ok_mids, mids = await adapter.get_all_mid_prices()
            if not ok_mids:
                return err("price_error", "Failed to fetch mid prices")
            mid = next(
                (float(v) for k, v in mids.items() if k.lower() == coin_name.lower()),
                None,
            )
            if not mid or mid <= 0:
                return err(
                    "price_error", f"Could not resolve mid price for {coin_name}"
                )
            px_for_sizing = mid

        sz = notional_usd / px_for_sizing
        sizing = {
            "source": "usd_amount",
            "usd_amount": usd_amt,
            "usd_amount_kind": usd_amount_kind,
            "notional_usd": notional_usd,
            "margin_usd_estimate": margin_usd,
            "price_used": px_for_sizing,
        }

    sz_valid = adapter.get_valid_order_size(asset_id, sz)
    if sz_valid <= 0:
        return err("invalid_request", "size is too small after lot-size rounding")

    # Builder fee
    try:
        builder = _resolve_builder_fee(config, builder_fee_tenths_bp)
    except ValueError as exc:
        return err("invalid_request", str(exc))

    # Set leverage if provided
    if leverage is not None:
        lev = int(leverage)
        if lev <= 0:
            return err("invalid_request", "leverage must be positive")
        ok_lev, res = await adapter.update_leverage(asset_id, lev, is_cross, sender)
        effects.append(
            {"type": "hl", "label": "update_leverage", "ok": ok_lev, "result": res}
        )
        if not ok_lev:
            return ok(
                {
                    "status": "failed",
                    "action": "place_order",
                    "wallet_label": label,
                    "address": sender,
                    "asset_id": asset_id,
                    "coin": coin,
                    "preview": preview,
                    "effects": effects,
                }
            )

    # Ensure builder fee approved
    desired = int(builder["f"])
    ok_fee, current = await adapter.get_max_builder_fee(
        user=sender, builder=builder["b"]
    )
    effects.append(
        {
            "type": "hl",
            "label": "get_max_builder_fee",
            "ok": ok_fee,
            "result": {"current_tenths_bp": current, "desired_tenths_bp": desired},
        }
    )
    if not ok_fee or current < desired:
        max_fee_rate = f"{desired / 1000:.3f}%"
        ok_appr, appr = await adapter.approve_builder_fee(
            builder=builder["b"], max_fee_rate=max_fee_rate, address=sender
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
            return ok(
                {
                    "status": "failed",
                    "action": "place_order",
                    "wallet_label": label,
                    "address": sender,
                    "asset_id": asset_id,
                    "coin": coin,
                    "preview": preview,
                    "effects": effects,
                }
            )

    # Place order
    if order_type == "limit":
        ok_order, res = await adapter.place_limit_order(
            asset_id,
            is_buy,
            float(price),
            sz_valid,
            sender,
            reduce_only=reduce_only,
            builder=builder,
        )
        effects.append(
            {"type": "hl", "label": "place_limit_order", "ok": ok_order, "result": res}
        )
    else:
        ok_order, res = await adapter.place_market_order(
            asset_id,
            is_buy,
            slippage,
            sz_valid,
            sender,
            reduce_only=reduce_only,
            cloid=cloid,
            builder=builder,
        )
        effects.append(
            {"type": "hl", "label": "place_market_order", "ok": ok_order, "result": res}
        )

    status = "confirmed" if all(e.get("ok") for e in effects) else "failed"
    _annotate(
        sender,
        label,
        "place_order",
        status,
        {
            "asset_id": asset_id,
            "coin": coin,
            "order_type": order_type,
            "is_buy": is_buy,
            "size": sz_valid,
        },
    )
    return ok(
        {
            "status": status,
            "action": "place_order",
            "wallet_label": label,
            "address": sender,
            "asset_id": asset_id,
            "coin": coin,
            "order": {
                "order_type": order_type,
                "is_buy": is_buy,
                "size_requested": sz,
                "size_valid": sz_valid,
                "price": float(price) if price else None,
                "slippage": slippage,
                "reduce_only": reduce_only,
                "cloid": cloid,
                "builder": builder,
                "sizing": sizing,
            },
            "preview": preview,
            "effects": effects,
        }
    )


# ---------------------------------------------------------------------------
# hyperliquid_execute (main dispatcher)
# ---------------------------------------------------------------------------


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
    # Resolve wallet
    label = (wallet_label or "").strip()
    if not label:
        return err("invalid_request", "wallet_label is required")

    w = find_wallet_by_label(label)
    if not w:
        return err("not_found", f"Unknown wallet_label: {label}")

    sender = normalize_address(w.get("address"))
    pk = w.get("private_key") or w.get("private_key_hex")
    if not sender or not pk:
        return err("invalid_wallet", "Wallet must include address and private_key_hex")

    # Build config
    cfg_json = load_config_json()
    strategy_cfg = (
        cfg_json.get("strategy") if isinstance(cfg_json.get("strategy"), dict) else {}
    )
    config: dict[str, Any] = dict(strategy_cfg)
    config["main_wallet"] = {"address": sender, "private_key_hex": pk}
    config["strategy_wallet"] = {"address": sender, "private_key_hex": pk}

    adapter = HyperliquidAdapter(config=config)

    # Build preview
    key_input = {
        "action": action,
        "wallet_label": label,
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
    preview = str(
        build_hyperliquid_execute_preview({"request": key_input}).get("summary") or ""
    ).strip()

    # Dispatch by action
    if action == "withdraw":
        return await _handle_withdraw(adapter, sender, label, amount_usdc, preview)

    if action == "spot_to_perp_transfer":
        return await _handle_transfer(
            adapter, sender, label, usd_amount, to_perp=True, preview=preview
        )

    if action == "perp_to_spot_transfer":
        return await _handle_transfer(
            adapter, sender, label, usd_amount, to_perp=False, preview=preview
        )

    # Remaining actions need asset_id resolution
    if not is_spot and asset_id is not None:
        resolved_asset_id = int(asset_id)
    else:
        c = (coin or "").strip()
        if not c:
            msg = "coin is required" if is_spot else "coin or asset_id is required"
            return err("invalid_request", msg)
        resolved_asset_id = await adapter.get_asset_id(c, is_perp=not is_spot)
        if resolved_asset_id is None:
            msg = (
                f"Unknown spot pair: {c.upper()}/USDC"
                if is_spot
                else f"Unknown perp coin: {c}"
            )
            return err("not_found", msg)

    if action == "update_leverage":
        return await _handle_update_leverage(
            adapter, sender, label, resolved_asset_id, coin, leverage, is_cross, preview
        )

    if action == "cancel_order":
        return await _handle_cancel_order(
            adapter,
            sender,
            label,
            resolved_asset_id,
            coin,
            order_id,
            cancel_cloid,
            preview,
        )

    if action == "place_order":
        if is_spot is None:
            return err(
                "invalid_request", "is_spot must be explicitly set for place_order"
            )
        return await _handle_place_order(
            adapter,
            sender,
            label,
            config,
            resolved_asset_id,
            coin,
            is_spot,
            is_buy,
            order_type,
            size,
            usd_amount,
            usd_amount_kind,
            price,
            slippage,
            reduce_only,
            cloid,
            leverage,
            is_cross,
            builder_fee_tenths_bp,
            preview,
        )

    return err("invalid_request", f"Unknown action: {action}")
