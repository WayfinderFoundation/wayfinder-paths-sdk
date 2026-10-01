"""Compact, observed market evidence for research-only portfolio checks."""

import re
from collections.abc import Iterable
from math import isfinite
from typing import Any

from wayfinder_paths.core.theses.models import Position, Proposal
from wayfinder_paths.mcp.polymarket_summary import (
    compact_market_candidate,
    compact_order_book,
)

RESEARCH_EVIDENCE_TOOLS = frozenset(
    {
        "polymarket_read",
        "hyperliquid_search_mid_prices",
        "onchain_resolve_token",
        "onchain_list_tokens",
        "core_web_fetch",
        "research_quantify_portfolio",
    }
)


def research_evidence(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Consume successful public market reads, never agent assertions."""
    outcomes: dict[str, str] = {}
    ask_depth: dict[str, float] = {}
    event_urls: set[str] = set()
    fetched_urls: set[str] = set()
    hyperliquid_depth: dict[str, dict[str, Any]] = {}
    onchain_tokens: dict[str, dict[str, Any]] = {}
    onchain_pools: dict[str, dict[str, Any]] = {}
    pages: list[dict[str, Any]] = []
    quantified_allocations: dict[str, dict[str, Any]] = {}
    for result in results:
        for portfolio in result.get("portfolio_quantification", {}).get(
            "portfolios", []
        ):
            quantified_allocations[portfolio["allocation_key"]] = portfolio
        hyperliquid_depth.update(result.get("depth", {}))
        resolution = result.get("resolved_token") or result
        if (
            resolution.get("token_id")
            and resolution.get("address")
            and resolution.get("chain")
        ):
            token = {
                key: resolution.get(key) or {}
                for key in ("token_id", "address", "chain", "identity")
            }
            onchain_tokens[resolution["token_id"]] = token
            if resolution.get("lookup_id"):
                onchain_tokens[resolution["lookup_id"]] = token
        if result.get("chain_code"):
            for token in result.get("tokens", []):
                if (
                    token.get("token_id")
                    and token.get("chain_code") == result["chain_code"]
                ):
                    onchain_pools[token["token_id"]] = {
                        key: token.get(key)
                        for key in (
                            "chain_code",
                            "address",
                            "pool_address",
                            "top_pool_name",
                            "dex",
                            "liquidity_usd",
                            "volume_24h_usd",
                        )
                    }
        # Source reads substantiate economic claims, not contract identity.
        pages.extend(result.get("results", []))
        # Direct event/market reads can supply resolution rules without a web
        # fetch. Search candidates alone cannot establish a source was read.
        if result.get("action") in {"get_event", "get_market"}:
            source = result.get("event") or result.get("market") or {}
            slug = (
                source.get("slug")
                if result["action"] == "get_event"
                else source.get("eventSlug")
            )
            if slug and (source.get("description") or source.get("rules")):
                fetched_urls.add(f"https://polymarket.com/event/{slug}")
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
    fetched_urls.update(
        page["url"]
        for page in pages
        if page.get("url") and page.get("contentExcerpt", "").strip()
    )
    return {
        "outcomes": outcomes,
        "ask_depth": ask_depth,
        "event_urls": sorted(event_urls),
        "fetched_urls": sorted(fetched_urls),
        "hyperliquid_depth": hyperliquid_depth,
        "onchain_tokens": onchain_tokens,
        "onchain_pools": onchain_pools,
        "quantified_allocations": quantified_allocations,
    }


def missing_source_reads(proposal: Proposal, evidence: dict[str, Any]) -> list[str]:
    """A cited source was read, not a certification of its truth or relevance."""
    fetched_urls = set(evidence.get("fetched_urls", []))
    invested = {p.component_id for v in proposal.variants for p in v.positions}
    return [
        c.id
        for c in proposal.components
        if c.id in invested
        and not any(
            set(re.findall(r"https?://[^\s<>\])]+", citation)) & fetched_urls
            for citation in c.evidence
        )
    ]


def validate_market_capacity(proposal: Proposal, evidence: dict[str, Any]) -> None:
    """Report every affected instrument; retain the largest-budget failure per ID."""
    errors: dict[str, str] = {}
    for variant in sorted(proposal.variants, key=lambda v: v.budget_usd):
        resolved_ids: set[tuple[str, str]] = set()
        for position in variant.positions:
            resolved = evidence.get("onchain_tokens", {}).get(
                position.instrument_id, {}
            )
            key = (position.kind, resolved.get("token_id") or position.instrument_id)
            if key in resolved_ids:
                errors[position.instrument_id] = (
                    f"{variant.budget_usd}/{position.id}: duplicate resolved instrument; "
                    "combine lookup aliases into one position before sizing"
                )
                continue
            resolved_ids.add(key)
            try:
                _validate_position_capacity(variant.budget_usd, position, evidence)
            except ValueError as exc:
                errors[position.instrument_id] = str(exc)
    if errors:
        raise ValueError("\n".join(errors.values()))


def _validate_position_capacity(
    budget_usd: int, position: Position, evidence: dict[str, Any]
) -> None:
    """Conservative research sizing, not an executable quote or future fill guarantee."""
    location = f"{budget_usd}/{position.id}"
    if position.kind == "token" and "/" not in position.instrument_id:
        token = evidence.get("onchain_tokens", {}).get(position.instrument_id, {})
        if not token:
            raise ValueError(
                f"{location}: unknown onchain instrument_id {position.instrument_id!r}; "
                "use an ID returned by onchain_resolve_token. A chain-scoped lookup "
                "ID is sufficient; no contract address or issuer-page proof is required"
            )
        identity = token.get("identity", {})
        if identity.get("suspicious"):
            raise ValueError(
                f"{location}: resolved token is flagged as a suspicious identity; "
                "choose a non-conflicting instrument"
            )
        # Native holdings use the backend registry's wrapped-native
        # market-data proxy, never an agent-proposed substitute.
        pool_address = token["address"]
        if (
            identity.get("is_canonical") is True
            and identity.get("verification") == "native"
            and identity.get("wrapped_native_address")
        ):
            pool_address = identity["wrapped_native_address"]
        pool_id = f"{token['chain']['code']}_{pool_address}"
        pool = evidence.get("onchain_pools", {}).get(pool_id, {})
        reserve = pool.get("liquidity_usd") or 0
        volume = pool.get("volume_24h_usd") or 0
        capital = budget_usd * position.capital_bps / 10000
        if (
            pool.get("address") != pool_address
            or pool.get("chain_code") != token["chain"]["code"]
            or not all(isfinite(n) and n > 0 for n in (reserve, volume))
            or capital > min(0.005 * reserve, 0.01 * volume)
        ):
            raise ValueError(
                f"{location}: onchain capital ${capital:g} exceeds the research cap "
                f"of 0.5% of selected-pool reserves (${reserve:g}) or 1% of its "
                f"24h volume (${volume:g}); use onchain_list_tokens with "
                f"token_id={position.instrument_id!r}, reduce capital_bps or omit the leg. "
                "This cap is a sizing proxy, not executable depth or a fill quote"
            )
    if position.kind in {"perp", "hip3"} or (
        position.kind == "token" and "/" in position.instrument_id
    ):
        book = evidence.get("hyperliquid_depth", {}).get(position.instrument_id, {})
        bid = book.get("bid_notional_usd_50bps", 0)
        ask = book.get("ask_notional_usd_50bps", 0)
        notional = budget_usd * position.capital_bps / 10000 * position.leverage
        if not all(isfinite(n) and n > 0 for n in (bid, ask)) or notional > 0.1 * min(
            bid, ask
        ):
            raise ValueError(
                f"{location}: Hyperliquid notional ${notional:g} exceeds 10% of "
                f"observed two-sided depth within 50 bps (bids ${bid:g}, asks ${ask:g}); "
                "refresh hyperliquid_search_mid_prices(include_depth=true), reduce capital_bps or omit the leg"
            )
    if position.kind != "prediction":
        return
    if evidence.get("outcomes", {}).get(position.instrument_id) != position.direction:
        raise ValueError(f"{location}: verify the tradable YES/NO outcome token")
    depth = evidence.get("ask_depth", {}).get(position.instrument_id, 0)
    capital = budget_usd * position.capital_bps / 10000
    if not isfinite(depth) or capital > 0.1 * depth:
        raise ValueError(
            f"{location}: prediction capital ${capital:g} exceeds 10% of "
            f"observed ask notional (${depth:g}); reduce capital_bps or omit the leg"
        )
