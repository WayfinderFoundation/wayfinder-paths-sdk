from __future__ import annotations

import json
from typing import Any, Literal

from wayfinder_paths.mcp.polymarket_order import as_float, first_present

DEFAULT_CANDIDATE_LIMIT = 10


def compact_truncation(total: int, returned: int) -> dict[str, Any]:
    return {
        "totalAvailable": total,
        "returnedCandidates": returned,
        "truncated": total > returned,
        "rawAvailableWithSummaryFalse": True,
    }


def compact_text(value: Any, *, max_chars: int = 700) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def maybe_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        return decoded if isinstance(decoded, list) else [decoded]
    return [value]


def as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def event_slug(market: dict[str, Any]) -> str | None:
    direct = first_present(market, "eventSlug", "event_slug")
    if direct:
        return str(direct)
    event = market.get("_event")
    if isinstance(event, dict) and event.get("slug"):
        return str(event["slug"])
    events = market.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        slug = events[0].get("slug")
        return str(slug) if slug else None
    return None


def compact_outcomes(market: dict[str, Any]) -> list[dict[str, Any]]:
    if market.get("yesTokenId") or market.get("noTokenId"):
        return [
            {
                "label": str(market.get("yesLabel") or "Yes"),
                "price": as_float(market.get("yesPrice")),
                "tokenId": market.get("yesTokenId"),
            },
            {
                "label": str(market.get("noLabel") or "No"),
                "price": as_float(market.get("noPrice")),
                "tokenId": market.get("noTokenId"),
            },
        ]

    labels = maybe_json_list(market.get("outcomes"))
    prices = maybe_json_list(market.get("outcomePrices"))
    token_ids = maybe_json_list(market.get("clobTokenIds"))
    count = max(len(labels), len(prices), len(token_ids))
    outcomes: list[dict[str, Any]] = []
    for idx in range(count):
        label = labels[idx] if idx < len(labels) else idx
        outcomes.append(
            {
                "label": str(label),
                "price": as_float(prices[idx] if idx < len(prices) else None),
                "tokenId": token_ids[idx] if idx < len(token_ids) else None,
            }
        )
    return outcomes


def compact_market_candidate(
    market: dict[str, Any], *, event_slug_override: str | None = None
) -> dict[str, Any]:
    outcomes = compact_outcomes(market)
    best_bid = as_float(first_present(market, "bestBid", "bid", "yesBid"))
    best_ask = as_float(first_present(market, "bestAsk", "ask", "yesAsk"))
    spread = as_float(market.get("spread"))
    if spread is None and best_bid is not None and best_ask is not None:
        spread = best_ask - best_bid
    active = as_bool(market.get("active"))
    accepting_orders = as_bool(market.get("acceptingOrders"))
    closed = as_bool(market.get("closed"))
    order_book_enabled = as_bool(market.get("enableOrderBook"))
    has_token_ids = any(o.get("tokenId") for o in outcomes)
    tradable = (
        has_token_ids
        and order_book_enabled is not False
        and accepting_orders is not False
        and active is not False
        and closed is not True
    )
    candidate = {
        "slug": first_present(market, "slug", "marketSlug"),
        "eventSlug": event_slug(market) or event_slug_override,
        "question": first_present(market, "question", "title", "symbol"),
        "outcomes": outcomes,
        "bestBid": best_bid,
        "bestAsk": best_ask,
        "spread": spread,
        "liquidity": as_float(
            first_present(market, "liquidity", "liquidityNum", "liquidityClob")
        ),
        "volume24h": as_float(
            first_present(market, "volume24h", "volume24hr", "volume24hrClob")
        ),
        "resolvesAt": first_present(
            market, "resolvesAt", "endDateIso", "endDate", "resolutionDate"
        ),
        "conditionId": first_present(market, "conditionId", "condition_id"),
        "tradable": bool(tradable),
        "active": active,
        "acceptingOrders": accepting_orders,
        "closed": closed,
    }
    if isinstance(market.get("_relevance"), dict):
        candidate["relevance"] = market["_relevance"]
    return candidate


def compact_candidates(
    markets: list[dict[str, Any]],
    candidate_limit: int,
    *,
    event_slug_override: str | None = None,
    sort_open_first: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    limit = max(0, int(candidate_limit))
    candidates = [
        compact_market_candidate(m, event_slug_override=event_slug_override)
        for m in markets
    ]
    if sort_open_first:
        candidates.sort(
            key=lambda c: (
                c.get("active") is not True,
                c.get("closed") is True,
                c.get("acceptingOrders") is not True,
                c.get("tradable") is not True,
                -float(c.get("volume24h") or 0),
                -float(c.get("liquidity") or 0),
            )
        )
    candidates = candidates[:limit]
    return candidates, compact_truncation(len(markets), len(candidates))


def compact_event_groups(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for market in markets:
        slug = event_slug(market)
        if slug:
            grouped.setdefault(slug, []).append(market)

    event_groups: list[dict[str, Any]] = []
    for slug, rows in grouped.items():
        candidates = [
            compact_market_candidate(m, event_slug_override=slug) for m in rows
        ]
        candidates.sort(
            key=lambda c: (
                -float(c.get("volume24h") or 0),
                -float(c.get("liquidity") or 0),
            )
        )
        top_questions = [
            str(c.get("question")) for c in candidates[:3] if c.get("question")
        ]
        event_groups.append(
            {
                "eventSlug": slug,
                "candidatesInSearch": len(candidates),
                "topQuestions": top_questions,
                "maxLiquidity": max(
                    (float(c.get("liquidity") or 0) for c in candidates),
                    default=0.0,
                ),
                "maxVolume24h": max(
                    (float(c.get("volume24h") or 0) for c in candidates),
                    default=0.0,
                ),
                "nextSuggestedCall": {
                    "action": "get_event",
                    "event_slug": slug,
                    "candidate_limit": 20,
                },
            }
        )

    event_groups.sort(
        key=lambda g: (
            -int(g["candidatesInSearch"]),
            -float(g["maxVolume24h"]),
            -float(g["maxLiquidity"]),
        )
    )
    return event_groups


def next_suggested_calls(
    *,
    event_groups: list[dict[str, Any]] | None = None,
    truncation: dict[str, Any] | None = None,
    event_slug_value: str | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    if event_slug_value and truncation and truncation.get("truncated"):
        calls.append(
            {
                "reason": "event candidates truncated",
                "call": {
                    "action": "get_event",
                    "event_slug": event_slug_value,
                    "candidate_limit": 20,
                },
            }
        )
    for group in event_groups or []:
        if int(group.get("candidatesInSearch") or 0) <= 1:
            continue
        calls.append(
            {
                "reason": "multiple candidates share an eventSlug; hydrate the event ladder instead of searching each date",
                "call": group["nextSuggestedCall"],
            }
        )
    return calls


def compact_market_detail(market: dict[str, Any]) -> dict[str, Any]:
    detail = compact_market_candidate(market)
    detail.update(
        {
            "description": compact_text(market.get("description"), max_chars=900),
            "resolutionSource": compact_text(
                market.get("resolutionSource"), max_chars=500
            ),
            "rules": compact_text(
                first_present(
                    market,
                    "rules",
                    "resolutionRules",
                    "resolutionCriteria",
                    "groupItemTitle",
                ),
                max_chars=900,
            ),
        }
    )
    return detail


def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": event.get("slug"),
        "title": event.get("title"),
        "description": compact_text(event.get("description"), max_chars=900),
        "startDate": first_present(event, "startDateIso", "startDate"),
        "endDate": first_present(event, "endDateIso", "endDate"),
        "active": as_bool(event.get("active")),
        "closed": as_bool(event.get("closed")),
    }


def compact_book_side(
    levels: Any, *, side: Literal["bids", "asks"]
) -> list[dict[str, Any]]:
    if not isinstance(levels, list):
        return []
    compact: list[dict[str, Any]] = []
    for level in levels:
        if not isinstance(level, dict):
            continue
        price = as_float(level.get("price"))
        size = as_float(level.get("size"))
        if price is None or size is None:
            continue
        compact.append({"price": price, "size": size, "notional": price * size})
    compact.sort(key=lambda level: level["price"], reverse=side == "bids")
    return compact


def compact_order_book(
    book: dict[str, Any], *, depth_levels: int = 3
) -> dict[str, Any]:
    bids = compact_book_side(book.get("bids"), side="bids")
    asks = compact_book_side(book.get("asks"), side="asks")
    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None
    return {
        "market": book.get("market"),
        "asset_id": book.get("asset_id"),
        "timestamp": book.get("timestamp"),
        "bestBid": best_bid,
        "bestAsk": best_ask,
        "spread": (
            best_ask - best_bid
            if best_bid is not None and best_ask is not None
            else None
        ),
        "bidLevels": len(bids),
        "askLevels": len(asks),
        "topBids": bids[:depth_levels],
        "topAsks": asks[:depth_levels],
        "topBidNotional": sum(level["notional"] for level in bids[:depth_levels]),
        "topAskNotional": sum(level["notional"] for level in asks[:depth_levels]),
        "rawAvailableWithSummaryFalse": True,
    }
