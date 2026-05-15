from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.constants.polymarket import POLYGON_CHAIN_ID
from wayfinder_paths.core.utils.wallets import (
    get_wallet_sign_hash_callback,
    get_wallet_sign_typed_data_callback,
    get_wallet_signing_callback,
)
from wayfinder_paths.mcp.preview import build_polymarket_execute_preview
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    catch_errors,
    err,
    normalize_address,
    ok,
    resolve_wallet_address,
    throw_if_empty_str,
    throw_if_none,
    throw_if_not_number,
)

_TRIM_MARKET_FIELDS: set[str] = {
    "id",
    "questionID",
    "image",
    "icon",
    "resolutionSource",
    "startDate",
    "startDateIso",
    "createdAt",
    "updatedAt",
    "marketMakerAddress",
    "new",
    "featured",
    "submitted_by",
    "archived",
    "resolvedBy",
    "restricted",
    "groupItemThreshold",
    "enableOrderBook",
    "hasReviewedDates",
    "volumeNum",
    "liquidityNum",
    "volume1wk",
    "volume1mo",
    "volume1yr",
    "volume24hrClob",
    "volume1wkClob",
    "volume1moClob",
    "volume1yrClob",
    "volumeClob",
    "liquidityClob",
    "umaBond",
    "umaReward",
    "umaResolutionStatus",
    "umaResolutionStatuses",
    "customLiveness",
    "negRisk",
    "negRiskMarketID",
    "negRiskRequestID",
    "ready",
    "funded",
    "acceptingOrdersTimestamp",
    "cyom",
    "competitive",
    "pagerDutyNotificationEnabled",
    "approved",
    "clobRewards",
    "rewardsMinSize",
    "rewardsMaxSpread",
    "automaticallyActive",
    "clearBookOnStart",
    "seriesColor",
    "showGmpSeries",
    "showGmpOutcome",
    "manualActivation",
    "negRiskOther",
    "pendingDeployment",
    "deploying",
    "deployingTimestamp",
    "rfqEnabled",
    "holdingRewardsEnabled",
    "feesEnabled",
    "requiresTranslation",
    "oneWeekPriceChange",
    "oneMonthPriceChange",
    "oneHourPriceChange",
}


def _trim_market(m: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in m.items() if k not in _TRIM_MARKET_FIELDS}
    desc = out.get("description") or ""
    if len(desc) > 300:
        out["description"] = desc[:300] + "…"
    if "events" in out:
        evt = out.pop("events")
        if evt:
            out["_event"] = {"slug": evt[0].get("slug"), "title": evt[0].get("title")}
    return out


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


@catch_errors
async def polymarket_get_state(
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
) -> dict[str, Any]:
    """Full Polymarket account state — positions, optional orders / activity / trades.

    With `wallet_label`, state is read from the derived deposit wallet. Without
    `wallet_label`, pass `account` or `wallet_address` directly.
    `include_orders` defaults to true; `include_activity` / `include_trades` default false
    to keep payloads tight. Each `*_limit` caps its respective list.
    """
    waddr, want = await resolve_wallet_address(wallet_label=wallet_label)
    if want and not waddr:
        return err("not_found", f"Unknown wallet_label: {want}")
    direct_account = normalize_address(account) or normalize_address(wallet_address)
    if not waddr and not direct_account:
        return err(
            "invalid_request",
            "account (or wallet_label/wallet_address) is required",
            {
                "wallet_label": wallet_label,
                "wallet_address": wallet_address,
                "account": account,
            },
        )

    sign_cb = None
    sign_hash_cb = None
    sign_typed_data_cb = None
    config: dict[str, Any] | None = None
    if want and waddr:
        sign_cb, _ = await get_wallet_signing_callback(want)
        sign_hash_cb, _ = await get_wallet_sign_hash_callback(want)
        sign_typed_data_cb, _ = await get_wallet_sign_typed_data_callback(want)
        config = dict(CONFIG)
        config["strategy_wallet"] = {"address": waddr}

    adapter = PolymarketAdapter(
        config=config,
        sign_callback=sign_cb,
        sign_hash_callback=sign_hash_cb,
        sign_typed_data_callback=sign_typed_data_cb,
        wallet_address=waddr,
    )
    try:
        acct = adapter.deposit_wallet_address() if waddr else direct_account
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
                "wallet_label": want,
                "account": acct,
                "ok": bool(ok_state),
                "state": state,
            }
        )
    finally:
        await adapter.close()


@catch_errors
async def polymarket_read(
    action: Literal[
        "search",
        "trending",
        "get_market",
        "get_event",
        "quote",
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
    # search/trending
    query: str | None = None,
    limit: int = 10,
    sort: Literal["trending", "volume24h", "liquidity", "fresh"] = "trending",
    status: Literal["active", "closed", "all"] = "active",
    offset: int = 0,
    # market/event
    market_slug: str | None = None,
    event_slug: str | None = None,
    outcome: str | int = "YES",
    # clob data
    token_id: str | None = None,
    side: Literal["BUY", "SELL"] = "BUY",
    amount_collateral: float | None = None,
    shares: float | None = None,
    interval: str | None = "1d",
    start_ts: int | None = None,
    end_ts: int | None = None,
    fidelity: int | None = None,
) -> dict[str, Any]:
    """Read-only Polymarket queries: market discovery, prices, books, history.

    For account state (positions / orders / activity / trades) call `polymarket_get_state`.

    Actions:
      - `search`: market search via vault-backend. Backend handles tag resolution, ticker
        synonyms (BTC↔Bitcoin), duration intent ("5 min" → 5-minute markets), ranking by
        relevance + activity + freshness. `sort`: trending|volume24h|liquidity|fresh.
        `status`: active|closed|all.
      - `trending`: list markets sorted by 24h volume (`limit`, `offset`).
      - `get_market` / `get_event`: fetch by `market_slug` / `event_slug`.
      - `quote`: market-order quote. BUY needs `amount_collateral` (USDC), SELL needs `shares`.
        Provide `market_slug`+`outcome` OR `token_id`.
      - `price`: best `BUY`/`SELL` price for a `token_id`.
      - `order_book`: full book for a `token_id`.
      - `price_history`: time series. `interval` ("1h"/"6h"/"1d"/"1w"/"max"), `start_ts`/`end_ts`
        (unix sec), `fidelity` (denser sampling for tight buckets).
      - `bridge_status`: pUSD bridge state for an account.
      - `open_orders`: requires Level-2 auth through the wallet hash-signing callback.

    Args:
        wallet_label / wallet_address / account: Target account; precedence is account >
            wallet_address > wallet_label-resolved address.
        outcome: "YES"/"NO" or numeric outcome index.
        side: "BUY" or "SELL".
        Other args: see action-specific descriptions above.
    """
    waddr, want = await resolve_wallet_address(wallet_label=wallet_label)

    acct = normalize_address(account) or normalize_address(wallet_address) or waddr

    if want and not waddr:
        return err("not_found", f"Unknown wallet_label: {want}")

    if action == "bridge_status" and not acct:
        return err(
            "invalid_request",
            "account (or wallet_label/wallet_address) is required",
            {
                "wallet_label": wallet_label,
                "wallet_address": wallet_address,
                "account": account,
            },
        )

    if action == "open_orders":
        throw_if_empty_str("wallet_label is required for open_orders", want)

    config: dict[str, Any] | None = None
    sign_cb = None
    sign_hash_cb = None
    sign_typed_data_cb = None
    if want and waddr:
        sign_cb, _ = await get_wallet_signing_callback(want)
        sign_hash_cb, _ = await get_wallet_sign_hash_callback(want)
        sign_typed_data_cb, _ = await get_wallet_sign_typed_data_callback(want)
        config = dict(CONFIG)
        config["strategy_wallet"] = {"address": waddr}

    adapter = PolymarketAdapter(
        config=config,
        sign_callback=sign_cb,
        sign_hash_callback=sign_hash_cb,
        sign_typed_data_callback=sign_typed_data_cb,
        wallet_address=waddr,
    )
    try:
        match action:
            case "search":
                q = throw_if_empty_str("query is required for search", query)
                ok_rows, rows = await adapter.search_markets(
                    query=q,
                    limit=int(limit),
                    sort=sort,
                    status=status,
                )
                if not ok_rows:
                    return err("error", str(rows))
                return ok({"action": action, "query": q, "markets": rows})

            case "trending":
                ok_rows, rows = await adapter.list_markets(
                    closed=False,
                    limit=int(limit),
                    offset=int(offset),
                    order="volume24hr",
                    ascending=False,
                )
                if not ok_rows:
                    return err("error", str(rows))
                return ok(
                    {"action": action, "markets": [_trim_market(m) for m in rows]}
                )

            case "get_market":
                slug = throw_if_empty_str("market_slug is required", market_slug)
                ok_m, m = await adapter.get_market_by_slug(slug)
                if not ok_m:
                    return err("error", str(m))
                return ok({"action": action, "market": m})

            case "get_event":
                slug = throw_if_empty_str("event_slug is required", event_slug)
                ok_e, e = await adapter.get_event_by_slug(slug)
                if not ok_e:
                    return err("error", str(e))
                return ok({"action": action, "event": e})

            case "quote":
                if side == "BUY":
                    throw_if_none(
                        "amount_collateral is required for BUY quote", amount_collateral
                    )
                    quote_amount = throw_if_not_number(
                        "amount_collateral must be a number", amount_collateral
                    )
                else:
                    throw_if_none("shares is required for SELL quote", shares)
                    quote_amount = throw_if_not_number(
                        "shares must be a number", shares
                    )

                if quote_amount <= 0:
                    raise ValueError("quote amount must be positive")

                slug = str(market_slug or "").strip()
                if slug:
                    ok_q, q = await adapter.quote_prediction(
                        market_slug=slug,
                        outcome=outcome,
                        side=side,
                        amount=quote_amount,
                    )
                else:
                    tid = str(token_id or "").strip()
                    if not tid:
                        raise ValueError("token_id or market_slug is required")
                    ok_q, q = await adapter.quote_market_order(
                        token_id=tid,
                        side=side,
                        amount=quote_amount,
                    )

                if not ok_q:
                    return err("error", str(q))
                return ok(
                    {
                        "action": action,
                        "token_id": q["token_id"],
                        "side": side,
                        "quote": q,
                    }
                )

            case "price":
                tid = throw_if_empty_str("token_id is required", token_id)
                ok_p, p = await adapter.get_price(token_id=tid, side=side)
                if not ok_p:
                    return err("error", str(p))
                return ok({"action": action, "token_id": tid, "side": side, "price": p})

            case "order_book":
                tid = throw_if_empty_str("token_id is required", token_id)
                ok_b, b = await adapter.get_order_book(token_id=tid)
                if not ok_b:
                    return err("error", str(b))
                return ok({"action": action, "token_id": tid, "book": b})

            case "price_history":
                tid = throw_if_empty_str("token_id is required", token_id)
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

            case "bridge_status":
                ok_s, s = await adapter.bridge_status(address=str(acct))
                if not ok_s:
                    return err("error", str(s))
                return ok({"action": action, "account": acct, "status": s})

            case "open_orders":
                if not want or not waddr:
                    return err("not_found", f"Unknown wallet_label: {wallet_label}")
                if not sign_hash_cb:
                    return err(
                        "invalid_wallet",
                        "Wallet must support hash signing to fetch open orders",
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
                        "account": adapter.deposit_wallet_address(),
                        "openOrders": orders,
                    }
                )

            case _:
                return err("invalid_request", f"Unknown polymarket action: {action}")
    finally:
        await adapter.close()


@catch_errors
async def polymarket_execute(
    action: Literal[
        "fund_deposit_wallet",
        "withdraw_deposit_wallet",
        "place_market_order",
        "place_limit_order",
        "cancel_order",
        "redeem_positions",
    ],
    *,
    wallet_label: str,
    # deposit-wallet move
    amount: float | None = None,
    # trade
    market_slug: str | None = None,
    outcome: str | int = "YES",
    token_id: str | None = None,
    amount_collateral: float | None = None,
    shares: float | None = None,
    # limit/cancel
    side: Literal["BUY", "SELL"] = "BUY",
    price: float | None = None,
    size: float | None = None,
    post_only: bool = False,
    order_id: str | None = None,
    # market-order slippage cap (percent). None = adapter default (2%).
    max_slippage_pct: float | None = None,
    # redeem
    condition_id: str | None = None,
) -> dict[str, Any]:
    """Execute Polymarket trades on the owner EOA / deposit wallet.

    Polymarket V2 settles on Polygon in pUSD (`0xC011a7...`). This tool handles the
    deposit-wallet + CLOB surface. For collateral routing in/out of pUSD, use the BRAP
    swap MCP tools (see "Routing collateral" below).

    Actions:
      - `fund_deposit_wallet`: move `amount` pUSD from the owner EOA into the derived deposit
        wallet (Polygon transfer). Required before trading.
      - `withdraw_deposit_wallet`: pull pUSD from the deposit wallet back to the owner EOA
        via the relayer. Omit `amount` to withdraw the full balance.
      - `place_market_order`: market order. Specify `market_slug`+`outcome` OR `token_id`, with
        `side="BUY"|"SELL"`. BUY needs `amount_collateral` (pUSD); SELL needs `shares`. Adapter
        quotes the book and signs an FOK limit at `worst_price * (1 ± max_slippage_pct/100)`
        (default 2%); order is killed if the book moves past the cap.
      - `place_limit_order`: requires `token_id`, `side`, `price`, `size`. `post_only` = maker-only.
      - `cancel_order`: by `order_id`.
      - `redeem_positions`: claim winnings on a resolved market by `condition_id`. USDC.e
        proceeds are auto-wrapped 1:1 to pUSD via BRAP's polymarket_bridge solver through
        the deposit wallet.

    Routing collateral (in/out of pUSD): use `onchain_quote_swap` then
    `core_execute(kind="swap", ...)` with `to_token` or `from_token` set to pUSD
    (`polygon_0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`). BRAP picks the right solver
    (`polymarket_bridge` for USDC.e ↔ pUSD 1:1 wraps; standard DEX routes for everything else)
    and handles cross-chain. Works for any source token/chain; no Polymarket-specific call needed.

    Args:
        wallet_label: Required.
        outcome: "YES"/"NO" or numeric index (default "YES").
        side: "BUY" or "SELL" for limit orders.
        Other args: see action-specific descriptions above.
    """
    sign_callback, sender = await get_wallet_signing_callback(wallet_label or "")
    sign_hash_cb, _ = await get_wallet_sign_hash_callback(wallet_label or "")
    sign_typed_data_cb, _ = await get_wallet_sign_typed_data_callback(
        wallet_label or ""
    )
    want = wallet_label

    tool_input = {
        "action": action,
        "wallet_label": want,
        "amount": amount,
        "market_slug": market_slug,
        "outcome": outcome,
        "token_id": token_id,
        "amount_collateral": amount_collateral,
        "shares": shares,
        "side": side,
        "price": price,
        "size": size,
        "post_only": post_only,
        "order_id": order_id,
        "max_slippage_pct": max_slippage_pct,
        "condition_id": condition_id,
    }
    preview_obj = await build_polymarket_execute_preview(tool_input)
    preview_text = str(preview_obj.get("summary") or "").strip()
    if preview_obj.get("recipient_mismatch"):
        preview_text = "⚠ RECIPIENT DIFFERS FROM SENDER\n" + preview_text

    cfg = dict(CONFIG)
    cfg["main_wallet"] = {"address": sender}
    cfg["strategy_wallet"] = {"address": sender}

    effects: list[dict[str, Any]] = []
    adapter = PolymarketAdapter(
        config=cfg,
        sign_callback=sign_callback,
        sign_hash_callback=sign_hash_cb,
        sign_typed_data_callback=sign_typed_data_cb,
        wallet_address=sender,
    )
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

        match action:
            case "fund_deposit_wallet":
                throw_if_none("amount is required for fund_deposit_wallet", amount)
                ok_fund, res = await adapter.fund_deposit_wallet(
                    amount_raw=int(Decimal(str(amount)) * Decimal(1_000_000))
                )
                effects.append(
                    {
                        "type": "polymarket",
                        "label": "fund_deposit_wallet",
                        "ok": ok_fund,
                        "result": res,
                    }
                )
                status = "confirmed" if ok_fund else "failed"
                _annotate(
                    address=sender,
                    label=want,
                    action="fund_deposit_wallet",
                    status=status,
                    chain_id=POLYGON_CHAIN_ID,
                    details={"amount": float(amount)},
                )
                return _done(status)

            case "withdraw_deposit_wallet":
                ok_w, res = await adapter.withdraw_deposit_wallet(
                    amount_raw=int(Decimal(str(amount)) * Decimal(1_000_000))
                    if amount is not None
                    else None
                )
                effects.append(
                    {
                        "type": "polymarket",
                        "label": "withdraw_deposit_wallet",
                        "ok": ok_w,
                        "result": res,
                    }
                )
                status = "confirmed" if ok_w else "failed"
                _annotate(
                    address=sender,
                    label=want,
                    action="withdraw_deposit_wallet",
                    status=status,
                    chain_id=POLYGON_CHAIN_ID,
                    details={"amount": float(amount) if amount is not None else None},
                )
                return _done(status)

            case "place_market_order":
                if side == "BUY":
                    throw_if_none(
                        "amount_collateral is required for BUY", amount_collateral
                    )
                else:
                    throw_if_none("shares is required for SELL", shares)
                if market_slug:
                    if side == "BUY":
                        ok_trade, res = await adapter.place_prediction(
                            market_slug=str(market_slug),
                            outcome=outcome,
                            amount_collateral=float(amount_collateral),
                            max_slippage_pct=max_slippage_pct,
                        )
                    else:
                        ok_trade, res = await adapter.cash_out_prediction(
                            market_slug=str(market_slug),
                            outcome=outcome,
                            shares=float(shares),
                            max_slippage_pct=max_slippage_pct,
                        )
                else:
                    tid = throw_if_empty_str(
                        "token_id or market_slug is required", token_id
                    )
                    ok_trade, res = await adapter.place_market_order(
                        token_id=tid,
                        side=side,
                        amount=float(amount_collateral if side == "BUY" else shares),
                        max_slippage_pct=max_slippage_pct,
                    )

                effects.append(
                    {
                        "type": "polymarket",
                        "label": "place_market_order",
                        "ok": ok_trade,
                        "result": res,
                    }
                )
                status = "confirmed" if ok_trade else "failed"
                _annotate(
                    address=sender,
                    label=want,
                    action="place_market_order",
                    status=status,
                    chain_id=int(POLYGON_CHAIN_ID),
                    details={
                        "market_slug": str(market_slug) if market_slug else None,
                        "token_id": str(token_id) if token_id else None,
                        "outcome": str(outcome),
                        "side": side,
                        "amount_collateral": float(amount_collateral)
                        if amount_collateral is not None
                        else None,
                        "shares": float(shares) if shares is not None else None,
                        "max_slippage_pct": float(max_slippage_pct)
                        if max_slippage_pct is not None
                        else None,
                    },
                )
                return _done(status)

            case "place_limit_order":
                tid = throw_if_empty_str(
                    "token_id is required for place_limit_order", token_id
                )
                throw_if_none("price is required for place_limit_order", price)
                throw_if_none("size is required for place_limit_order", size)
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

            case "cancel_order":
                oid = throw_if_empty_str(
                    "order_id is required for cancel_order", order_id
                )
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

            case "redeem_positions":
                cid = throw_if_empty_str(
                    "condition_id is required for redeem_positions", condition_id
                )
                ok_r, res = await adapter.redeem_positions(condition_id=cid)
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

            case _:
                return err(
                    "invalid_request", f"Unknown polymarket_execute action: {action}"
                )
    finally:
        await adapter.close()
