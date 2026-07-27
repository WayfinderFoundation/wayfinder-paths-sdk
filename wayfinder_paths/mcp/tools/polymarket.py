from __future__ import annotations

import asyncio
import math
import re
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
from wayfinder_paths.mcp.arg_validation import MCPArgumentError
from wayfinder_paths.mcp.polymarket_order import (
    normalize_pm_execution_summary,
    normalize_pm_side,
    validate_pm_market_order_size,
)
from wayfinder_paths.mcp.polymarket_relevance import relevance_search
from wayfinder_paths.mcp.polymarket_summary import (
    DEFAULT_CANDIDATE_LIMIT,
    compact_candidates,
    compact_category_summary,
    compact_child_events,
    compact_event,
    compact_event_groups,
    compact_market_detail,
    compact_order_book,
    compact_truncation,
    event_markets,
    next_suggested_calls,
)
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.tool_annotations import (
    PolymarketBuyAmount,
    PolymarketMarketSlug,
    PolymarketOutcome,
    PolymarketProbability,
    PolymarketSellShares,
    PolymarketShares,
    PolymarketTokenId,
    SlippagePercentPoints,
)
from wayfinder_paths.mcp.utils import (
    catch_errors,
    err,
    normalize_address,
    ok,
    resolve_wallet_address,
    throw_if_empty_str,
    throw_if_none,
)


def _adapter_error(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("error") or payload)
        return err(str(payload.get("code") or "error"), message, payload)
    return err("error", str(payload))


def _validate_market_reference(
    *,
    token_id: Any,
    market_slug: Any,
    event_slug: Any = None,
    allow_event: bool = False,
) -> None:
    references = {
        "token_id": str(token_id or "").strip(),
        "market_slug": str(market_slug or "").strip(),
    }
    if allow_event:
        references["event_slug"] = str(event_slug or "").strip()
    provided = [field for field, value in references.items() if value]
    if len(provided) == 1:
        return
    expected = (
        "exactly one of token_id, market_slug, or event_slug"
        if allow_event
        else "exactly one of token_id or market_slug"
    )
    raise MCPArgumentError(
        f"provide {expected}; market_slug must be paired with outcome",
        field="token_id",
        received={field: value or None for field, value in references.items()},
        suggested_arguments={
            "token_id": None,
            "market_slug": "exact-market-slug",
            "outcome": "YES",
        },
    )


def _positive_pm_number(value: Any, *, field_name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise MCPArgumentError(
            f"{field_name} must be a positive number",
            field=field_name,
            received=value,
        ) from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise MCPArgumentError(
            f"{field_name} must be positive",
            field=field_name,
            received=value,
        )
    return parsed


def _normalize_pm_lookup_text(value: Any) -> str:
    return " ".join(
        "".join(ch.lower() if ch.isalnum() else " " for ch in str(value or "")).split()
    )


def _extract_polymarket_event_slug(value: Any) -> tuple[str | None, bool]:
    text = str(value or "").strip()
    if not text:
        return None, False
    match = re.search(
        r"polymarket\.com/(?:[a-z]{2}/)?(?:sports/[^/\s]+/|event/)([a-z0-9][a-z0-9-]+)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1), True
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", text):
        return text, False
    return None, False


def _is_polymarket_sports_event(event: dict[str, Any]) -> bool:
    if event.get("gameId") or event.get("sport"):
        return True
    tags = event.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            slug = str(tag.get("slug") or "").lower()
            if slug in {"sports", "games", "soccer", "fifa-world-cup"}:
                return True
    return False


async def _polymarket_event_summary(
    adapter: PolymarketAdapter,
    *,
    action: str,
    slug: str,
    candidate_limit: int,
    offset: int = 0,
    query: str | None = None,
    exact_event_hydration: bool = False,
) -> tuple[bool, dict[str, Any]]:
    ok_e, e = await adapter.get_event_by_slug(slug)
    if not ok_e:
        assert isinstance(e, (dict, str))
        return False, e if isinstance(e, dict) else {"code": "error", "message": str(e)}
    assert isinstance(e, dict)

    child_events: list[dict[str, Any]] = []
    warnings: list[str] = []
    parent_id = str(e.get("id") or "").strip()
    if parent_id and _is_polymarket_sports_event(e):
        ok_children, children = await adapter.list_events(
            parent_event_id=parent_id,
            limit=50,
            closed=False,
        )
        if ok_children and isinstance(children, list):
            child_events = [child for child in children if isinstance(child, dict)]
        elif not ok_children:
            warnings.append("sports child-event hydration failed; parent markets only")

    parent_markets = event_markets(e, event_slug_override=slug)
    child_markets = [
        market
        for child in child_events
        for market in event_markets(
            child, event_slug_override=str(child.get("slug") or "")
        )
    ]
    markets = parent_markets + child_markets
    candidates, truncation = compact_candidates(
        markets,
        candidate_limit,
        event_slug_override=slug,
        sort_open_first=True,
        offset=offset,
    )
    payload: dict[str, Any] = {
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
    if query is not None:
        payload["query"] = query
    if exact_event_hydration:
        payload["exactEventHydration"] = True
        payload["eventSlug"] = slug
    if child_events:
        payload["sportsBoard"] = {
            "parentMarketCount": len(parent_markets),
            "childEventCount": len(child_events),
            "childMarketCount": len(child_markets),
            "totalMarketCount": len(markets),
        }
        payload["childEvents"] = compact_child_events(child_events)
        payload["categorySummary"] = compact_category_summary(markets)
    if warnings:
        payload["warnings"] = warnings
    return True, payload


def _summary_outcome_token_id(market: dict[str, Any], outcome: str | int) -> str | None:
    if isinstance(outcome, int):
        if outcome == 0:
            return str(market.get("yesTokenId") or "").strip() or None
        if outcome == 1:
            return str(market.get("noTokenId") or "").strip() or None
        return None

    want = _normalize_pm_lookup_text(outcome)
    yes_label = _normalize_pm_lookup_text(market.get("yesLabel") or "yes")
    no_label = _normalize_pm_lookup_text(market.get("noLabel") or "no")
    if want in {"yes", yes_label}:
        return str(market.get("yesTokenId") or "").strip() or None
    if want in {"no", no_label}:
        return str(market.get("noTokenId") or "").strip() or None
    return None


def _market_lookup_score(market: dict[str, Any], query: str) -> float:
    want = _normalize_pm_lookup_text(query)
    if not want:
        return 0.0
    slug = _normalize_pm_lookup_text(market.get("slug"))
    question = _normalize_pm_lookup_text(market.get("question") or market.get("title"))
    event_slug = _normalize_pm_lookup_text(market.get("eventSlug"))
    haystack = " ".join(part for part in (slug, question, event_slug) if part)
    if slug == want:
        return 1.0
    if want and want in slug:
        return 0.92
    if want and want in question:
        return 0.84
    tokens = [token for token in want.split() if len(token) > 2]
    if tokens and haystack:
        return sum(1 for token in tokens if token in haystack) / len(tokens)
    return 0.0


def _compact_resolution_candidates(
    markets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates, _ = compact_candidates(markets, min(len(markets), 5) or 5)
    return candidates


async def _resolve_read_token_id(
    adapter: PolymarketAdapter,
    *,
    token_id: str | None,
    market_slug: str | None,
    event_slug: str | None,
    outcome: str | int,
) -> tuple[bool, dict[str, Any] | str]:
    """Resolve read-only CLOB actions from token_id or market_slug+outcome.

    Agents often have a compact market row in context and naturally pass
    market_slug+outcome, mirroring quote/write tools. Support that path here, and
    do one bounded search for loose slugs so the failure is actionable instead of
    the opaque CLOB-level "token_id is required".
    """
    tid = str(token_id or "").strip()
    if tid:
        return True, {"token_id": tid, "resolution": {"source": "token_id"}}

    slug = str(market_slug or "").strip()
    event = str(event_slug or "").strip()
    if not slug:
        return (
            False,
            {
                "code": "token_resolution_required",
                "message": (
                    "token_id is required, or pass an exact market_slug plus outcome. "
                    "If you only have a natural label, call search/get_event first and "
                    "use outcomes[].tokenId."
                ),
            },
        )

    exact_market_resolution_error: str | None = None
    ok_m, market = await adapter.get_market_by_slug(slug)
    if ok_m and isinstance(market, dict):
        ok_tid, resolved = adapter.resolve_clob_token_id(market=market, outcome=outcome)
        if ok_tid:
            return (
                True,
                {
                    "token_id": resolved,
                    "resolution": {
                        "source": "market_slug",
                        "market_slug": market.get("slug") or slug,
                        "question": market.get("question"),
                        "outcome": outcome,
                    },
                },
            )
        exact_market_resolution_error = resolved
        if "missing clobtokenids" not in str(resolved).lower():
            return False, {"code": "token_resolution_failed", "message": resolved}

    if event:
        ok_e, event_payload = await adapter.get_event_by_slug(event)
        if ok_e and isinstance(event_payload, dict):
            markets = [
                item
                for item in event_payload.get("markets", [])
                if isinstance(item, dict)
            ]
            scored = sorted(
                ((item, _market_lookup_score(item, slug)) for item in markets),
                key=lambda item: item[1],
                reverse=True,
            )
            if scored and scored[0][1] >= 0.75:
                market = scored[0][0]
                ok_tid, resolved = adapter.resolve_clob_token_id(
                    market=market, outcome=outcome
                )
                if ok_tid:
                    return (
                        True,
                        {
                            "token_id": resolved,
                            "resolution": {
                                "source": "event_slug_market_match",
                                "event_slug": event,
                                "market_slug": market.get("slug"),
                                "question": market.get("question"),
                                "outcome": outcome,
                                "score": scored[0][1],
                            },
                        },
                    )

    ok_s, rows = await adapter.search_markets(
        query=slug,
        limit=5,
        sort="liquidity",
        status="active",
    )
    if ok_s and isinstance(rows, list):
        markets = [item for item in rows if isinstance(item, dict)]
        if event:
            markets = [item for item in markets if item.get("eventSlug") == event]
        scored = sorted(
            ((item, _market_lookup_score(item, slug)) for item in markets),
            key=lambda item: item[1],
            reverse=True,
        )
        best = scored[0] if scored else None
        second_score = scored[1][1] if len(scored) > 1 else 0.0
        confident_unique_match = (
            best
            and best[1] >= 0.75
            and (
                best[1] - second_score >= 0.15
                or (best[1] >= 0.98 and second_score < 0.98)
            )
        )
        if confident_unique_match:
            market = best[0]
            resolved = _summary_outcome_token_id(market, outcome)
            if resolved:
                return (
                    True,
                    {
                        "token_id": resolved,
                        "resolution": {
                            "source": "search_market_match",
                            "market_slug": market.get("slug"),
                            "eventSlug": market.get("eventSlug"),
                            "question": market.get("question"),
                            "outcome": outcome,
                            "score": best[1],
                        },
                    },
                )

        return (
            False,
            {
                "code": "ambiguous_market_slug",
                "message": (
                    "Could not confidently resolve market_slug to a token_id. "
                    "Use one of the returned outcomes[].tokenId values."
                ),
                "candidates": _compact_resolution_candidates(markets),
            },
        )

    return (
        False,
        {
            "code": "token_resolution_failed",
            "message": (
                "Could not resolve market_slug to a token_id. Call search/get_event "
                "and use outcomes[].tokenId."
            ),
            "market_slug": slug,
            "event_slug": event or None,
            "exactMarketError": exact_market_resolution_error,
            "lookupError": rows if not ok_s else None,
        },
    )


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

    `wallet_label` resolves its derived deposit wallet; otherwise pass an account
    or address. Orders default on, activity/trades off. Keep list limits low.
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
    market_slug: PolymarketMarketSlug = None,
    event_slug: str | None = None,
    outcome: PolymarketOutcome = "YES",
    # clob data
    token_id: PolymarketTokenId = None,
    side: Literal["BUY", "SELL"] = "BUY",
    buy_amount_pusd: PolymarketBuyAmount = None,
    sell_amount_shares: PolymarketSellShares = None,
    interval: str | None = "1d",
    start_ts: int | None = None,
    end_ts: int | None = None,
    fidelity: int | None = None,
) -> dict[str, Any]:
    """Read-only Polymarket queries: market discovery, prices, books, history.

    Search/trending discover markets; get_market/event hydrate exact slugs.
    quote/price/book/history accept `token_id` or exact market_slug+outcome.
    BUY quotes need pUSD spend; SELL quotes need share count. Summary mode is
    compact—disable only for raw debugging. Account precedence is account,
    wallet_address, then label; open_orders requires a label with hash signing.
    """
    if action == "quote":
        _validate_market_reference(token_id=token_id, market_slug=market_slug)
        normalized_side = normalize_pm_side(side)
        validate_pm_market_order_size(
            side=normalized_side,
            buy_amount_pusd=buy_amount_pusd,
            sell_amount_shares=sell_amount_shares,
        )
    elif action in {"price", "order_book", "price_history"}:
        _validate_market_reference(
            token_id=token_id,
            market_slug=market_slug,
            event_slug=event_slug,
            allow_event=True,
        )

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
                if summary:
                    exact_slug, strict_exact = _extract_polymarket_event_slug(q)
                    if exact_slug:
                        ok_summary, payload = await _polymarket_event_summary(
                            adapter,
                            action=action,
                            slug=exact_slug,
                            candidate_limit=candidate_limit,
                            offset=int(offset),
                            query=q,
                            exact_event_hydration=True,
                        )
                        if ok_summary:
                            return ok(payload)
                        if strict_exact:
                            return _adapter_error(payload)

                    relevance = await relevance_search(
                        adapter,
                        query=q,
                        limit=int(limit),
                        sort=sort,
                        status=status,
                        candidate_limit=candidate_limit,
                    )
                    if not relevance.ok:
                        return _adapter_error(relevance.error)
                    rows = relevance.rows
                    candidates, truncation = compact_candidates(rows, candidate_limit)
                    event_groups = compact_event_groups(rows)
                    return ok(
                        {
                            "action": action,
                            "query": q,
                            "summaryMode": True,
                            "relevance": relevance.metadata,
                            "candidates": candidates,
                            "eventGroups": event_groups,
                            "nextSuggestedCalls": next_suggested_calls(
                                event_groups=event_groups,
                                truncation=truncation,
                            ),
                            "truncation": truncation,
                        }
                    )
                ok_rows, rows = await adapter.search_markets(
                    query=q,
                    limit=int(limit),
                    sort=sort,
                    status=status,
                )
                if not ok_rows:
                    return _adapter_error(rows)
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
                if summary:
                    ok_summary, payload = await _polymarket_event_summary(
                        adapter,
                        action=action,
                        slug=slug,
                        candidate_limit=candidate_limit,
                        offset=int(offset),
                    )
                    if not ok_summary:
                        return _adapter_error(payload)
                    return ok(payload)
                ok_e, e = await adapter.get_event_by_slug(slug)
                if not ok_e:
                    return _adapter_error(e)
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
                ok_tid, resolved = await _resolve_read_token_id(
                    adapter,
                    token_id=token_id,
                    market_slug=market_slug,
                    event_slug=event_slug,
                    outcome=outcome,
                )
                if not ok_tid:
                    assert isinstance(resolved, dict)
                    return err(
                        str(resolved.get("code") or "token_resolution_failed"),
                        str(resolved.get("message") or "Could not resolve token_id"),
                        resolved,
                    )
                assert isinstance(resolved, dict)
                tid = str(resolved["token_id"])
                ok_p, p = await adapter.get_price(token_id=tid, side=side)
                if not ok_p:
                    return _adapter_error(p)
                return ok(
                    {
                        "action": action,
                        "token_id": tid,
                        "side": side,
                        "price": p,
                        "resolution": resolved.get("resolution"),
                    }
                )

            case "order_book":
                ok_tid, resolved = await _resolve_read_token_id(
                    adapter,
                    token_id=token_id,
                    market_slug=market_slug,
                    event_slug=event_slug,
                    outcome=outcome,
                )
                if not ok_tid:
                    assert isinstance(resolved, dict)
                    return err(
                        str(resolved.get("code") or "token_resolution_failed"),
                        str(resolved.get("message") or "Could not resolve token_id"),
                        resolved,
                    )
                assert isinstance(resolved, dict)
                tid = str(resolved["token_id"])
                ok_b, b = await adapter.get_order_book(token_id=tid)
                if not ok_b:
                    return _adapter_error(b)
                if summary:
                    return ok(
                        {
                            "action": action,
                            "token_id": tid,
                            "summaryMode": True,
                            "resolution": resolved.get("resolution"),
                            "book": compact_order_book(b),
                        }
                    )
                return ok(
                    {
                        "action": action,
                        "token_id": tid,
                        "resolution": resolved.get("resolution"),
                        "book": b,
                    }
                )

            case "price_history":
                ok_tid, resolved = await _resolve_read_token_id(
                    adapter,
                    token_id=token_id,
                    market_slug=market_slug,
                    event_slug=event_slug,
                    outcome=outcome,
                )
                if not ok_tid:
                    assert isinstance(resolved, dict)
                    return err(
                        str(resolved.get("code") or "token_resolution_failed"),
                        str(resolved.get("message") or "Could not resolve token_id"),
                        resolved,
                    )
                assert isinstance(resolved, dict)
                tid = str(resolved["token_id"])
                ok_h, h = await adapter.get_prices_history(
                    token_id=tid,
                    interval=interval,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    fidelity=fidelity,
                )
                if not ok_h:
                    return _adapter_error(h)
                return ok(
                    {
                        "action": action,
                        "token_id": tid,
                        "resolution": resolved.get("resolution"),
                        "history": h,
                    }
                )

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
    """Deposit human-unit pUSD from the owner EOA into its Polymarket wallet.

    Trading settles from the deposit wallet. This only transfers existing pUSD
    on Polygon—it does not wrap USDC—and the owner pays POL gas.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    throw_if_none("amount is required", amount)
    amt = _positive_pm_number(amount, field_name="amount")
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
    """Withdraw human-unit pUSD from the deposit wallet to its owner via relayer.

    Omit `amount` to drain the balance; the owner EOA pays no gas.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    amt = (
        _positive_pm_number(amount, field_name="amount") if amount is not None else None
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
    market_slug: PolymarketMarketSlug = None,
    outcome: PolymarketOutcome = "YES",
    token_id: PolymarketTokenId = None,
    buy_amount_pusd: PolymarketBuyAmount = None,
    sell_amount_shares: PolymarketSellShares = None,
    max_slippage_pct: SlippagePercentPoints = None,
) -> dict[str, Any]:
    """Place a Polymarket market order (FOK limit at a slippage-derived cap).

    Identify the token directly or by market_slug+outcome. BUY sizing is pUSD
    spend; SELL sizing is shares—not interchangeable. The funded deposit wallet
    signs an FOK limit at the slippage cap (default 2%); movement past it cancels.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    _validate_market_reference(token_id=token_id, market_slug=market_slug)
    side = normalize_pm_side(side)
    sizing = validate_pm_market_order_size(
        side=side,
        buy_amount_pusd=buy_amount_pusd,
        sell_amount_shares=sell_amount_shares,
    )
    if max_slippage_pct is not None:
        slippage = float(max_slippage_pct)
        if not math.isfinite(slippage) or slippage < 0 or slippage > 100:
            raise MCPArgumentError(
                "max_slippage_pct must be from 0 to 100; 2.0 means 2%",
                field="max_slippage_pct",
                received=max_slippage_pct,
                suggested_arguments={"max_slippage_pct": 2.0},
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
    side: Literal["BUY", "SELL"],
    price: PolymarketProbability,
    size: PolymarketShares,
    market_slug: PolymarketMarketSlug = None,
    outcome: PolymarketOutcome = "YES",
    token_id: PolymarketTokenId = None,
    post_only: bool = False,
) -> dict[str, Any]:
    """Place a Polymarket limit order.

    Identify the token directly or by market_slug+outcome. `price` is probability
    in [0,1] and `size` is shares. post_only rejects rather than crossing.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    _validate_market_reference(token_id=token_id, market_slug=market_slug)
    side = normalize_pm_side(side)
    throw_if_none("price is required", price)
    throw_if_none("size is required", size)
    px = float(price)
    if not math.isfinite(px) or not 0 < px < 1:
        raise MCPArgumentError(
            "price must be a probability strictly between 0 and 1",
            field="price",
            received=price,
            suggested_arguments={"price": 0.5},
        )
    shares = _positive_pm_number(size, field_name="size")

    adapter, sender = await _make_polymarket_adapter(wallet_label)
    resolved_outcome: str | None = None
    try:
        if market_slug:
            ok_m, market = await adapter.get_market_by_slug(str(market_slug))
            if not ok_m:
                return err(
                    "not_found",
                    market if isinstance(market, str) else "market lookup failed",
                )
            ok_tid, tid_or_err = adapter.resolve_clob_token_id(
                market=market, outcome=outcome
            )
            if not ok_tid:
                return err("invalid_request", tid_or_err)
            tid = tid_or_err
            resolved_outcome = str(outcome)
        else:
            tid = throw_if_empty_str("token_id or market_slug is required", token_id)
            ok_tm, market = await adapter.get_market_by_token_id(token_id=tid)
            if ok_tm:
                resolved_outcome = adapter.resolve_outcome_from_token_id(
                    market=market, token_id=tid
                )

        ok_lo, res = await adapter.place_limit_order(
            token_id=tid,
            side=side,
            price=px,
            size=shares,
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
                "market_slug": str(market_slug) if market_slug else None,
                "token_id": tid,
                "outcome": resolved_outcome,
                "side": side,
                "price": px,
                "size": shares,
                "post_only": bool(post_only),
            },
        )
        return ok(
            {
                "status": status,
                "wallet_label": wallet_label,
                "address": sender,
                "market_slug": str(market_slug) if market_slug else None,
                "token_id": tid,
                "outcome": resolved_outcome,
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

    Proceeds are normalized to pUSD, including neg-risk WCOL unwrapping. Re-run
    safely to recover stranded WCOL from an incomplete prior redemption.
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
