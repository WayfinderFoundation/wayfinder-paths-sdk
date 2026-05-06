from __future__ import annotations

import asyncio
import json
from typing import Any, Literal, TypedDict

from hyperliquid.utils.types import OUTCOME_ASSET_OFFSET

from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter
from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.constants.hyperliquid import (
    ARBITRUM_USDC_ADDRESS,
    DEFAULT_HYPERLIQUID_BUILDER_FEE_TENTHS_BP,
    HYPE_FEE_WALLET,
    HYPERLIQUID_BRIDGE_ADDRESS,
)
from wayfinder_paths.core.utils.tokens import build_send_transaction
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.core.utils.wallets import get_wallet_signing_callback
from wayfinder_paths.mcp.preview import build_hyperliquid_execute_preview
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    err,
    normalize_address,
    ok,
    parse_amount_to_raw,
    resolve_wallet_address,
)

# Coin name grammar accepted by hyperliquid_execute and surfaced by hyperliquid_get_markets.
# Match is exact — no case folding, no whitespace tolerance, no aliasing.
#   "BTC-USDC"   core perp           -> coin_to_asset["BTC"]
#   "xyz:SP500"  HIP-3 builder perp  -> coin_to_asset["xyz:SP500"]
#   "BTC/USDC"   spot pair           -> get_spot_assets["BTC/USDC"]
#   "#40"        HIP-4 outcome       -> encoding=40, outcome_id=4, side=0
_PERP_QUOTE_SUFFIX = "-USDC"
_BAD_COIN_HINT = (
    "Expected one of 'BTC-USDC' (core perp), 'xyz:SP500' (HIP-3 perp), "
    "'BTC/USDC' (spot), or '#40' (HIP-4 outcome). "
    "Call hyperliquid_get_markets to look up the canonical name."
)


def _format_perp_market(name: str) -> str:
    """Render a perp universe entry in the canonical coin-path form.

    HIP-3 builder dexes already carry a `<dex>:<base>` prefix; core perps don't
    have a quote suffix, so we tack on `-USDC`.
    """
    return name if ":" in name else f"{name}{_PERP_QUOTE_SUFFIX}"


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


class ResolvedCoin(TypedDict):
    market_type: Literal["perp", "spot", "outcome"]
    asset_id: int
    coin_clean: str
    outcome_id: int
    side: int


class ResolveError(TypedDict):
    code: str
    message: str


def _bad_coin(coin: str | None) -> ResolveError:
    return {
        "code": "invalid_coin",
        "message": f"Invalid coin {coin!r}. {_BAD_COIN_HINT}",
    }


async def _resolve_coin(
    adapter: HyperliquidAdapter, *, coin: str | None
) -> tuple[Literal[True], ResolvedCoin] | tuple[Literal[False], ResolveError]:
    """Resolve a canonical coin path. Match is exact; bad input is rejected."""
    if not coin:
        return False, _bad_coin(coin)

    if coin.startswith("#"):
        rest = coin[1:]
        if not rest.isdigit():
            return False, _bad_coin(coin)
        encoding = int(rest)
        return True, {
            "market_type": "outcome",
            "asset_id": OUTCOME_ASSET_OFFSET + encoding,
            "coin_clean": coin,
            "outcome_id": encoding // 10,
            "side": encoding % 10,
        }

    if "/" in coin:
        ok_assets, assets = await adapter.get_spot_assets()
        if not ok_assets:
            return False, {"code": "error", "message": "Failed to fetch spot assets"}
        if coin not in assets:
            return False, _bad_coin(coin)
        return True, {
            "market_type": "spot",
            "asset_id": assets[coin],
            "coin_clean": coin,
            "outcome_id": 0,
            "side": 0,
        }

    mapping = adapter.coin_to_asset

    if ":" in coin:  # HIP-3 builder-deployed perp
        if coin not in mapping:
            return False, _bad_coin(coin)
        return True, {
            "market_type": "perp",
            "asset_id": mapping[coin],
            "coin_clean": coin,
            "outcome_id": 0,
            "side": 0,
        }

    if coin.endswith(_PERP_QUOTE_SUFFIX):
        bare = coin[: -len(_PERP_QUOTE_SUFFIX)]
        if bare and bare in mapping:
            return True, {
                "market_type": "perp",
                "asset_id": mapping[bare],
                "coin_clean": bare,
                "outcome_id": 0,
                "side": 0,
            }

    return False, _bad_coin(coin)


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
    coin: str | None = None,
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

    `coin` is the canonical market path returned by `hyperliquid_get_markets`:
      * Core perp:   `"BTC-USDC"`, `"ETH-USDC"`
      * HIP-3 perp:  `"xyz:SP500"`
      * Spot pair:   `"BTC/USDC"`, `"USDC/USDH"`
      * HIP-4 outcome: `"#0"`, `"#1"`, `"#40"` (encoding = `outcome_id*10 + side`)

    Builder attribution is mandatory — every order routes through the Wayfinder builder wallet
    and the tool auto-approves the builder fee on first use.

    Actions:
      - `place_order`: spot / perp / HIP-4 outcome market or limit.
        Size via `size` (coin units) or `usd_amount` (with `usd_amount_kind="notional"|"margin"` for perps).
      - `place_trigger_order`: TP/SL trigger. `tpsl="tp"|"sl"`, `trigger_price`, `is_buy` set to
        the side that closes the position (long → False, short → True).
      - `cancel_order`: by `order_id` or `cancel_cloid`.
      - `update_leverage`: set `leverage` and `is_cross` for an asset.
      - `deposit`: bridge `amount_usdc` from Arbitrum USDC into the HL perp account
        (≥ 5 USDC; below is lost). Auto-waits for the perp clearinghouse credit before returning.
      - `withdraw`: bridge `amount_usdc` from perp account back to Arbitrum.
      - `spot_to_perp_transfer` / `perp_to_spot_transfer`: shift `usd_amount` between sub-accounts.
    """
    want = str(wallet_label or "").strip()
    if not want:
        return err("invalid_request", "wallet_label is required")

    key_input = {
        "action": action,
        "wallet_label": want,
        "coin": coin,
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
        "trigger_price": trigger_price,
        "tpsl": tpsl,
        "is_market_trigger": is_market_trigger,
    }
    tool_input = {"request": key_input}
    preview_obj = await build_hyperliquid_execute_preview(tool_input)
    preview_text = str(preview_obj.get("summary") or "").strip()

    strategy_raw = CONFIG.get("strategy")
    strategy_cfg = strategy_raw if isinstance(strategy_raw, dict) else {}
    config: dict[str, Any] = dict(strategy_cfg)

    effects: list[dict[str, Any]] = []

    try:
        adapter = await get_adapter(HyperliquidAdapter, want, config_overrides=config)
    except ValueError as e:
        return err("invalid_wallet", str(e))
    sender = adapter.wallet_address

    if action == "deposit":
        if amount_usdc is None:
            return err("invalid_request", "amount_usdc is required for deposit")
        try:
            amt = float(amount_usdc)
        except (TypeError, ValueError):
            return err("invalid_request", "amount_usdc must be a number")
        if amt < 5:
            return err(
                "invalid_request",
                "amount_usdc must be >= 5 USDC (HL deposits below are lost)",
            )

        try:
            sign_callback, deposit_sender = await get_wallet_signing_callback(want)
        except ValueError as exc:
            return err("invalid_wallet", str(exc))

        recipient = (
            normalize_address(HYPERLIQUID_BRIDGE_ADDRESS) or HYPERLIQUID_BRIDGE_ADDRESS
        )
        chain_id = 42161
        amount_raw = parse_amount_to_raw(str(amt), 6)
        transaction = await build_send_transaction(
            from_address=deposit_sender,
            to_address=recipient,
            token_address=ARBITRUM_USDC_ADDRESS,
            chain_id=chain_id,
            amount=int(amount_raw),
        )
        try:
            tx_hash = await send_transaction(
                transaction, sign_callback, wait_for_receipt=True
            )
            sent_ok = True
            sent_result: dict[str, Any] = {"txn_hash": tx_hash, "chain_id": chain_id}
        except Exception as exc:  # noqa: BLE001
            sent_ok = False
            sent_result = {"error": str(exc), "chain_id": chain_id}
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
                "wallet_label": want,
                "address": deposit_sender,
                "amount_usdc": amt,
                "preview": preview_text,
                "effects": effects,
            }
        )
        _annotate_hl_profile(
            address=deposit_sender,
            label=want,
            action="deposit",
            status=status,
            details={"amount_usdc": amt, "chain_id": chain_id},
        )
        return response

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

    ok_resolve, resolved = await _resolve_coin(adapter, coin=coin)
    if not ok_resolve:
        return err(resolved["code"], resolved["message"])
    market_type = resolved["market_type"]
    resolved_asset_id = resolved["asset_id"]
    coin_clean = resolved["coin_clean"]

    # HIP-4 outcome orders use a dedicated execution path (not perp/spot wire).
    if action == "place_order" and market_type == "outcome":
        outcome_id_v = resolved["outcome_id"]
        side_v = resolved["side"]
        if is_buy is None or size is None:
            return err(
                "invalid_request", "is_buy and size are required for outcome orders"
            )
        if order_type == "limit" and price is None:
            return err("invalid_request", "price is required for limit orders")
        ok_order, res = await adapter.place_outcome_order(
            outcome_id=outcome_id_v,
            side=side_v,
            is_buy=bool(is_buy),
            size=int(size),
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
            label=want,
            action="place_order",
            status=status,
            details={
                "coin": coin,
                "outcome_id": outcome_id_v,
                "side": side_v,
                "is_buy": bool(is_buy),
                "size": int(size),
            },
        )
        return ok(
            {
                "status": status,
                "action": action,
                "wallet_label": want,
                "address": sender,
                "coin": coin,
                "outcome_id": outcome_id_v,
                "side": side_v,
                "order": {
                    "order_type": order_type,
                    "is_buy": bool(is_buy),
                    "size": int(size),
                    "price": float(price) if price is not None else None,
                    "slippage": float(slippage),
                    "reduce_only": bool(reduce_only),
                    "cloid": cloid,
                },
                "preview": preview_text,
                "effects": effects,
            }
        )

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

    if action == "place_trigger_order":
        if tpsl not in ("tp", "sl"):
            return err(
                "invalid_request", "tpsl must be 'tp' (take-profit) or 'sl' (stop-loss)"
            )
        if trigger_price is None:
            return err(
                "invalid_request", "trigger_price is required for place_trigger_order"
            )
        try:
            tpx = float(trigger_price)
        except (TypeError, ValueError):
            return err("invalid_request", "trigger_price must be a number")
        if tpx <= 0:
            return err("invalid_request", "trigger_price must be positive")
        if is_buy is None:
            return err(
                "invalid_request",
                "is_buy is required for place_trigger_order — set to opposite of your position "
                "(long position → is_buy=False to sell; short position → is_buy=True to buy back)",
            )
        if size is None:
            return err(
                "invalid_request",
                "size is required for place_trigger_order (coin units)",
            )
        try:
            sz = float(size)
        except (TypeError, ValueError):
            return err("invalid_request", "size must be a number")
        if sz <= 0:
            return err("invalid_request", "size must be positive")

        limit_px: float | None = None
        if not is_market_trigger:
            if price is None:
                return err(
                    "invalid_request",
                    "price is required for limit trigger orders (is_market_trigger=False)",
                )
            try:
                limit_px = float(price)
            except (TypeError, ValueError):
                return err("invalid_request", "price must be a number")
            if limit_px <= 0:
                return err("invalid_request", "price must be positive")

        try:
            builder = _resolve_builder_fee(
                config=config, builder_fee_tenths_bp=builder_fee_tenths_bp
            )
        except ValueError as exc:
            return err("invalid_request", str(exc))

        sz_valid = adapter.get_valid_order_size(resolved_asset_id, sz)
        if sz_valid <= 0:
            return err("invalid_request", "size is too small after lot-size rounding")

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
                "wallet_label": want,
                "address": sender,
                "asset_id": resolved_asset_id,
                "coin": coin,
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
                "preview": preview_text,
                "effects": effects,
            }
        )
        _annotate_hl_profile(
            address=sender,
            label=want,
            action="place_trigger_order",
            status=status,
            details={
                "asset_id": resolved_asset_id,
                "coin": coin,
                "tpsl": tpsl,
                "is_buy": bool(is_buy),
                "trigger_price": tpx,
                "size": float(sz_valid),
            },
        )
        return response

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
        if market_type == "spot":
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
            ok_mids, mids = await adapter.get_all_mid_prices()
            if not ok_mids or not isinstance(mids, dict):
                response = err("price_error", "Failed to fetch mid prices")
                return response
            mid = None
            for k, v in mids.items():
                if str(k).lower() == coin_clean.lower():
                    try:
                        mid = float(v)
                    except (TypeError, ValueError):
                        mid = None
                    break
            if mid is None or mid <= 0:
                response = err(
                    "price_error",
                    f"Could not resolve mid price for {coin_clean}",
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


async def hyperliquid_get_state(label: str) -> str:
    """Return perp + spot + outcome state for a Hyperliquid wallet in one shot."""
    addr, _ = await resolve_wallet_address(wallet_label=label)
    if not addr:
        return json.dumps({"error": f"Wallet not found: {label}"})

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

    return json.dumps(
        {
            "label": label,
            "address": addr,
            "perp": {"success": perp_ok, "state": perp},
            "spot": {"success": spot_ok, "state": spot},
            "outcomes": {"success": spot_ok, "positions": outcome_positions},
        },
        indent=2,
    )


async def hyperliquid_get_mid_prices() -> str:
    adapter = HyperliquidAdapter()
    success, data = await adapter.get_all_mid_prices()
    return json.dumps({"success": success, "prices": data}, indent=2)


async def hyperliquid_get_markets() -> str:
    """Return the HL universe as flat lists of canonical coin paths.

    Output keys:
      * `perps`: core perps as `<base>-USDC` and HIP-3 builder perps as `<dex>:<base>`
      * `spots`: spot pairs as `<base>/<quote>` (e.g. `BTC/USDC`, `USDC/USDH`)
      * `outcomes`: HIP-4 outcome book coins as `#<encoding>` (`encoding = outcome_id*10 + side`)
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

    perps = (
        [_format_perp_market(entry["name"]) for entry in perp_data[0]["universe"]]
        if perp_ok
        else []
    )
    spots = list(spot_data) if spot_ok else []
    outcomes = (
        [s["book_coin"] for market in outcome_data for s in market["sides"]]
        if outcome_ok
        else []
    )

    return json.dumps(
        {"perps": perps, "spots": spots, "outcomes": outcomes},
        indent=2,
    )
