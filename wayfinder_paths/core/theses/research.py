"""Compact, observed market evidence for research-only portfolio checks."""

from collections.abc import Iterable
from math import isfinite
from typing import Any

from wayfinder_paths.core.theses.models import Proposal
from wayfinder_paths.mcp.polymarket_summary import (
    compact_market_candidate,
    compact_order_book,
)


def research_evidence(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Consume successful prediction/Hyperliquid market reads, never agent assertions."""
    outcomes: dict[str, str] = {}
    ask_depth: dict[str, float] = {}
    event_urls: set[str] = set()
    hyperliquid_depth: dict[str, dict[str, Any]] = {}
    for result in results:
        hyperliquid_depth.update(result.get("depth", {}))
        candidates = list(result.get("candidates", []))
        if market := result.get("market"):
            candidates.append(
                market
                if result.get("summaryMode")
                else compact_market_candidate(market)
            )
        candidates.extend(
            compact_market_candidate(m) for m in result.get("markets", [])
        )
        candidates.extend(
            compact_market_candidate(m, event_slug_override=result["event"].get("slug"))
            for m in result.get("event", {}).get("markets", [])
        )
        for candidate in candidates:
            if slug := candidate.get("eventSlug"):
                event_urls.add(f"https://polymarket.com/event/{slug}")
            for outcome in candidate.get("outcomes", []):
                token_id = outcome["tokenId"]
                if candidate.get("tradable"):
                    outcomes[token_id] = outcome["label"].lower()
                else:
                    outcomes.pop(token_id, None)
        if result.get("action") == "order_book":
            # Most recent observed book wins; market-wide liquidity is not ask depth.
            book = result["book"]
            if "asks" in book:
                book = compact_order_book(book)
            ask_depth[result["token_id"]] = book.get("topAskNotional") or 0
    return {
        "outcomes": outcomes,
        "ask_depth": ask_depth,
        "event_urls": sorted(event_urls),
        "hyperliquid_depth": hyperliquid_depth,
    }


def validate_market_capacity(proposal: Proposal, evidence: dict[str, Any]) -> None:
    """Conservative research sizing, not an executable quote or future fill guarantee."""
    for variant in proposal.variants:
        for position in variant.positions:
            location = f"{variant.budget_usd}/{position.id}"
            if position.kind in {"perp", "hip3"} or (
                position.kind == "token" and "/" in position.instrument_id
            ):
                book = evidence.get("hyperliquid_depth", {}).get(
                    position.instrument_id, {}
                )
                bid = book.get("bid_notional_usd_50bps", 0)
                ask = book.get("ask_notional_usd_50bps", 0)
                notional = (
                    variant.budget_usd
                    * position.capital_bps
                    / 10000
                    * position.leverage
                )
                if not all(
                    isfinite(n) and n > 0 for n in (bid, ask)
                ) or notional > 0.1 * min(bid, ask):
                    raise ValueError(
                        f"{location}: Hyperliquid notional ${notional:g} exceeds 10% of "
                        f"observed two-sided depth within 50 bps (bids ${bid:g}, asks ${ask:g}); "
                        "refresh hyperliquid_search_mid_prices(include_depth=true), reduce capital_bps or omit the leg"
                    )
            if position.kind != "prediction":
                continue
            if (
                evidence.get("outcomes", {}).get(position.instrument_id)
                != position.direction
            ):
                raise ValueError(
                    f"{location}: verify the tradable YES/NO outcome token"
                )
            depth = evidence.get("ask_depth", {}).get(position.instrument_id, 0)
            capital = variant.budget_usd * position.capital_bps / 10000
            if not isfinite(depth) or capital > 0.1 * depth:
                raise ValueError(
                    f"{location}: prediction capital ${capital:g} exceeds 10% of "
                    f"observed ask notional (${depth:g}); reduce capital_bps or omit the leg"
                )
