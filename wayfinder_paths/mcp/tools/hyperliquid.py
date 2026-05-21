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
    MARKET_TYPE_HIP3,
    MARKET_TYPE_HIP4,
    MARKET_TYPE_SPOT,
    MIN_ORDER_USD_NOTIONAL,
    MIN_WITHDRAW_USD,
    WITHDRAW_FEE_USD,
    HyperliquidMarketType,
)
from wayfinder_paths.core.utils.tokens import build_send_transaction
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.mcp.arg_validation import optional_int
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
    """Reject prices off HL's tick grid; suggest the floor."""
    floored = adapter.get_valid_order_price(asset_id, float(price))
    if floored != float(price):
        price_decimals = adapter.get_price_decimals(asset_id)
        tick = float(Decimal(10) ** (-price_decimals))
        raise ValueError(
            f"price {price} invalid — HL requires ≤ 5 sig figs and ≤ "
            f"{price_decimals} decimals (tick = {tick}). Try price={floored}."
        )


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _position_for_coin(user_state: dict[str, Any], coin: str) -> dict[str, Any] | None:
    for entry in user_state.get("assetPositions", []):
        if not isinstance(entry, dict):
            continue
        position = entry.get("position")
        if not isinstance(position, dict):
            continue
        if str(position.get("coin") or "") == coin:
            return position
    return None


def _normalize_position(position: dict[str, Any] | None) -> dict[str, Any] | None:
    if position is None:
        return None

    raw_size = _float_or_none(position.get("szi")) or 0.0
    if raw_size == 0:
        return None

    leverage = position.get("leverage")
    leverage_value: float | None = None
    margin_mode: str | None = None
    if isinstance(leverage, dict):
        leverage_value = _float_or_none(leverage.get("value"))
        margin_mode = str(leverage.get("type") or "") or None

    return {
        "coin": position.get("coin"),
        "side": "long" if raw_size > 0 else "short",
        "size": raw_size,
        "abs_size": abs(raw_size),
        "entry_px": _float_or_none(position.get("entryPx")),
        "position_value_usd": _float_or_none(position.get("positionValue")),
        "unrealized_pnl_usd": _float_or_none(position.get("unrealizedPnl")),
        "return_on_equity": _float_or_none(position.get("returnOnEquity")),
        "liquidation_px": _float_or_none(position.get("liquidationPx")),
        "margin_used_usd": _float_or_none(position.get("marginUsed")),
        "funding_pnl_since_open_usd": _float_or_none(
            position.get("cumFunding", {}).get("sinceOpen")
        )
        if isinstance(position.get("cumFunding"), dict)
        else None,
        "leverage": leverage_value,
        "margin_mode": margin_mode,
        "raw": position,
    }


def _active_asset_float_pair(
    data: dict[str, Any], key: str
) -> tuple[float | None, float | None]:
    values = data.get(key)
    if not isinstance(values, list) or len(values) < 2:
        return None, None
    return _float_or_none(values[0]), _float_or_none(values[1])


def _capacity_notional_usd(
    *,
    available_margin_usd: float | None,
    leverage: float | None,
    max_base_size: float | None,
    price: float | None,
) -> float | None:
    candidates: list[float] = []
    if available_margin_usd is not None and leverage is not None and leverage > 0:
        candidates.append(max(0.0, available_margin_usd * leverage))
    if max_base_size is not None and price is not None and price > 0:
        candidates.append(max(0.0, max_base_size * price))
    return min(candidates) if candidates else None


def _trade_capacity(
    *,
    available_to_trade_usd: float | None,
    leverage: float | None,
    max_trade_size: float | None,
    price: float | None,
) -> dict[str, Any]:
    available_margin = available_to_trade_usd
    if (
        available_margin is None
        and max_trade_size is not None
        and price is not None
        and price > 0
        and leverage is not None
        and leverage > 0
    ):
        available_margin = max_trade_size * price / leverage

    max_notional = _capacity_notional_usd(
        available_margin_usd=available_margin,
        leverage=leverage,
        max_base_size=max_trade_size,
        price=price,
    )
    max_base_size = max_trade_size
    if (
        max_base_size is None
        and max_notional is not None
        and price is not None
        and price > 0
    ):
        max_base_size = max_notional / price

    return {
        "available_margin_usd": available_margin,
        "max_order_notional_usd": max_notional,
        "max_base_size": max_base_size,
    }


def _summarize_active_asset_data(
    active_asset_data: dict[str, Any],
) -> dict[str, Any]:
    available_long, available_short = _active_asset_float_pair(
        active_asset_data, "availableToTrade"
    )
    max_size_long, max_size_short = _active_asset_float_pair(
        active_asset_data, "maxTradeSzs"
    )
    leverage = active_asset_data.get("leverage")
    leverage_value: float | None = None
    margin_mode: str | None = None
    isolated_raw_usd: float | None = None
    if isinstance(leverage, dict):
        leverage_value = _float_or_none(leverage.get("value"))
        margin_mode = str(leverage.get("type") or "") or None
        isolated_raw_usd = _float_or_none(leverage.get("rawUsd"))

    mark_px = _float_or_none(active_asset_data.get("markPx"))
    long = _trade_capacity(
        available_to_trade_usd=available_long,
        leverage=leverage_value,
        max_trade_size=max_size_long,
        price=mark_px,
    )
    short = _trade_capacity(
        available_to_trade_usd=available_short,
        leverage=leverage_value,
        max_trade_size=max_size_short,
        price=mark_px,
    )
    return {
        "mark_px": mark_px,
        "leverage": leverage_value,
        "margin_mode": margin_mode,
        "isolated_raw_usd": isolated_raw_usd,
        "long": long,
        "short": short,
        "raw": active_asset_data,
    }


def _market_info_from_meta_and_asset_ctxs(
    meta_and_ctxs: Any, coin: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(meta_and_ctxs, list) or not meta_and_ctxs:
        return None, None

    meta = meta_and_ctxs[0] if isinstance(meta_and_ctxs[0], dict) else {}
    universe = meta.get("universe")
    if not isinstance(universe, list):
        return None, None

    ctxs = meta_and_ctxs[1] if len(meta_and_ctxs) > 1 else []
    for index, entry in enumerate(universe):
        if not isinstance(entry, dict) or entry.get("name") != coin:
            continue
        ctx = ctxs[index] if isinstance(ctxs, list) and index < len(ctxs) else None
        return entry, ctx if isinstance(ctx, dict) else None
    return None, None


def _market_info_from_all_perp_metas(
    all_perp_metas: Any, coin: str
) -> dict[str, Any] | None:
    if not isinstance(all_perp_metas, list):
        return None

    for dex_index, meta in enumerate(all_perp_metas):
        if not isinstance(meta, dict):
            continue
        universe = meta.get("universe")
        if not isinstance(universe, list):
            continue
        for entry in universe:
            if not isinstance(entry, dict) or entry.get("name") != coin:
                continue
            dex_name = coin.split(":", 1)[0] if ":" in coin else ""
            return {
                **entry,
                "_perp_dex": {
                    "index": dex_index,
                    "name": dex_name,
                    "kind": "hip3" if dex_name else "validator",
                },
                "_collateral_token_index": _int_or_none(meta.get("collateralToken")),
            }
    return None


def _spot_token_by_index(
    spot_meta: Any, token_index: int | None
) -> dict[str, Any] | None:
    if token_index is None or not isinstance(spot_meta, dict):
        return None
    tokens = spot_meta.get("tokens")
    if not isinstance(tokens, list):
        return None
    for token in tokens:
        if not isinstance(token, dict):
            continue
        if _int_or_none(token.get("index")) == token_index:
            return token
    return None


def _collateral_payload(
    *,
    metadata: dict[str, Any] | None,
    spot_meta: Any,
) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None

    token_index = _int_or_none(metadata.get("_collateral_token_index"))
    if token_index is None:
        return None
    token = _spot_token_by_index(spot_meta, token_index)
    dex = (
        metadata.get("_perp_dex")
        if isinstance(metadata.get("_perp_dex"), dict)
        else None
    )
    symbol = token.get("name") if isinstance(token, dict) else None

    return {
        "token_index": token_index,
        "symbol": symbol,
        "full_name": token.get("fullName") if isinstance(token, dict) else None,
        "token_id": token.get("tokenId") if isinstance(token, dict) else None,
        "evm_contract": token.get("evmContract") if isinstance(token, dict) else None,
        "dex": dex,
        "source": "allPerpMetas.collateralToken + spotMeta.tokens",
        "balance_source": (
            "For unified/portfolio accounts, use spotClearinghouseState for "
            "balances and activeAssetData.availableToTrade for this market's "
            "side-specific trade capacity."
        ),
        "requirement": (
            f"Hold or route {symbol or 'the listed collateral token'} collateral "
            "for this perp dex before opening new exposure."
        ),
    }


def _compatible_margin_modes(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {
            "compatible_margin_modes": ["cross", "isolated"],
            "margin_mode_restriction": None,
            "can_remove_isolated_margin": True,
        }

    restriction = metadata.get("marginMode")
    only_isolated = bool(metadata.get("onlyIsolated"))
    if restriction == "strictIsolated":
        return {
            "compatible_margin_modes": ["isolated"],
            "margin_mode_restriction": "strictIsolated",
            "can_remove_isolated_margin": False,
        }
    if restriction == "noCross":
        return {
            "compatible_margin_modes": ["isolated"],
            "margin_mode_restriction": "noCross",
            "can_remove_isolated_margin": True,
        }
    if only_isolated:
        return {
            "compatible_margin_modes": ["isolated"],
            "margin_mode_restriction": "onlyIsolated",
            "can_remove_isolated_margin": None,
        }
    return {
        "compatible_margin_modes": ["cross", "isolated"],
        "margin_mode_restriction": restriction,
        "can_remove_isolated_margin": True,
    }


def _summarize_market_context(
    metadata: dict[str, Any] | None,
    asset_ctx: dict[str, Any] | None,
) -> dict[str, Any]:
    capabilities = _compatible_margin_modes(metadata)
    funding_rate_hourly = (
        _float_or_none(asset_ctx.get("funding"))
        if isinstance(asset_ctx, dict)
        else None
    )
    impact_px_bid: float | None = None
    impact_px_ask: float | None = None
    if isinstance(asset_ctx, dict):
        impact_pxs = asset_ctx.get("impactPxs")
        if isinstance(impact_pxs, list) and len(impact_pxs) >= 2:
            impact_px_bid = _float_or_none(impact_pxs[0])
            impact_px_ask = _float_or_none(impact_pxs[1])

    return {
        "max_leverage": optional_int(
            metadata.get("maxLeverage"), field_name="maxLeverage"
        )
        if isinstance(metadata, dict) and metadata.get("maxLeverage") is not None
        else None,
        "size_decimals": optional_int(
            metadata.get("szDecimals"), field_name="szDecimals"
        )
        if isinstance(metadata, dict) and metadata.get("szDecimals") is not None
        else None,
        "margin_table_id": optional_int(
            metadata.get("marginTableId"), field_name="marginTableId"
        )
        if isinstance(metadata, dict) and metadata.get("marginTableId") is not None
        else None,
        "is_delisted": bool(metadata.get("isDelisted"))
        if isinstance(metadata, dict) and metadata.get("isDelisted") is not None
        else False,
        "growth_mode": metadata.get("growthMode")
        if isinstance(metadata, dict)
        else None,
        "last_growth_mode_change_time": metadata.get("lastGrowthModeChangeTime")
        if isinstance(metadata, dict)
        else None,
        "funding_rate_hourly": funding_rate_hourly,
        "funding_apr": funding_rate_hourly * 24 * 365
        if funding_rate_hourly is not None
        else None,
        "open_interest": _float_or_none(asset_ctx.get("openInterest"))
        if isinstance(asset_ctx, dict)
        else None,
        "day_notional_volume_usd": _float_or_none(asset_ctx.get("dayNtlVlm"))
        if isinstance(asset_ctx, dict)
        else None,
        "mid_px": _float_or_none(asset_ctx.get("midPx"))
        if isinstance(asset_ctx, dict)
        else None,
        "oracle_px": _float_or_none(asset_ctx.get("oraclePx"))
        if isinstance(asset_ctx, dict)
        else None,
        "premium": _float_or_none(asset_ctx.get("premium"))
        if isinstance(asset_ctx, dict)
        else None,
        "impact_px_bid": impact_px_bid,
        "impact_px_ask": impact_px_ask,
        "raw_metadata": metadata,
        "raw_context": asset_ctx,
        **capabilities,
    }


async def _build_trade_asset(
    *,
    adapter: HyperliquidAdapter,
    address: str,
    asset_name: str,
    perp_state: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        coin = adapter.active_asset_data_coin(asset_name)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    active_ok, active = await adapter.get_active_asset_data(address, asset_name)
    if not active_ok or not isinstance(active, dict):
        return {"success": False, "coin": coin, "error": str(active)}

    metadata: dict[str, Any] | None = None
    asset_ctx: dict[str, Any] | None = None
    spot_meta: Any = None
    all_metas_ok, all_metas = await adapter.get_all_perp_metas()
    if all_metas_ok:
        metadata = _market_info_from_all_perp_metas(all_metas, coin)

    meta_ok, meta_and_ctxs = await adapter.get_meta_and_asset_ctxs()
    if meta_ok:
        fallback_metadata, asset_ctx = _market_info_from_meta_and_asset_ctxs(
            meta_and_ctxs, coin
        )
        if metadata is None:
            metadata = fallback_metadata
    spot_ok, fetched_spot_meta = await adapter.get_spot_meta()
    if spot_ok:
        spot_meta = fetched_spot_meta
    market = _summarize_market_context(metadata, asset_ctx)
    collateral = _collateral_payload(metadata=metadata, spot_meta=spot_meta)
    if collateral is not None:
        market["collateral"] = collateral

    position = None
    if isinstance(perp_state, dict):
        position = _position_for_coin(perp_state, coin)

    summary = _summarize_active_asset_data(active)
    summary.update(
        {
            "success": True,
            "asset_name": asset_name,
            "coin": coin,
            "position": _normalize_position(position),
            "market_type": adapter.get_market_type(asset_name),
            "market": market,
            "collateral": collateral,
            "max_leverage": market["max_leverage"],
            "compatible_margin_modes": market["compatible_margin_modes"],
            "margin_mode_restriction": market["margin_mode_restriction"],
            "can_remove_isolated_margin": market["can_remove_isolated_margin"],
        }
    )
    return summary


async def _reject_unsafe_perp_order(
    *,
    adapter: HyperliquidAdapter,
    sender: str,
    asset_name: str,
    market_type: str,
    is_buy: bool,
    reduce_only: bool,
    allow_flip: bool,
    size: float,
    price: float | None,
) -> dict[str, Any] | None:
    if market_type in {MARKET_TYPE_SPOT, MARKET_TYPE_HIP4}:
        return None

    try:
        coin = adapter.active_asset_data_coin(asset_name)
    except ValueError:
        return None

    state_ok, state = await adapter.get_user_state(sender)
    if not state_ok or not isinstance(state, dict):
        return err(
            "preflight_failed",
            "Could not verify the live Hyperliquid position before placing a perp order.",
            details={"asset_name": asset_name, "coin": coin, "error": str(state)},
        )

    active_ok, active = await adapter.get_active_asset_data(sender, asset_name)
    if not active_ok or not isinstance(active, dict):
        return err(
            "preflight_failed",
            "Could not verify Hyperliquid available-to-trade capacity before placing a perp order.",
            details={"asset_name": asset_name, "coin": coin, "error": str(active)},
        )
    summary = _summarize_active_asset_data(active)
    price_used = price if price is not None and price > 0 else summary["mark_px"]
    if price_used is None or price_used <= 0:
        return err(
            "preflight_failed",
            "Could not verify Hyperliquid margin capacity without a valid price.",
            details={
                "asset_name": asset_name,
                "coin": coin,
                "price": price,
                "mark_px": summary["mark_px"],
            },
        )

    order_size = float(size)
    order_sign = 1.0 if is_buy else -1.0
    position = _normalize_position(_position_for_coin(state, coin))
    position_size = float(position["size"]) if position else 0.0
    position_abs = abs(position_size)
    is_reducing = position is not None and position_size * order_sign < 0

    if reduce_only:
        if position is None or not is_reducing:
            return err(
                "reduce_only_no_position",
                "reduce_only=true was set, but there is no opposite live position to reduce.",
                details={
                    "asset_name": asset_name,
                    "coin": coin,
                    "position": position,
                    "is_buy": bool(is_buy),
                },
            )
        if order_size > position_abs + 1e-9:
            return err(
                "reduce_only_size_exceeds_position",
                "reduce_only order size is larger than the live position. Use the closeable size or set allow_flip=true and reduce_only=false if the user explicitly wants to flip.",
                details={
                    "asset_name": asset_name,
                    "coin": coin,
                    "position": position,
                    "requested_size": order_size,
                    "closeable_size": position_abs,
                    "requested_notional_usd": order_size * float(price_used),
                    "closeable_notional_usd": position_abs * float(price_used),
                    "allow_flip": bool(allow_flip),
                },
            )
        return None

    opening_size = order_size
    if is_reducing:
        if not allow_flip:
            return err(
                "reduce_only_required",
                "This order is opposite an existing perp position. Use reduce_only=true to reduce/close, or allow_flip=true if the user explicitly asked to flip/open the other side.",
                details={
                    "asset_name": asset_name,
                    "coin": coin,
                    "position": position,
                    "is_buy": bool(is_buy),
                    "reduce_only": bool(reduce_only),
                    "allow_flip": bool(allow_flip),
                },
            )
        opening_size = max(0.0, order_size - position_abs)
        if opening_size <= 1e-9:
            return None

    side = "long" if is_buy else "short"
    side_context = summary[side]
    available_margin = side_context["available_margin_usd"]
    max_trade_size = side_context["max_base_size"]
    leverage = summary["leverage"]
    capacity_notional = _capacity_notional_usd(
        available_margin_usd=available_margin,
        leverage=leverage,
        max_base_size=max_trade_size,
        price=float(price_used),
    )
    requested_notional = opening_size * float(price_used)
    required_margin = (
        requested_notional / float(leverage)
        if leverage is not None and leverage > 0
        else None
    )

    if capacity_notional is None:
        return err(
            "preflight_failed",
            "Could not compute Hyperliquid order capacity from activeAssetData.",
            details={
                "asset_name": asset_name,
                "coin": coin,
                "side": side,
                "active_asset_data": summary,
                "side_context": side_context,
            },
        )

    if requested_notional > capacity_notional + 1e-9:
        if capacity_notional <= 1e-9:
            dex = asset_name.split(":", 1)[0] if market_type == MARKET_TYPE_HIP3 else ""
            collateral_coin = (await adapter.get_dex_collateral_mapping())[dex]
            return err(
                "insufficient_collateral",
                f"You have 0 collateral for this market, you need {collateral_coin} to collateralize this position.",
                details={
                    "asset_name": asset_name,
                    "market_type": market_type,
                    "collateral_coin": collateral_coin,
                },
            )
        return err(
            "insufficient_hyperliquid_margin",
            "Requested Hyperliquid perp notional exceeds the side-specific available-to-trade capacity.",
            details={
                "asset_name": asset_name,
                "coin": coin,
                "side": side,
                "position": position,
                "requested_size": order_size,
                "opening_size_checked": opening_size,
                "price_used": float(price_used),
                "requested_notional_usd": requested_notional,
                "max_order_notional_usd": capacity_notional,
                "available_to_trade_margin_usd": available_margin,
                "available_margin_usd": available_margin,
                "leverage": leverage,
                "required_margin_usd": required_margin,
                "max_trade_size": max_trade_size,
                "side_context": side_context,
                "reduce_only": bool(reduce_only),
                "allow_flip": bool(allow_flip),
            },
        )

    return None


async def _reject_if_no_market_collateral(
    *,
    adapter: HyperliquidAdapter,
    sender: str,
    asset_name: str,
    market_type: str,
) -> dict[str, Any] | None:
    if market_type == MARKET_TYPE_SPOT:
        return None
    if market_type == MARKET_TYPE_HIP4:
        spot_ok, spot = await adapter.get_spot_user_state(sender)
        if not spot_ok:
            return None
        has_funds = any(
            b["coin"] == "USDH" and float(b["total"]) > 0 for b in spot["balances"]
        )
        coin = "USDH"
    else:
        dex = asset_name.split(":", 1)[0] if market_type == MARKET_TYPE_HIP3 else ""
        state_ok, state = await adapter.get_clearinghouse_state_for_dex(sender, dex)
        if not state_ok:
            return None
        has_funds = float(state["marginSummary"]["accountValue"]) > 0
        coin = (await adapter.get_dex_collateral_mapping())[dex]
    if has_funds:
        return None
    return err(
        "insufficient_collateral",
        f"You have 0 collateral for this market, you need {coin} to collateralize this position.",
        details={
            "asset_name": asset_name,
            "market_type": market_type,
            "collateral_coin": coin,
        },
    )


def _extract_filled_notional_usd(result: dict[str, Any]) -> float | None:
    statuses = (
        result.get("response", {}).get("data", {}).get("statuses", [])
        if isinstance(result, dict)
        else []
    )
    if not isinstance(statuses, list):
        return None

    total = 0.0
    saw_fill = False
    for status in statuses:
        if not isinstance(status, dict):
            continue
        fill = status.get("filled")
        if not isinstance(fill, dict):
            continue
        size = (
            _float_or_none(fill.get("totalSz"))
            or _float_or_none(fill.get("sz"))
            or _float_or_none(fill.get("size"))
        )
        price = (
            _float_or_none(fill.get("avgPx"))
            or _float_or_none(fill.get("px"))
            or _float_or_none(fill.get("price"))
        )
        if size is None or price is None:
            continue
        total += abs(size) * price
        saw_fill = True

    return total if saw_fill else None


def _market_fill_summary(
    *,
    ok_order: bool,
    result: dict[str, Any],
    sizing: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    if not ok_order:
        return "failed", None
    if sizing.get("source") != "usd_amount":
        return "confirmed", None

    requested = _float_or_none(sizing.get("usd_amount"))
    filled = _extract_filled_notional_usd(result)
    if requested is None or requested <= 0 or filled is None:
        return "confirmed", None

    fill_ratio = filled / requested
    is_partial = fill_ratio < 0.95
    return (
        "partial" if is_partial else "confirmed",
        {
            "requested_notional_usd": requested,
            "filled_notional_usd": filled,
            "fill_ratio": fill_ratio,
            "is_partial": is_partial,
            "warning": "Filled materially below requested notional."
            if is_partial
            else None,
        },
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

    Outcomes settle in USDH (token 360), trade as integer contracts, and have
    a $10 USDH minimum order value. The standard Wayfinder builder code is
    attached on every outcome order (HL accrues the fee on the sell side per
    the HIP-4 spec). `usd_amount` sizing is market-only — limit outcome
    orders require explicit integer `size`.
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
    if not ok_order:
        no_collateral = await _reject_if_no_market_collateral(
            adapter=adapter,
            sender=sender,
            asset_name=asset_name,
            market_type=MARKET_TYPE_HIP4,
        )
        if no_collateral is not None:
            return no_collateral
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

    `amount_usdc` is the **gross amount debited from the unified balance**.
    Bridge2 takes a $1 USDC fee out of it, so the wallet receives
    `amount_usdc - 1` USDC on Arbitrum. Minimum `amount_usdc` is `$2`
    (anything smaller leaves nothing after the fee).

    Args:
        wallet_label: Wallet receiving the withdrawal on Arbitrum.
        amount_usdc: USDC debited from the unified balance (must be >= 2).
            Net delivered to Arbitrum = `amount_usdc - 1`.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    amt = throw_if_not_number("amount_usdc must be a number", amount_usdc)
    if amt < MIN_WITHDRAW_USD:
        raise ValueError(
            f"amount_usdc must be >= {MIN_WITHDRAW_USD:g} USDC "
            f"(Bridge2 takes a ${WITHDRAW_FEE_USD:g} fee out of this amount)"
        )

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

    HIP-3 perps (`xyz:`, `flx:`, `vntl:`, `hyna:`, `km:`, ...) only support
    isolated margin; `is_cross=True` is silently overridden to `False` for them.

    Args:
        wallet_label: Wallet to update.
        asset_name: Canonical perp identifier (`BTC-USDC`, `xyz:SP500`). Not for spot.
        leverage: Positive integer; HL enforces a per-asset maximum.
        is_cross: True for cross margin (default), False for isolated. Forced
            to False on HIP-3 perps.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    asset_name = throw_if_empty_str("asset_name is required", asset_name)
    lev = throw_if_not_int("leverage must be an int", leverage)
    if lev <= 0:
        raise ValueError("leverage must be positive")

    try:
        adapter, sender = await _make_hl_adapter(wallet_label)
        resolved_asset_id, market_type = await _resolve_asset(adapter, asset_name)
    except ValueError as exc:
        return err("invalid_coin", str(exc))

    effective_is_cross = bool(is_cross)
    if market_type == MARKET_TYPE_HIP3 and effective_is_cross:
        effective_is_cross = False

    effects: list[dict[str, Any]] = []
    ok_lev, res = await adapter.update_leverage(
        resolved_asset_id, lev, effective_is_cross, sender
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
            "is_cross": effective_is_cross,
        },
    )
    return ok(
        {
            "status": status,
            "wallet_label": wallet_label,
            "address": sender,
            "asset_id": resolved_asset_id,
            "asset_name": asset_name,
            "leverage": lev,
            "is_cross": effective_is_cross,
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

    Perp-only: spot and HIP-4 outcome markets are rejected up-front because
    triggers close an existing perp position (always `reduce_only`), which
    those markets don't have.

    Set `is_buy` to the side that **closes** your position (long → False, short → True).
    A market trigger fills at market on touch; a limit trigger needs `price`.

    Args:
        wallet_label: Wallet owning the position.
        asset_name: Perp identifier (`BTC-USDC`, `xyz:SP500`). Spot (`BTC/USDC`)
            and HIP-4 outcomes (`#N`) are rejected.
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
        resolved_asset_id, market_type = await _resolve_asset(adapter, asset_name)
    except ValueError as exc:
        return err("invalid_coin", str(exc))

    # Trigger orders close existing positions (always reduce_only). Spot has no
    # positions to reduce, and HIP-4 outcomes are binary integer contracts with
    # no TP/SL semantics — HL rejects both downstream, so guard up-front.
    if market_type in (MARKET_TYPE_SPOT, MARKET_TYPE_HIP4):
        return err(
            "invalid_market",
            f"Trigger (TP/SL) orders are perp-only; {asset_name!r} is a "
            f"{market_type} market. Trigger orders close an existing perp "
            "position, which spot and HIP-4 outcome markets don't have.",
        )

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
    allow_flip: bool = False,
    cloid: str | None = None,
) -> dict[str, Any]:
    """Place an IOC market order on a Hyperliquid perp / spot / HIP-4 market.

    HIP-4 outcome markets (`#N` asset names) trade as integer contracts and
    require a $10 USDH minimum order value — `usd_amount` is converted to
    contracts at mid.

    `usd_amount` is converted to asset units at the mid price, then **rounded
    down to the asset's lot size** — actual notional can be a few % below the
    requested USD.

    For leverage / margin mode, call `hyperliquid_update_leverage` first.

    Args:
        wallet_label: Wallet placing the order.
        asset_name: Canonical perp/spot/outcome identifier (`BTC-USDC`, `xyz:SP500`, `BTC/USDC`, `#N`).
        is_buy: True to buy, False to sell.
        size: Order size in asset units (or integer contracts for HIP-4).
        usd_amount: USD notional alternative to `size`.
        slippage: Slippage cap as a fraction (default 0.01 = 1%, max 0.25).
        reduce_only: True to close-only (perp). Ignored for spot.
        allow_flip: True only when the user explicitly asked to flip/open through an opposite position.
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
    unsafe = await _reject_unsafe_perp_order(
        adapter=adapter,
        sender=sender,
        asset_name=asset_name,
        market_type=market_type,
        is_buy=bool(is_buy),
        reduce_only=bool(reduce_only),
        allow_flip=bool(allow_flip),
        size=float(sz_valid),
        price=px_for_sizing,
    )
    if unsafe is not None:
        return unsafe

    effects: list[dict[str, Any]] = []
    await _ensure_builder_fee_approval(adapter, sender=sender, effects=effects)

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
    if not ok_order:
        no_collateral = await _reject_if_no_market_collateral(
            adapter=adapter,
            sender=sender,
            asset_name=asset_name,
            market_type=market_type,
        )
        if no_collateral is not None:
            return no_collateral
    effects.append(
        {"type": "hl", "label": "place_market_order", "ok": ok_order, "result": res}
    )

    status, fill = _market_fill_summary(
        ok_order=all(e["ok"] for e in effects),
        result=res,
        sizing=sizing,
    )
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
                "allow_flip": bool(allow_flip),
                "cloid": cloid,
                "builder": DEFAULT_HYPERLIQUID_BUILDER_FEE,
                "sizing": sizing,
                "fill": fill,
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
    allow_flip: bool = False,
    cloid: str | None = None,
) -> dict[str, Any]:
    """Place a GTC limit order on a Hyperliquid perp / spot / HIP-4 market.

    HIP-4 outcome markets (`#N` asset names) trade as integer contracts and
    require a $10 USDH minimum order value. `usd_amount` sizing is not
    supported for limit outcomes — pass an integer `size`.

    For leverage / margin mode, call `hyperliquid_update_leverage` first.

    Args:
        wallet_label: Wallet placing the order.
        asset_name: Canonical perp/spot/outcome identifier (`BTC-USDC`, `xyz:SP500`, `BTC/USDC`, `#N`).
        is_buy: True to buy, False to sell.
        price: Limit price (positive).
        size: Order size in asset units (or integer contracts for HIP-4).
        usd_amount: USD notional alternative to `size`; converted to size at `price`. Not supported for HIP-4.
        reduce_only: True to close-only (perp). Ignored for spot.
        allow_flip: True only when the user explicitly asked to flip/open through an opposite position.
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
    unsafe = await _reject_unsafe_perp_order(
        adapter=adapter,
        sender=sender,
        asset_name=asset_name,
        market_type=market_type,
        is_buy=bool(is_buy),
        reduce_only=bool(reduce_only),
        allow_flip=bool(allow_flip),
        size=float(sz_valid),
        price=float(px),
    )
    if unsafe is not None:
        return unsafe

    effects: list[dict[str, Any]] = []
    await _ensure_builder_fee_approval(adapter, sender=sender, effects=effects)

    ok_order, res = await adapter.place_limit_order(
        resolved_asset_id,
        bool(is_buy),
        float(px),
        float(sz_valid),
        sender,
        reduce_only=bool(reduce_only),
        cloid=cloid,
        builder=DEFAULT_HYPERLIQUID_BUILDER_FEE,
    )
    if not ok_order:
        no_collateral = await _reject_if_no_market_collateral(
            adapter=adapter,
            sender=sender,
            asset_name=asset_name,
            market_type=market_type,
        )
        if no_collateral is not None:
            return no_collateral
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
                "allow_flip": bool(allow_flip),
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
    abstraction_ok, abstraction = await adapter.get_user_abstraction(addr)

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
            "account_abstraction": {
                "success": abstraction_ok,
                "state": abstraction,
            },
            "outcomes": {"success": spot_ok, "positions": outcome_positions},
        }
    )


@catch_errors
async def hyperliquid_get_trade_asset(label: str, asset_name: str) -> dict[str, Any]:
    """Return selected perp/HIP-3 trade capacity from activeAssetData.

    The response includes current account-side capacity (`long`/`short`), market
    metadata (`max_leverage`, `compatible_margin_modes`, size decimals,
    margin-table id), and live market context such as funding/open interest when
    available from `metaAndAssetCtxs`.

    Args:
        label: Configured Wayfinder wallet label in the current runtime, such as
            "main" or "free-seeking-moon primary wallet".
        asset_name: Hyperliquid perp/HIP-3 market, such as "ETH-USDC",
            "HYPE-USDC", or "xyz:NVDA".
    """
    addr, _ = await resolve_wallet_address(wallet_label=label)
    if not addr:
        return err("not_found", f"Wallet not found: {label}")

    adapter = HyperliquidAdapter()
    perp_ok, perp = await adapter.get_user_state(addr)
    if not perp_ok or not isinstance(perp, dict):
        return err(
            "state_error",
            "Could not fetch Hyperliquid perp state for trade asset.",
            details={"asset_name": asset_name, "error": str(perp)},
        )

    return ok(
        await _build_trade_asset(
            adapter=adapter,
            address=addr,
            asset_name=asset_name,
            perp_state=perp,
        )
    )


@catch_errors
async def hyperliquid_search_mid_prices(
    asset_names: list[str] | None = None,
) -> dict[str, Any]:
    """
    Search Hyperliquid perpetual, spot, hip3 perpetual and hip4 outcome markets for current mid prices.

    Returned keys are always the canonical market paths (`BTC-USDC`, `HYPE/USDC`,
    `xyz:NVDA`, `#40`) regardless of whether `asset_names` is provided.

    asset_names: Canonical market paths to filter mid prices (e.g. "BTC-USDC", "xyz:NVDA",
        "KNTQ/USDH", "#40"), get these from hyperliquid_search_market(). If omitted, returns every market's mid price. Prefer non empty asset_names for efficiency.
    """
    adapter = HyperliquidAdapter()
    success, prices = await adapter.get_all_mid_prices()
    if asset_names:
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

    # Rewrite raw allMids keys to canonical asset names so the response is
    # interchangeable with every other tool's asset_name format. See
    # HyperliquidAdapter.canonical_from_mid_price_key for the key grammar.
    _, spot_map = await adapter.get_spot_assets()
    spot_index_to_pair = {f"@{aid - 10000}": name for name, aid in spot_map.items()}
    canonical = {
        adapter.canonical_from_mid_price_key(key, spot_index_to_pair): mid
        for key, mid in prices.items()
    }
    return ok({"success": success, "prices": canonical})


@catch_errors
async def hyperliquid_search_market(
    query: str,
    limit: int = 10,
    market_type: HyperliquidMarketType | None = None,
) -> dict[str, Any]:
    """
    Search Hyperliquid perpetual, spot, hip3 perpetual and hip4 outcome markets by a simple query string. An empty
    query returns the first `limit` items from each bucket unfiltered.

    query: A simple string containing asset names, for example: btc, eth, oil. Prefer non empty queries for efficiency.
    limit: Max number of results to return per category.
    market_type: optional filter — "perp", "hip3", "spot", or "hip4". Buckets the caller filters out come back empty.

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

    match market_type:
        case "perp":
            perp_hits = [h for h in perp_hits if ":" not in h["name"]]
            spot_hits, outcome_hits = [], []
        case "hip3":
            perp_hits = [h for h in perp_hits if ":" in h["name"]]
            spot_hits, outcome_hits = [], []
        case "spot":
            perp_hits, outcome_hits = [], []
        case "hip4":
            perp_hits, spot_hits = [], []

    return ok(
        {
            "perps": perp_hits,
            "spots": spot_hits,
            "outcomes": outcome_hits,
        }
    )
