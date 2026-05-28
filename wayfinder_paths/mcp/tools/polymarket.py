from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Literal

from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.core.clients.PolymarketClient import (
    PolymarketSort,
    PolymarketStatus,
)
from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.constants.polymarket import (
    POLYGON_CHAIN_ID,
    POLYGON_P_USDC_PROXY_ADDRESS,
)
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.core.utils.wallets import (
    get_wallet_sign_hash_callback,
    get_wallet_sign_typed_data_callback,
    get_wallet_signing_callback,
)
from wayfinder_paths.mcp.polymarket_order import (
    normalize_pm_execution_summary,
    normalize_pm_side,
    validate_pm_market_order_size,
)
from wayfinder_paths.mcp.polymarket_summary import (
    DEFAULT_CANDIDATE_LIMIT,
    compact_candidates,
    compact_event,
    compact_event_groups,
    compact_market_detail,
    compact_order_book,
    compact_truncation,
    next_suggested_calls,
)
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


def _adapter_error(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("error") or payload)
        return err(str(payload.get("code") or "error"), message, payload)
    return err("error", str(payload))


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
        tool=f"polymarket_{action}",
        status=status,
        chain_id=chain_id,
        details=details,
    )


async def _make_polymarket_adapter(
    wallet_label: str,
) -> tuple[PolymarketAdapter, str]:
    """Resolve signing callbacks + build a wallet-bound PolymarketAdapter."""
    (
        (sign_callback, sender),
        (sign_hash_cb, _),
        (sign_typed_data_cb, _),
    ) = await asyncio.gather(
        get_wallet_signing_callback(wallet_label),
        get_wallet_sign_hash_callback(wallet_label),
        get_wallet_sign_typed_data_callback(wallet_label),
    )

    cfg = dict(CONFIG)
    cfg["main_wallet"] = {"address": sender}
    cfg["strategy_wallet"] = {"address": sender}

    adapter = PolymarketAdapter(
        config=cfg,
        sign_callback=sign_callback,
        sign_hash_callback=sign_hash_cb,
        sign_typed_data_callback=sign_typed_data_cb,
        wallet_address=sender,
    )
    return adapter, sender


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
    sort: PolymarketSort = "trending",
    status: PolymarketStatus = "active",
    offset: int = 0,
    summary: bool = True,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    # market/event
    market_slug: str | None = None,
    event_slug: str | None = None,
    outcome: str | int = "YES",
    # clob data
    token_id: str | None = None,
    side: Literal["BUY", "SELL"] = "BUY",
    buy_amount_pusd: float | None = None,
    sell_amount_shares: float | None = None,
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
        These discovery actions return compact candidates by default; pass
        `summary=False` only when debugging raw Gamma/backend payloads.
      - `quote`: market-order quote. BUY needs `buy_amount_pusd`; SELL needs
        `sell_amount_shares`. Results include a normalized execution summary.
        Provide `market_slug`+`outcome` OR `token_id`.
      - `price`: best `BUY`/`SELL` price for a `token_id`.
      - `order_book`: compact book summary for a `token_id`; pass `summary=False`
        for the raw full book.
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
                    return _adapter_error(rows)
                if summary:
                    candidates, truncation = compact_candidates(rows, candidate_limit)
                    event_groups = compact_event_groups(rows)
                    return ok(
                        {
                            "action": action,
                            "query": q,
                            "summaryMode": True,
                            "candidates": candidates,
                            "eventGroups": event_groups,
                            "nextSuggestedCalls": next_suggested_calls(
                                event_groups=event_groups,
                                truncation=truncation,
                            ),
                            "truncation": truncation,
                        }
                    )
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
                    return _adapter_error(rows)
                if summary:
                    candidates, truncation = compact_candidates(rows, candidate_limit)
                    event_groups = compact_event_groups(rows)
                    return ok(
                        {
                            "action": action,
                            "summaryMode": True,
                            "candidates": candidates,
                            "eventGroups": event_groups,
                            "nextSuggestedCalls": next_suggested_calls(
                                event_groups=event_groups,
                                truncation=truncation,
                            ),
                            "truncation": truncation,
                        }
                    )
                return ok({"action": action, "markets": rows})

            case "get_market":
                slug = throw_if_empty_str("market_slug is required", market_slug)
                ok_m, m = await adapter.get_market_by_slug(slug)
                if not ok_m:
                    return _adapter_error(m)
                if summary:
                    return ok(
                        {
                            "action": action,
                            "summaryMode": True,
                            "market": compact_market_detail(m),
                            "truncation": compact_truncation(1, 1),
                        }
                    )
                return ok({"action": action, "market": m})

            case "get_event":
                slug = throw_if_empty_str("event_slug is required", event_slug)
                ok_e, e = await adapter.get_event_by_slug(slug)
                if not ok_e:
                    return _adapter_error(e)
                if summary:
                    markets = [m for m in e.get("markets", []) if isinstance(m, dict)]
                    candidates, truncation = compact_candidates(
                        markets,
                        candidate_limit,
                        event_slug_override=slug,
                        sort_open_first=True,
                    )
                    return ok(
                        {
                            "action": action,
                            "summaryMode": True,
                            "event": compact_event(e),
                            "candidates": candidates,
                            "nextSuggestedCalls": next_suggested_calls(
                                event_slug_value=slug,
                                truncation=truncation,
                            ),
                            "truncation": truncation,
                        }
                    )
                return ok({"action": action, "event": e})

            case "quote":
                side = normalize_pm_side(side)
                sizing = validate_pm_market_order_size(
                    side=side,
                    buy_amount_pusd=buy_amount_pusd,
                    sell_amount_shares=sell_amount_shares,
                )

                slug = str(market_slug or "").strip()
                if slug:
                    ok_q, q = await adapter.quote_prediction(
                        market_slug=slug,
                        outcome=outcome,
                        side=side,
                        amount=sizing["adapter_amount"],
                    )
                else:
                    tid = str(token_id or "").strip()
                    if not tid:
                        raise ValueError("token_id or market_slug is required")
                    ok_q, q = await adapter.quote_market_order(
                        token_id=tid,
                        side=side,
                        amount=sizing["adapter_amount"],
                    )

                if not ok_q:
                    return _adapter_error(q)
                execution_summary = normalize_pm_execution_summary(
                    side=side,
                    sizing=sizing,
                    quote=q if isinstance(q, dict) else None,
                )
                return ok(
                    {
                        "action": action,
                        "token_id": q["token_id"],
                        "side": side,
                        "sizing_kind": sizing["sizing_kind"],
                        "buy_amount_pusd": sizing["buy_amount_pusd"],
                        "sell_amount_shares": sizing["sell_amount_shares"],
                        "executionSummary": execution_summary,
                        "quote": q,
                    }
                )

            case "price":
                tid = throw_if_empty_str("token_id is required", token_id)
                ok_p, p = await adapter.get_price(token_id=tid, side=side)
                if not ok_p:
                    return _adapter_error(p)
                return ok({"action": action, "token_id": tid, "side": side, "price": p})

            case "order_book":
                tid = throw_if_empty_str("token_id is required", token_id)
                ok_b, b = await adapter.get_order_book(token_id=tid)
                if not ok_b:
                    return _adapter_error(b)
                if summary:
                    return ok(
                        {
                            "action": action,
                            "token_id": tid,
                            "summaryMode": True,
                            "book": compact_order_book(b),
                        }
                    )
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
                    return _adapter_error(h)
                return ok({"action": action, "token_id": tid, "history": h})

            case "bridge_status":
                ok_s, s = await adapter.bridge_status(address=str(acct))
                if not ok_s:
                    return _adapter_error(s)
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
                    return _adapter_error(orders)
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
async def polymarket_deposit_pusd(
    *,
    wallet_label: str,
    amount: float,
) -> dict[str, Any]:
    """Move pUSD from the owner EOA into the derived Polymarket V2 deposit wallet.

    Required before any trade — Polymarket settles from the deposit wallet, not the EOA. Direct Polygon ERC20 transfer; owner pays POL gas.

    Args:
        wallet_label: Owner EOA wallet.
        amount: pUSD to deposit, in human units (e.g. 10.5).
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    throw_if_none("amount is required", amount)
    amt = throw_if_not_number("amount must be a number", amount)
    adapter, sender = await _make_polymarket_adapter(wallet_label)
    try:
        amount_raw = int(Decimal(str(amt)) * Decimal(1_000_000))
        pusd_balance = await get_token_balance(
            POLYGON_P_USDC_PROXY_ADDRESS,
            POLYGON_CHAIN_ID,
            sender,
            block_identifier="latest",
        )
        if pusd_balance < amount_raw:
            return err(
                "insufficient_pusd",
                f"Owner EOA has {pusd_balance / 1_000_000:.6f} pUSD, need "
                f"{amt:.6f}. polymarket_deposit_pusd only transfers pUSD on Polygon "
                "— wrap USDC.e / native USDC to pUSD first.",
                {
                    "owner": sender,
                    "have_raw": pusd_balance,
                    "need_raw": amount_raw,
                },
            )
        ok_fund, res = await adapter.fund_deposit_wallet(amount_raw=amount_raw)
        effects = [
            {
                "type": "polymarket",
                "label": "fund_deposit_wallet",
                "ok": ok_fund,
                "result": res,
            }
        ]
        status = "confirmed" if ok_fund else "failed"
        _annotate(
            address=sender,
            label=wallet_label,
            action="fund_deposit_wallet",
            status=status,
            chain_id=POLYGON_CHAIN_ID,
            details={"amount": amt},
        )
        return ok(
            {
                "status": status,
                "wallet_label": wallet_label,
                "address": sender,
                "amount": amt,
                "effects": effects,
            }
        )
    finally:
        await adapter.close()


@catch_errors
async def polymarket_withdraw_pusd(
    *,
    wallet_label: str,
    amount: float | None = None,
) -> dict[str, Any]:
    """Pull pUSD from the deposit wallet back to the owner EOA via the Polymarket relayer.

    Relayer-mediated batch — the owner EOA pays no gas. Omit `amount` to drain the
    full deposit-wallet pUSD balance.

    Args:
        wallet_label: Owner EOA wallet.
        amount: pUSD to withdraw, in human units. Omit to drain.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    amt = (
        throw_if_not_number("amount must be a number", amount)
        if amount is not None
        else None
    )
    adapter, sender = await _make_polymarket_adapter(wallet_label)
    try:
        ok_w, res = await adapter.withdraw_deposit_wallet(
            amount_raw=int(Decimal(str(amt)) * Decimal(1_000_000))
            if amt is not None
            else None
        )
        effects = [
            {
                "type": "polymarket",
                "label": "withdraw_deposit_wallet",
                "ok": ok_w,
                "result": res,
            }
        ]
        status = "confirmed" if ok_w else "failed"
        _annotate(
            address=sender,
            label=wallet_label,
            action="withdraw_deposit_wallet",
            status=status,
            chain_id=POLYGON_CHAIN_ID,
            details={"amount": amt},
        )
        return ok(
            {
                "status": status,
                "wallet_label": wallet_label,
                "address": sender,
                "amount": amt,
                "effects": effects,
            }
        )
    finally:
        await adapter.close()


@catch_errors
async def polymarket_place_market_order(
    *,
    wallet_label: str,
    side: Literal["BUY", "SELL"] = "BUY",
    market_slug: str | None = None,
    outcome: str | int = "YES",
    token_id: str | None = None,
    buy_amount_pusd: float | None = None,
    sell_amount_shares: float | None = None,
    max_slippage_pct: float | None = None,
) -> dict[str, Any]:
    """Place a Polymarket market order (FOK limit at a slippage-derived cap).

    Provide `market_slug`+`outcome` OR `token_id`. BUY needs `buy_amount_pusd`;
    SELL needs `sell_amount_shares`. The adapter quotes the book and signs an FOK limit at
    `worst_price * (1 ± max_slippage_pct/100)` (default 2%) — order is killed if the
    book moves past the cap.

    Args:
        wallet_label: Owner EOA wallet (deposit wallet must already be funded).
        side: `"BUY"` or `"SELL"`.
        market_slug: Polymarket market slug; used with `outcome` to resolve token_id.
        outcome: `"YES"`/`"NO"` or numeric index (default `"YES"`).
        token_id: Direct CLOB token id; alternative to market_slug + outcome.
        buy_amount_pusd: pUSD to spend (required for BUY).
        sell_amount_shares: Shares to sell (required for SELL).
        max_slippage_pct: Slippage cap as a percent (e.g. 2.0). None = adapter default (2%).
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    side = normalize_pm_side(side)
    sizing = validate_pm_market_order_size(
        side=side,
        buy_amount_pusd=buy_amount_pusd,
        sell_amount_shares=sell_amount_shares,
    )

    adapter, sender = await _make_polymarket_adapter(wallet_label)
    resolved_outcome = str(outcome) if market_slug else None
    try:
        if market_slug:
            if side == "BUY":
                ok_trade, res = await adapter.place_prediction(
                    market_slug=str(market_slug),
                    outcome=outcome,
                    amount_collateral=sizing["adapter_amount"],
                    max_slippage_pct=max_slippage_pct,
                )
            else:
                ok_trade, res = await adapter.cash_out_prediction(
                    market_slug=str(market_slug),
                    outcome=outcome,
                    shares=sizing["adapter_amount"],
                    max_slippage_pct=max_slippage_pct,
                )
        else:
            tid = throw_if_empty_str("token_id or market_slug is required", token_id)
            ok_tm, market = await adapter.get_market_by_token_id(token_id=tid)
            if ok_tm:
                resolved_outcome = adapter.resolve_outcome_from_token_id(
                    market=market, token_id=tid
                )
            ok_trade, res = await adapter.place_market_order(
                token_id=tid,
                side=side,
                amount=sizing["adapter_amount"],
                max_slippage_pct=max_slippage_pct,
            )
        raw = res if isinstance(res, dict) else {"result": res}
        raw_quote = raw.get("quote") if isinstance(raw.get("quote"), dict) else None
        execution_summary = normalize_pm_execution_summary(
            side=side,
            sizing=sizing,
            quote=raw_quote,
            raw=raw,
            failed=not ok_trade and raw_quote is None,
        )
        effects = [
            {
                "type": "polymarket",
                "label": "place_market_order",
                "ok": ok_trade,
                "result": res,
            }
        ]
        status = "confirmed" if ok_trade else "failed"
        _annotate(
            address=sender,
            label=wallet_label,
            action="place_market_order",
            status=status,
            chain_id=POLYGON_CHAIN_ID,
            details={
                "market_slug": str(market_slug) if market_slug else None,
                "token_id": str(token_id) if token_id else None,
                "outcome": resolved_outcome,
                "side": side,
                "sizing_kind": sizing["sizing_kind"],
                "buy_amount_pusd": sizing["buy_amount_pusd"],
                "sell_amount_shares": sizing["sell_amount_shares"],
                "max_slippage_pct": float(max_slippage_pct)
                if max_slippage_pct is not None
                else None,
            },
        )
        return ok(
            {
                "status": status,
                "wallet_label": wallet_label,
                "address": sender,
                "market_slug": str(market_slug) if market_slug else None,
                "token_id": str(token_id) if token_id else None,
                "outcome": resolved_outcome,
                "side": side,
                "sizing_kind": sizing["sizing_kind"],
                "buy_amount_pusd": sizing["buy_amount_pusd"],
                "sell_amount_shares": sizing["sell_amount_shares"],
                "max_slippage_pct": float(max_slippage_pct)
                if max_slippage_pct is not None
                else None,
                "executionSummary": execution_summary,
                "effects": effects,
                "raw": raw,
            }
        )
    finally:
        await adapter.close()


@catch_errors
async def polymarket_place_limit_order(
    *,
    wallet_label: str,
    token_id: str,
    side: Literal["BUY", "SELL"],
    price: float,
    size: float,
    post_only: bool = False,
) -> dict[str, Any]:
    """Place a Polymarket limit order on a specific CLOB token id.

    `post_only=True` enforces maker-only — the order is rejected if it would cross.

    Args:
        wallet_label: Owner EOA wallet (deposit wallet must already be funded).
        token_id: CLOB token id (from market.yesTokenId / .noTokenId).
        side: `"BUY"` or `"SELL"`.
        price: Limit price in [0, 1] (probability).
        size: Shares.
        post_only: Reject if order would cross the book.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    tid = throw_if_empty_str("token_id is required", token_id)
    throw_if_none("price is required", price)
    throw_if_none("size is required", size)

    adapter, sender = await _make_polymarket_adapter(wallet_label)
    try:
        ok_lo, res = await adapter.place_limit_order(
            token_id=tid,
            side=side,
            price=float(price),
            size=float(size),
            post_only=bool(post_only),
        )
        effects = [
            {
                "type": "polymarket",
                "label": "place_limit_order",
                "ok": ok_lo,
                "result": res,
            }
        ]
        status = "confirmed" if ok_lo else "failed"
        _annotate(
            address=sender,
            label=wallet_label,
            action="place_limit_order",
            status=status,
            chain_id=POLYGON_CHAIN_ID,
            details={
                "token_id": tid,
                "side": side,
                "price": float(price),
                "size": float(size),
                "post_only": bool(post_only),
            },
        )
        return ok(
            {
                "status": status,
                "wallet_label": wallet_label,
                "address": sender,
                "token_id": tid,
                "side": side,
                "price": float(price),
                "size": float(size),
                "post_only": bool(post_only),
                "effects": effects,
            }
        )
    finally:
        await adapter.close()


@catch_errors
async def polymarket_cancel_order(
    *,
    wallet_label: str,
    order_id: str,
) -> dict[str, Any]:
    """Cancel a resting Polymarket order by id.

    Args:
        wallet_label: Owner EOA wallet that placed the order.
        order_id: CLOB order id returned at placement.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    oid = throw_if_empty_str("order_id is required", order_id)
    adapter, sender = await _make_polymarket_adapter(wallet_label)
    try:
        ok_c, res = await adapter.cancel_order(order_id=oid)
        effects = [
            {
                "type": "polymarket",
                "label": "cancel_order",
                "ok": ok_c,
                "result": res,
            }
        ]
        status = "confirmed" if ok_c else "failed"
        _annotate(
            address=sender,
            label=wallet_label,
            action="cancel_order",
            status=status,
            chain_id=POLYGON_CHAIN_ID,
            details={"order_id": oid},
        )
        return ok(
            {
                "status": status,
                "wallet_label": wallet_label,
                "address": sender,
                "order_id": oid,
                "effects": effects,
            }
        )
    finally:
        await adapter.close()


@catch_errors
async def polymarket_redeem_positions(
    *,
    wallet_label: str,
    condition_id: str,
) -> dict[str, Any]:
    """Claim winnings on a resolved Polymarket market.

    Any USDC.e proceeds are auto-wrapped 1:1 to pUSD via BRAP's polymarket_bridge solver
    inside the deposit wallet, so the agent ends up holding pUSD.

    Args:
        wallet_label: Owner EOA wallet that held the position.
        condition_id: Market's CTF condition id (from Gamma).
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    cid = throw_if_empty_str("condition_id is required", condition_id)
    adapter, sender = await _make_polymarket_adapter(wallet_label)
    try:
        ok_r, res = await adapter.redeem_positions(condition_id=cid)
        effects = [
            {
                "type": "polymarket",
                "label": "redeem_positions",
                "ok": ok_r,
                "result": res,
            }
        ]
        status = "confirmed" if ok_r else "failed"
        _annotate(
            address=sender,
            label=wallet_label,
            action="redeem_positions",
            status=status,
            chain_id=POLYGON_CHAIN_ID,
            details={"condition_id": cid},
        )
        return ok(
            {
                "status": status,
                "wallet_label": wallet_label,
                "address": sender,
                "condition_id": cid,
                "effects": effects,
            }
        )
    finally:
        await adapter.close()
