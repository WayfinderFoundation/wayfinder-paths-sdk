"""Compact, observed market evidence for research-only portfolio checks."""

import re
from collections.abc import Iterable
from math import isfinite
from typing import Any
from urllib.parse import urlsplit

from tldextract import TLDExtract

from wayfinder_paths.core.theses.models import Proposal
from wayfinder_paths.mcp.polymarket_summary import (
    compact_market_candidate,
    compact_order_book,
)

# Offline PSL matching includes private suffixes: docs.aave.com belongs with
# app.aave.com, but another user's github.io site does not belong with ours.
_DOMAIN = TLDExtract(
    suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True
)

RESEARCH_EVIDENCE_TOOLS = frozenset(
    {
        "polymarket_read",
        "hyperliquid_search_mid_prices",
        "onchain_resolve_token",
        "onchain_list_tokens",
        "core_web_fetch",
    }
)


def research_evidence(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Consume successful public market reads, never agent assertions."""
    outcomes: dict[str, str] = {}
    ask_depth: dict[str, float] = {}
    event_urls: set[str] = set()
    hyperliquid_depth: dict[str, dict[str, Any]] = {}
    onchain_tokens: dict[str, dict[str, Any]] = {}
    onchain_pools: dict[str, dict[str, Any]] = {}
    pages: list[dict[str, Any]] = []
    for result in results:
        hyperliquid_depth.update(result.get("depth", {}))
        if result.get("token_id") and result.get("address") and result.get("chain"):
            onchain_tokens[result["token_id"]] = {
                key: result.get(key, {})
                for key in ("address", "chain", "identity", "links")
            }
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
        # Only core_web_fetch results are admitted by the caller, not search
        # snippets or the agent's query (which can already contain the address).
        pages.extend(result.get("results", []))
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
    for token in onchain_tokens.values():
        token["issuer_reference"] = _issuer_reference(token, pages)
        token.pop("links")
    return {
        "outcomes": outcomes,
        "ask_depth": ask_depth,
        "event_urls": sorted(event_urls),
        "hyperliquid_depth": hyperliquid_depth,
        "onchain_tokens": onchain_tokens,
        "onchain_pools": onchain_pools,
    }


def _issuer_reference(token: dict[str, Any], pages: list[dict[str, Any]]) -> str | None:
    """Corroborate the address on a registry-linked website, not token safety."""
    address = token["address"]
    for page in pages:
        content = page.get("contentExcerpt", "")
        # EVM addresses ignore case; Solana mint addresses must retain it.
        if address.startswith("0x"):
            content, address = content.lower(), address.lower()
        if not re.search(
            rf"(?<![A-Za-z0-9]){re.escape(address)}(?![A-Za-z0-9])", content
        ):
            continue
        parsed = urlsplit(page["url"])
        for website in token["links"].get("homepage", []) + token["links"].get(
            "github", []
        ):
            official = urlsplit(website)
            if official.scheme not in {"http", "https"}:
                continue
            host = official.hostname or ""
            host = _DOMAIN(host).top_domain_under_public_suffix or host
            path = official.path.rstrip("/")
            if (
                host
                and parsed.scheme in {"http", "https"}
                and (
                    parsed.hostname == host
                    or (parsed.hostname or "").endswith("." + host)
                )
                and (
                    not path
                    or parsed.path == path
                    or parsed.path.startswith(path + "/")
                )
            ):
                return page["url"]
    return None


def validate_market_capacity(proposal: Proposal, evidence: dict[str, Any]) -> None:
    """Conservative research sizing, not an executable quote or future fill guarantee."""
    for variant in proposal.variants:
        for position in variant.positions:
            location = f"{variant.budget_usd}/{position.id}"
            if position.kind == "token" and "/" not in position.instrument_id:
                token = evidence.get("onchain_tokens", {}).get(
                    position.instrument_id, {}
                )
                identity = token.get("identity", {})
                if (
                    not token
                    or identity.get("suspicious")
                    or not (
                        identity.get("is_canonical") is True
                        or token.get("issuer_reference")
                    )
                ):
                    raise ValueError(
                        f"{location}: resolve the exact onchain token, then use core_web_fetch "
                        "to corroborate its contract on its registry-linked issuer website; "
                        "a listing, search match or disclaimer does not verify the contract"
                    )
                pool = evidence.get("onchain_pools", {}).get(position.instrument_id, {})
                reserve = pool.get("liquidity_usd") or 0
                volume = pool.get("volume_24h_usd") or 0
                capital = variant.budget_usd * position.capital_bps / 10000
                if (
                    pool.get("address") != token["address"]
                    or pool.get("chain_code") != token["chain"]["code"]
                    or not all(isfinite(n) and n > 0 for n in (reserve, volume))
                    or capital > min(0.005 * reserve, 0.01 * volume)
                ):
                    raise ValueError(
                        f"{location}: onchain capital ${capital:g} exceeds the research cap "
                        f"of 0.5% of selected-pool reserves (${reserve:g}) or 1% of its "
                        f"24h volume (${volume:g}); use onchain_list_tokens with the chain "
                        "and exact address query, reduce capital_bps or omit the leg. "
                        "This cap is a sizing proxy, not executable depth or a fill quote"
                    )
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
