from __future__ import annotations

from typing import Any, Literal

from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.constants.polymarket import (
    POLYGON_CHAIN_ID,
    POLYGON_USDC_ADDRESS,
)
from wayfinder_paths.mcp.preview import build_polymarket_execute_preview
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    err,
    find_wallet_by_label,
    normalize_address,
    ok,
)


def _resolve_wallet(
    *, wallet_label: str | None
) -> tuple[str | None, str | None, str | None]:
    want = (wallet_label or "").strip()
    if not want:
        return None, None, None
    w = find_wallet_by_label(want)
    if not w:
        return want, None, None
    addr = normalize_address(w.get("address"))
    pk = (
        (w.get("private_key") or w.get("private_key_hex"))
        if isinstance(w, dict)
        else None
    )
    pk_s = str(pk).strip() if pk else None
    return want, addr, pk_s


def _annotate(
    *,
    address: str,
    label: str,
    action: str,
    status: str,
    chain_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    WalletProfileStore.default().annotate_safe(
        address=address,
        label=label,
        protocol="polymarket",
        action=action,
        tool="polymarket_execute",
        status=status,
        chain_id=chain_id,
        details=details,
    )


async def polymarket(
    action: Literal[
        "status",
        "search",
        "trending",
        "get_market",
        "get_event",
        "price",
        "order_book",
        "price_history",
        "bridge_status",
        "open_orders",
    ],
    *,
    wallet_label: str | None = None,
    wallet_address: str | None = None,
    account: str | None = None,
    include_orders: bool = True,
    include_activity: bool = False,
    activity_limit: int = 50,
    include_trades: bool = False,
    trades_limit: int = 50,
    positions_limit: int = 500,
    max_positions_pages: int = 10,
    # search/trending
    query: str | None = None,
    limit: int = 10,
    page: int = 1,
    keep_closed_markets: bool = False,
    rerank: bool = True,
    offset: int = 0,
    # market/event
    market_slug: str | None = None,
    event_slug: str | None = None,
    # clob data
    token_id: str | None = None,
    side: Literal["BUY", "SELL"] = "BUY",
    interval: str | None = "1d",
    start_ts: int | None = None,
    end_ts: int | None = None,
    fidelity: int | None = None,
) -> dict[str, Any]:
    want, waddr, _pk = _resolve_wallet(wallet_label=wallet_label)

    acct = normalize_address(account) or normalize_address(wallet_address) or waddr

    if (wallet_label or "").strip() and want and not waddr:
        return err("not_found", f"Unknown wallet_label: {want}")

    # Most actions require an account; search/data-only actions do not.
    if action in {"status", "bridge_status"} and not acct:
        return err(
            "invalid_request",
            "account (or wallet_label/wallet_address) is required",
            {
                "wallet_label": wallet_label,
                "wallet_address": wallet_address,
                "account": account,
            },
        )

    if action == "open_orders" and not want:
        return err("invalid_request", "wallet_label is required for open_orders")

    config: dict[str, Any] | None = None
    if want and waddr:
        config = dict(CONFIG)
        wobj: dict[str, Any] = {"address": waddr}
        if _pk:
            wobj["private_key_hex"] = _pk
        config["strategy_wallet"] = wobj

    adapter = PolymarketAdapter(config=config)
    try:
        if action == "status":
            ok_state, state = await adapter.get_full_user_state(
                account=str(acct),
                include_orders=bool(include_orders),
                include_activity=bool(include_activity),
                activity_limit=int(activity_limit),
                include_trades=bool(include_trades),
                trades_limit=int(trades_limit),
                positions_limit=int(positions_limit),
                max_positions_pages=int(max_positions_pages),
            )
            return ok(
                {
                    "action": action,
                    "wallet_label": want,
                    "account": acct,
                    "ok": bool(ok_state),
                    "state": state,
                }
            )

        if action == "search":
            q = str(query or "").strip()
            if not q:
                return err("invalid_request", "query is required for search")
            ok_rows, rows = await adapter.search_markets_fuzzy(
                query=q,
                limit=int(limit),
                page=int(page),
                keep_closed_markets=bool(keep_closed_markets),
                rerank=bool(rerank),
            )
            if not ok_rows:
                return err("error", str(rows))
            return ok({"action": action, "query": q, "markets": rows})

        if action == "trending":
            ok_rows, rows = await adapter.list_markets(
                closed=False,
                limit=int(limit),
                offset=int(offset),
                order="volume24hr",
                ascending=False,
            )
            if not ok_rows:
                return err("error", str(rows))
            return ok({"action": action, "markets": rows})

        if action == "get_market":
            slug = str(market_slug or "").strip()
            if not slug:
                return err("invalid_request", "market_slug is required")
            ok_m, m = await adapter.get_market_by_slug(slug)
            if not ok_m:
                return err("error", str(m))
            return ok({"action": action, "market": m})

        if action == "get_event":
            slug = str(event_slug or "").strip()
            if not slug:
                return err("invalid_request", "event_slug is required")
            ok_e, e = await adapter.get_event_by_slug(slug)
            if not ok_e:
                return err("error", str(e))
            return ok({"action": action, "event": e})

        if action == "price":
            tid = str(token_id or "").strip()
            if not tid:
                return err("invalid_request", "token_id is required")
            ok_p, p = await adapter.get_price(token_id=tid, side=side)
            if not ok_p:
                return err("error", str(p))
            return ok({"action": action, "token_id": tid, "side": side, "price": p})

        if action == "order_book":
            tid = str(token_id or "").strip()
            if not tid:
                return err("invalid_request", "token_id is required")
            ok_b, b = await adapter.get_order_book(token_id=tid)
            if not ok_b:
                return err("error", str(b))
            return ok({"action": action, "token_id": tid, "book": b})

        if action == "price_history":
            tid = str(token_id or "").strip()
            if not tid:
                return err("invalid_request", "token_id is required")
            ok_h, h = await adapter.get_prices_history(
                token_id=tid,
                interval=interval,
                start_ts=start_ts,
                end_ts=end_ts,
                fidelity=fidelity,
            )
            if not ok_h:
                return err("error", str(h))
            return ok({"action": action, "token_id": tid, "history": h})

        if action == "bridge_status":
            ok_s, s = await adapter.bridge_status(address=str(acct))
            if not ok_s:
                return err("error", str(s))
            return ok({"action": action, "account": acct, "status": s})

        if action == "open_orders":
            if not want or not waddr:
                return err("not_found", f"Unknown wallet_label: {wallet_label}")
            if not _pk:
                return err(
                    "invalid_wallet",
                    "Wallet must include private_key_hex in config.json to fetch open orders",
                    {"wallet_label": want},
                )
            # Open orders require Level-2 auth and the signing wallet in config.
            ok_o, orders = await adapter.list_open_orders(token_id=token_id)
            if not ok_o:
                return err("error", str(orders))
            return ok(
                {
                    "action": action,
                    "wallet_label": want,
                    "account": waddr,
                    "openOrders": orders,
                }
            )

        return err("invalid_request", f"Unknown polymarket action: {action}")
    finally:
        await adapter.close()


async def polymarket_execute(
    action: Literal[
        "bridge_deposit",
        "bridge_withdraw",
        "buy",
        "sell",
        "close_position",
        "place_limit_order",
        "cancel_order",
        "redeem_positions",
    ],
    *,
    wallet_label: str,
    # bridge
    from_chain_id: int = POLYGON_CHAIN_ID,
    from_token_address: str = POLYGON_USDC_ADDRESS,
    amount: float | None = None,
    recipient_address: str | None = None,
    amount_usdce: float | None = None,
    to_chain_id: int = POLYGON_CHAIN_ID,
    to_token_address: str = POLYGON_USDC_ADDRESS,
    recipient_addr: str | None = None,
    token_decimals: int = 6,
    # trade
    market_slug: str | None = None,
    outcome: str | int = "YES",
    token_id: str | None = None,
    amount_usdc: float | None = None,
    shares: float | None = None,
    # limit/cancel
    side: Literal["BUY", "SELL"] = "BUY",
    price: float | None = None,
    size: float | None = None,
    post_only: bool = False,
    order_id: str | None = None,
    # redeem
    condition_id: str | None = None,
) -> dict[str, Any]:
    want, sender, pk = _resolve_wallet(wallet_label=wallet_label)
    if not want:
        return err("invalid_request", "wallet_label is required")
    if not sender or not pk:
        return err(
            "invalid_wallet",
            "Wallet must include address and private_key_hex in config.json (local dev only)",
            {"wallet_label": want},
        )

    tool_input = {
        "action": action,
        "wallet_label": want,
        "from_chain_id": from_chain_id,
        "from_token_address": from_token_address,
        "amount": amount,
        "recipient_address": recipient_address,
        "amount_usdce": amount_usdce,
        "to_chain_id": to_chain_id,
        "to_token_address": to_token_address,
        "recipient_addr": recipient_addr,
        "token_decimals": token_decimals,
        "market_slug": market_slug,
        "outcome": outcome,
        "token_id": token_id,
        "amount_usdc": amount_usdc,
        "shares": shares,
        "side": side,
        "price": price,
        "size": size,
        "post_only": post_only,
        "order_id": order_id,
        "condition_id": condition_id,
    }
    preview_obj = build_polymarket_execute_preview(tool_input)
    preview_text = str(preview_obj.get("summary") or "").strip()
    if preview_obj.get("recipient_mismatch"):
        preview_text = "⚠ RECIPIENT DIFFERS FROM SENDER\n" + preview_text

    cfg = dict(CONFIG)
    cfg["main_wallet"] = {"address": sender, "private_key_hex": pk}
    cfg["strategy_wallet"] = {"address": sender, "private_key_hex": pk}

    effects: list[dict[str, Any]] = []
    adapter = PolymarketAdapter(config=cfg)
    try:

        def _done(status: str) -> dict[str, Any]:
            return ok(
                {
                    "status": status,
                    "action": action,
                    "wallet_label": want,
                    "address": sender,
                    "preview": preview_text,
                    "effects": effects,
                }
            )

        if action == "bridge_deposit":
            if amount is None:
                return err("invalid_request", "amount is required for bridge_deposit")
            rcpt = normalize_address(recipient_address) or sender
            ok_dep, res = await adapter.bridge_deposit(
                from_chain_id=int(from_chain_id),
                from_token_address=str(from_token_address),
                amount=float(amount),
                recipient_address=str(rcpt),
                token_decimals=int(token_decimals),
            )
            effects.append(
                {
                    "type": "polymarket",
                    "label": "bridge_deposit",
                    "ok": ok_dep,
                    "result": res,
                }
            )
            status = "confirmed" if ok_dep else "failed"
            _annotate(
                address=sender,
                label=want,
                action="bridge_deposit",
                status=status,
                chain_id=int(from_chain_id),
                details={
                    "amount": float(amount),
                    "from_token_address": str(from_token_address),
                    "recipient_address": str(rcpt),
                },
            )
            return _done(status)

        if action == "bridge_withdraw":
            if amount_usdce is None:
                return err(
                    "invalid_request", "amount_usdce is required for bridge_withdraw"
                )
            rcpt = normalize_address(recipient_addr) or sender
            ok_wd, res = await adapter.bridge_withdraw(
                amount_usdce=float(amount_usdce),
                to_chain_id=int(to_chain_id),
                to_token_address=str(to_token_address),
                recipient_addr=str(rcpt),
                token_decimals=int(token_decimals),
            )
            effects.append(
                {
                    "type": "polymarket",
                    "label": "bridge_withdraw",
                    "ok": ok_wd,
                    "result": res,
                }
            )
            status = "confirmed" if ok_wd else "failed"
            _annotate(
                address=sender,
                label=want,
                action="bridge_withdraw",
                status=status,
                chain_id=int(POLYGON_CHAIN_ID),
                details={
                    "amount_usdce": float(amount_usdce),
                    "to_chain_id": int(to_chain_id),
                    "to_token_address": str(to_token_address),
                    "recipient_addr": str(rcpt),
                },
            )
            return _done(status)

        if action in {"buy", "sell"}:
            if market_slug:
                if action == "buy":
                    if amount_usdc is None:
                        return err("invalid_request", "amount_usdc is required for buy")
                    ok_trade, res = await adapter.place_prediction(
                        market_slug=str(market_slug),
                        outcome=outcome,
                        amount_usdc=float(amount_usdc),
                    )
                else:
                    if shares is None:
                        return err("invalid_request", "shares is required for sell")
                    ok_trade, res = await adapter.cash_out_prediction(
                        market_slug=str(market_slug),
                        outcome=outcome,
                        shares=float(shares),
                    )
            else:
                tid = str(token_id or "").strip()
                if not tid:
                    return err("invalid_request", "token_id or market_slug is required")
                if action == "buy":
                    if amount_usdc is None:
                        return err("invalid_request", "amount_usdc is required for buy")
                    ok_trade, res = await adapter.place_market_order(
                        token_id=tid,
                        side="BUY",
                        amount=float(amount_usdc),
                    )
                else:
                    if shares is None:
                        return err("invalid_request", "shares is required for sell")
                    ok_trade, res = await adapter.place_market_order(
                        token_id=tid,
                        side="SELL",
                        amount=float(shares),
                    )

            effects.append(
                {"type": "polymarket", "label": action, "ok": ok_trade, "result": res}
            )
            status = "confirmed" if ok_trade else "failed"
            _annotate(
                address=sender,
                label=want,
                action=action,
                status=status,
                chain_id=int(POLYGON_CHAIN_ID),
                details={
                    "market_slug": str(market_slug) if market_slug else None,
                    "token_id": str(token_id) if token_id else None,
                    "outcome": str(outcome),
                    "amount_usdc": float(amount_usdc)
                    if amount_usdc is not None
                    else None,
                    "shares": float(shares) if shares is not None else None,
                },
            )
            return _done(status)

        if action == "close_position":
            # Convenience: sell the full size from Data API positions.
            tid = str(token_id or "").strip()

            if not tid and market_slug:
                ok_m, market = await adapter.get_market_by_slug(str(market_slug))
                if not ok_m or not isinstance(market, dict):
                    return err("not_found", f"Market not found: {market_slug}")
                ok_tid, tid_or_err = adapter.resolve_clob_token_id(
                    market=market, outcome=outcome
                )
                if not ok_tid:
                    return err("invalid_request", str(tid_or_err))
                tid = str(tid_or_err)

            if not tid and condition_id:
                ok_pos, pos = await adapter.get_positions(
                    user=sender, limit=500, offset=0
                )
                if ok_pos and isinstance(pos, list):
                    for p in pos:
                        if not isinstance(p, dict):
                            continue
                        if (
                            str(p.get("conditionId") or "").lower()
                            == str(condition_id).lower()
                        ):
                            tid = str(p.get("asset") or "").strip()
                            if tid:
                                break

            if not tid:
                return err(
                    "invalid_request",
                    "Provide token_id, or market_slug+outcome, or condition_id for close_position",
                )

            sell_shares = shares
            if sell_shares is None:
                ok_pos, pos = await adapter.get_positions(
                    user=sender, limit=500, offset=0
                )
                if not ok_pos:
                    return err("error", f"Failed to fetch positions: {pos}")
                if not isinstance(pos, list):
                    return err("error", "Unexpected positions response")
                match = next(
                    (
                        p
                        for p in pos
                        if isinstance(p, dict)
                        and str(p.get("asset") or "").strip() == tid
                    ),
                    None,
                )
                if not match:
                    return err("not_found", "No matching position found to close")
                try:
                    sell_shares = float(match.get("size") or 0)
                except (TypeError, ValueError):
                    sell_shares = 0.0
            if not sell_shares or float(sell_shares) <= 0:
                return err("invalid_request", "No shares available to close")

            ok_sell, res = await adapter.place_market_order(
                token_id=str(tid),
                side="SELL",
                amount=float(sell_shares),
            )
            effects.append(
                {
                    "type": "polymarket",
                    "label": "close_position",
                    "ok": ok_sell,
                    "result": res,
                }
            )
            status = "confirmed" if ok_sell else "failed"
            _annotate(
                address=sender,
                label=want,
                action="close_position",
                status=status,
                chain_id=int(POLYGON_CHAIN_ID),
                details={"token_id": str(tid), "shares": float(sell_shares)},
            )
            return _done(status)

        if action == "place_limit_order":
            tid = str(token_id or "").strip()
            if not tid:
                return err(
                    "invalid_request", "token_id is required for place_limit_order"
                )
            if price is None or size is None:
                return err(
                    "invalid_request",
                    "price and size are required for place_limit_order",
                )
            ok_lo, res = await adapter.place_limit_order(
                token_id=tid,
                side=side,
                price=float(price),
                size=float(size),
                post_only=bool(post_only),
            )
            effects.append(
                {
                    "type": "polymarket",
                    "label": "place_limit_order",
                    "ok": ok_lo,
                    "result": res,
                }
            )
            status = "confirmed" if ok_lo else "failed"
            _annotate(
                address=sender,
                label=want,
                action="place_limit_order",
                status=status,
                chain_id=int(POLYGON_CHAIN_ID),
                details={
                    "token_id": tid,
                    "side": side,
                    "price": float(price),
                    "size": float(size),
                    "post_only": bool(post_only),
                },
            )
            return _done(status)

        if action == "cancel_order":
            oid = str(order_id or "").strip()
            if not oid:
                return err("invalid_request", "order_id is required for cancel_order")
            ok_c, res = await adapter.cancel_order(order_id=oid)
            effects.append(
                {
                    "type": "polymarket",
                    "label": "cancel_order",
                    "ok": ok_c,
                    "result": res,
                }
            )
            status = "confirmed" if ok_c else "failed"
            _annotate(
                address=sender,
                label=want,
                action="cancel_order",
                status=status,
                chain_id=int(POLYGON_CHAIN_ID),
                details={"order_id": oid},
            )
            return _done(status)

        if action == "redeem_positions":
            cid = str(condition_id or "").strip()
            if not cid:
                return err(
                    "invalid_request", "condition_id is required for redeem_positions"
                )
            ok_r, res = await adapter.redeem_positions(condition_id=cid, holder=sender)
            effects.append(
                {
                    "type": "polymarket",
                    "label": "redeem_positions",
                    "ok": ok_r,
                    "result": res,
                }
            )
            status = "confirmed" if ok_r else "failed"
            _annotate(
                address=sender,
                label=want,
                action="redeem_positions",
                status=status,
                chain_id=int(POLYGON_CHAIN_ID),
                details={"condition_id": cid},
            )
            return _done(status)

        return err("invalid_request", f"Unknown polymarket_execute action: {action}")
    finally:
        await adapter.close()
