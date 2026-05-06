from __future__ import annotations

import asyncio
import json
import re
from difflib import SequenceMatcher
from typing import Any, Literal

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

_PERP_SUFFIX_RE = re.compile(r"[-_ ]?perp$", re.IGNORECASE)
_MARKET_SEARCH_STOPWORDS = {
    "future",
    "futures",
    "market",
    "markets",
    "option",
    "options",
    "perp",
    "perps",
    "spot",
    "trade",
    "trading",
}
_MARKET_SEARCH_ALIASES = {
    "oil": {
        "oil",
        "wti",
        "brent",
        "crude",
        "usoil",
        "brentoil",
        "energy",
        "gas",
        "natgas",
        "naturalgas",
    },
    "wti": {"oil", "wti", "crude", "usoil"},
    "brent": {"oil", "brent", "crude", "brentoil"},
    "crude": {"oil", "wti", "brent", "crude", "usoil", "brentoil"},
    "gas": {"gas", "natgas", "naturalgas", "energy"},
    "natgas": {"gas", "natgas", "naturalgas", "energy"},
    "naturalgas": {"gas", "natgas", "naturalgas", "energy"},
    "energy": {"energy", "oil", "gas", "natgas", "naturalgas"},
    "btc": {"btc", "bitcoin", "ubtc"},
    "bitcoin": {"btc", "bitcoin", "ubtc"},
    "ubtc": {"btc", "bitcoin", "ubtc"},
    "eth": {"eth", "ethereum", "ueth"},
    "ethereum": {"eth", "ethereum", "ueth"},
    "ueth": {"eth", "ethereum", "ueth"},
    "sol": {"sol", "solana", "usol"},
    "solana": {"sol", "solana", "usol"},
    "usol": {"sol", "solana", "usol"},
    "bonk": {"bonk", "kbonk"},
    "kbonk": {"bonk", "kbonk"},
    "nvidia": {"nvidia", "nvda"},
    "nvda": {"nvidia", "nvda"},
    "monad": {"monad", "mon"},
    "mon": {"monad", "mon"},
}


def _market_search_parts(value: str) -> tuple[str, list[str]]:
    """Return compact and tokenized forms for deliberately broad market matching."""
    lower = str(value).lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", lower) if t]
    compact = "".join(tokens)
    extras: list[str] = []
    for alias_terms in _MARKET_SEARCH_ALIASES.values():
        for alias in alias_terms:
            if alias and alias in compact and alias not in tokens:
                extras.append(alias)
    for token in [compact, *tokens]:
        if len(token) > 2 and token[0] in {"k", "u"}:
            extras.append(token[1:])
    return compact, tokens + extras


def _expanded_market_query(query: str) -> tuple[str, list[str], set[str]]:
    compact, raw_terms = _market_search_parts(query)
    terms = {t for t in raw_terms if t not in _MARKET_SEARCH_STOPWORDS}
    if compact == "naturalgas":
        terms.add("naturalgas")
    expanded = set(terms)
    for term in list(terms):
        expanded.update(_MARKET_SEARCH_ALIASES.get(term, set()))
        if len(term) > 1:
            expanded.add(f"k{term}")
            expanded.add(f"u{term}")
        if len(term) > 2 and term[0] in {"k", "u"}:
            expanded.add(term[1:])
    return compact, sorted(terms), expanded


def _score_market_candidate(
    *,
    query_compact: str,
    query_terms: list[str],
    expanded_terms: set[str],
    candidate_name: str,
) -> tuple[float, list[str]]:
    candidate_compact, candidate_terms = _market_search_parts(candidate_name)
    candidate_term_set = set(candidate_terms)
    reasons: list[str] = []
    score = 0.0

    if query_compact and query_compact == candidate_compact:
        score = max(score, 1.0)
        reasons.append("exact")

    direct_hits = {
        term
        for term in query_terms
        if term and (term in candidate_term_set or term in candidate_compact)
    }
    if direct_hits:
        coverage = len(direct_hits) / max(len(query_terms), 1)
        score = max(score, 0.68 + 0.22 * coverage)
        reasons.append("direct:" + ",".join(sorted(direct_hits)))

    alias_hits = {
        term
        for term in expanded_terms
        if term and (term in candidate_term_set or term in candidate_compact)
    } - direct_hits
    if alias_hits:
        score = max(score, 0.74 + min(len(alias_hits), 3) * 0.04)
        reasons.append("alias:" + ",".join(sorted(alias_hits)[:5]))

    prefix_hits = {
        term
        for term in expanded_terms
        for candidate_term in candidate_term_set
        if len(term) >= 3
        and len(candidate_term) >= 3
        and (candidate_term.startswith(term) or term.startswith(candidate_term))
    }
    if prefix_hits:
        score = max(score, 0.52)
        reasons.append("prefix:" + ",".join(sorted(prefix_hits)[:5]))

    fuzzy_inputs = [query_compact, *query_terms, *sorted(expanded_terms)]
    fuzzy_score = max(
        (
            SequenceMatcher(None, term, candidate_compact).ratio()
            for term in fuzzy_inputs
            if term and candidate_compact
        ),
        default=0.0,
    )
    if fuzzy_score:
        score = max(score, fuzzy_score * 0.62)
        if fuzzy_score >= 0.35:
            reasons.append(f"fuzzy:{fuzzy_score:.2f}")

    return min(score, 1.0), reasons or ["weak_fuzzy"]


def _market_search_confidence(score: float) -> str:
    if score >= 0.74:
        return "high"
    if score >= 0.44:
        return "medium"
    return "low"


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
    # place_trigger_order params
    trigger_price: float | None = None,
    tpsl: Literal["tp", "sl"] | None = None,
    is_market_trigger: bool = True,
) -> dict[str, Any]:
    """Place orders, transfer collateral, or adjust leverage on Hyperliquid.

    Builder attribution is mandatory — every order routes through the Wayfinder builder wallet
    and the tool auto-approves the builder fee on first use.

    Actions:
      - `place_order`: spot, perp, or HIP-4 outcome market/limit.
          * Perp: `coin="BTC"` (or `"xyz:SP500"` HIP-3), `is_spot=False`.
          * Spot: `coin="@107"`, `is_spot=True`.
          * HIP-4 outcome: `coin="#<encoding>"` (e.g. `"#0"` NO / `"#1"` YES); integer `size`.
        Size by either `size` (coin units) or `usd_amount` (with `usd_amount_kind="notional"|"margin"` for perps).
      - `place_trigger_order`: TP/SL trigger. `tpsl="tp"|"sl"`, `trigger_price`, `is_buy` set to
        the side that closes the position (long → False, short → True).
      - `cancel_order`: by `order_id` or `cancel_cloid`.
      - `update_leverage`: set `leverage` and `is_cross` for an asset.
      - `deposit`: bridge `amount_usdc` from Arbitrum USDC into the HL perp account
        (≥ 5 USDC; below is lost). Auto-waits for the perp clearinghouse credit before returning.
      - `withdraw`: bridge `amount_usdc` from perp account back to Arbitrum.
      - `spot_to_perp_transfer` / `perp_to_spot_transfer`: shift `usd_amount` between sub-accounts.

    Args:
        wallet_label: Required — config.json wallet label.
        coin / asset_id: Symbol (e.g. "BTC", "@107" spot, "xyz:SP500" HIP-3, "#1" outcome) or numeric asset id.
        is_spot: Required for `place_order`; routes coin to the spot vs perp asset-id space.
        order_type: "market" or "limit"; `price` required for limit.
        size / usd_amount: Pick one. `usd_amount_kind` disambiguates perp notional vs margin.
        slippage: Market-order slippage cap (0.01 = 1%, max 0.25).
        reduce_only / cloid / order_id / cancel_cloid: Standard HL order flags / IDs.
        leverage / is_cross: Used by `update_leverage` and (optionally) pre-order leverage adjust.
        amount_usdc: USDC amount for `withdraw`.
        builder_fee_tenths_bp: Override builder fee (default from config or hardcoded constant).
        trigger_price / tpsl / is_market_trigger / price: Trigger-order parameters.
    """
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

    def _coin_from_asset_id(aid: int) -> str | None:
        for k, v in (adapter.coin_to_asset or {}).items():
            try:
                if v == aid:
                    return str(k)
            except Exception:
                continue
        return None

    # HIP-4 outcome orders: coin="#<encoding>" routes to the outcome asset-id
    # space (separate from spot/perp). Encoding = outcome_id * 10 + side.
    outcome_match = re.match(r"^#(\d+)$", (coin or "").strip())
    if action == "place_order" and outcome_match:
        encoding = int(outcome_match.group(1))
        outcome_id_v, side_v = encoding // 10, encoding % 10
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

    # spot/perp orders require explicit is_spot
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


async def hyperliquid_search_markets(
    query: str,
    market_type: Literal["perp", "spot", "both"] = "both",
    limit: int = 50,
    min_score: float = 0.0,
) -> str:
    """High-recall fuzzy search for Hyperliquid perp and spot markets.

    This is the preferred discovery tool when the user asks for market candidates. It returns
    compact candidate rows instead of the full HL universe, and it intentionally favors recall
    over precision: low-confidence rows are still useful candidates for follow-up filtering.

    Args:
        query: Market intent, symbol, or theme (e.g. "oil", "wti", "hype spot").
        market_type: "perp", "spot", or "both".
        limit: Maximum rows to return (default 50, hard-capped at 100).
        min_score: Optional score floor. Keep at 0.0 for maximum recall.
    """
    q = str(query or "").strip()
    if not q:
        return json.dumps(
            {
                "success": False,
                "error": "query is required",
                "query": q,
                "market_type": market_type,
            },
            indent=2,
        )
    if market_type not in {"perp", "spot", "both"}:
        return json.dumps(
            {
                "success": False,
                "error": "market_type must be one of: perp, spot, both",
                "query": q,
                "market_type": market_type,
            },
            indent=2,
        )

    try:
        limit_i = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit_i = 50
    try:
        min_score_f = max(0.0, min(float(min_score), 1.0))
    except (TypeError, ValueError):
        min_score_f = 0.0

    query_compact, query_terms, expanded_terms = _expanded_market_query(q)
    adapter = HyperliquidAdapter()

    perp_ok = spot_ok = True
    perp_data: Any = [{}, []]
    spot_assets: dict[str, int] = {}
    if market_type in {"perp", "both"}:
        perp_ok, perp_data = await adapter.get_meta_and_asset_ctxs()
    if market_type in {"spot", "both"}:
        spot_ok, spot_assets = await adapter.get_spot_assets()

    matches: list[dict[str, Any]] = []
    searched_counts = {"perp": 0, "spot": 0}

    if perp_ok and isinstance(perp_data, list) and perp_data:
        meta = perp_data[0] if isinstance(perp_data[0], dict) else {}
        universe = meta.get("universe", []) if isinstance(meta, dict) else []
        if isinstance(universe, list):
            searched_counts["perp"] = len(universe)
            for market in universe:
                if not isinstance(market, dict):
                    continue
                name = str(market.get("name") or "").strip()
                if not name:
                    continue
                score, reasons = _score_market_candidate(
                    query_compact=query_compact,
                    query_terms=query_terms,
                    expanded_terms=expanded_terms,
                    candidate_name=name,
                )
                if score < min_score_f:
                    continue
                matches.append(
                    {
                        "type": "perp",
                        "name": name,
                        "score": round(score, 4),
                        "confidence": _market_search_confidence(score),
                        "match_reasons": reasons,
                        "max_leverage": market.get("maxLeverage"),
                        "sz_decimals": market.get("szDecimals"),
                    }
                )

    if spot_ok and isinstance(spot_assets, dict):
        searched_counts["spot"] = len(spot_assets)
        for name, asset_id in spot_assets.items():
            score, reasons = _score_market_candidate(
                query_compact=query_compact,
                query_terms=query_terms,
                expanded_terms=expanded_terms,
                candidate_name=str(name),
            )
            if score < min_score_f:
                continue
            matches.append(
                {
                    "type": "spot",
                    "name": str(name),
                    "score": round(score, 4),
                    "confidence": _market_search_confidence(score),
                    "match_reasons": reasons,
                    "asset_id": asset_id,
                }
            )

    matches.sort(
        key=lambda row: (-float(row["score"]), str(row["type"]), str(row["name"]))
    )
    limited = matches[:limit_i]
    return json.dumps(
        {
            "success": bool(perp_ok and spot_ok),
            "query": q,
            "market_type": market_type,
            "count": len(limited),
            "total_candidates": len(matches),
            "searched_counts": searched_counts,
            "matches": limited,
            "errors": {
                "perp": None if perp_ok else str(perp_data),
                "spot": None if spot_ok else str(spot_assets),
            },
        },
        indent=2,
    )


async def hyperliquid_get_markets() -> str:
    """Return the full HL universe in one shot: perps (incl. HIP-3 builder dexes), spot, HIP-4 outcomes.

    `perp` already merges across every builder-deployed dex via `_post_across_dexes`, so HIP-3
    listings (e.g. `xyz:SP500`) appear alongside core perps. `outcomes` covers HIP-4 binary /
    multi-outcome markets.
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
    return json.dumps(
        {
            "perp": {"success": perp_ok, "markets": perp_data},
            "spot": {"success": spot_ok, "assets": spot_data},
            "outcomes": {"success": outcome_ok, "markets": outcome_data},
        },
        indent=2,
    )
