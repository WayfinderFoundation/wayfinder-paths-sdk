from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.ResearchClient import RESEARCH_CLIENT
from wayfinder_paths.mcp.utils import catch_errors, ok

_SKIP = {"", "_", "none", "null"}


def _optional_str(value: str, *, field_name: str | None = None) -> str | None:
    raw = str(value).strip()
    if raw.lower() in _SKIP:
        return None
    if field_name and len(raw) > 1000:
        raise ValueError(f"{field_name} must be 1000 characters or fewer")
    return raw


def _optional_int(value: str, *, field_name: str) -> int | None:
    raw = str(value).strip()
    if raw.lower() in _SKIP:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def _split_values(
    value: str,
    *,
    field_name: str,
    max_items: int = 25,
) -> list[str] | None:
    raw = _optional_str(value)
    if raw is None:
        return None
    values = [
        item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()
    ]
    if not values:
        return None
    if len(values) > max_items:
        raise ValueError(f"{field_name} must include {max_items} values or fewer")
    return values


@catch_errors
async def research_web_search(
    query: str,
    numResults: str = "8",
    type: str = "auto",
    category: str = "_",
    includeDomains: str = "_",
    excludeDomains: str = "_",
    startPublishedDate: str = "_",
    endPublishedDate: str = "_",
    maxAgeHours: str = "_",
    additionalQueries: str = "_",
    contentType: str = "highlights",
    livecrawl: str = "fallback",
    contextMaxCharacters: str = "_",
    sessionID: str = "_",
) -> dict[str, Any]:
    """Search the public web through the Wayfinder Research Gateway.

    Args:
        query: Search query. Do not include secrets, tokens, or private URLs.
        numResults: Max result count (default "8", range 1-100).
        type: Search type: auto, fast, instant, deep-lite, deep, deep-reasoning, or neural.
        category: Optional category: company, people, research paper, news,
            personal site, financial report, or "_".
        includeDomains: Optional comma/newline-separated domains to include.
        excludeDomains: Optional comma/newline-separated domains to exclude.
        startPublishedDate: Optional ISO datetime lower bound, or "_".
        endPublishedDate: Optional ISO datetime upper bound, or "_".
        maxAgeHours: Optional freshness window in hours, or "_".
        additionalQueries: Optional comma/newline-separated deep-search expansions.
        contentType: Result content mode: highlights, text, or summary.
        livecrawl: Live crawl policy: "fallback" or "preferred".
        contextMaxCharacters: Optional excerpt character cap (500-50000). Use "_"
            to let the backend default apply.
        sessionID: Optional OpenCode session id. Use "_" to resolve from the
            runtime environment or SDK default.
    """
    context_max = _optional_int(
        contextMaxCharacters,
        field_name="contextMaxCharacters",
    )
    result = await RESEARCH_CLIENT.search(
        query=query,
        num_results=int(numResults),
        search_type=type,  # type: ignore[arg-type]
        category=_optional_str(category),  # type: ignore[arg-type]
        include_domains=_split_values(includeDomains, field_name="includeDomains"),
        exclude_domains=_split_values(excludeDomains, field_name="excludeDomains"),
        start_published_date=_optional_str(
            startPublishedDate,
            field_name="startPublishedDate",
        ),
        end_published_date=_optional_str(
            endPublishedDate,
            field_name="endPublishedDate",
        ),
        max_age_hours=_optional_int(maxAgeHours, field_name="maxAgeHours"),
        additional_queries=_split_values(
            additionalQueries,
            field_name="additionalQueries",
        ),
        content_type=contentType,  # type: ignore[arg-type]
        livecrawl=livecrawl,  # type: ignore[arg-type]
        context_max_characters=context_max,
        session_id=sessionID,
    )
    return ok(result)


@catch_errors
async def research_web_fetch(
    urls: str,
    query: str = "_",
    contentType: str = "text",
    livecrawl: str = "fallback",
    maxAgeHours: str = "_",
    subpages: str = "_",
    subpageTarget: str = "_",
    contextMaxCharacters: str = "_",
    sessionID: str = "_",
) -> dict[str, Any]:
    """Fetch/crawl public URLs through the Wayfinder Research Gateway.

    Args:
        urls: One or more public http(s) URLs, comma- or newline-separated.
        query: Optional highlight/summary query, or "_".
        contentType: Result content mode: text, highlights, or summary.
        livecrawl: Live crawl policy: "fallback" or "preferred".
        maxAgeHours: Optional freshness window in hours, or "_".
        subpages: Optional subpage count, or "_".
        subpageTarget: Optional comma/newline-separated subpage target hints.
        contextMaxCharacters: Optional excerpt character cap (500-50000). Use "_"
            to let the backend default apply.
        sessionID: Optional OpenCode session id. Use "_" to resolve from the
            runtime environment or SDK default.
    """
    parsed_urls = _split_values(urls, field_name="urls")
    if not parsed_urls:
        raise ValueError("urls is required")
    context_max = _optional_int(
        contextMaxCharacters,
        field_name="contextMaxCharacters",
    )
    result = await RESEARCH_CLIENT.fetch(
        urls=parsed_urls,
        query=_optional_str(query, field_name="query"),
        content_type=contentType,  # type: ignore[arg-type]
        livecrawl=livecrawl,  # type: ignore[arg-type]
        max_age_hours=_optional_int(maxAgeHours, field_name="maxAgeHours"),
        subpages=_optional_int(subpages, field_name="subpages"),
        subpage_target=_split_values(subpageTarget, field_name="subpageTarget"),
        context_max_characters=context_max,
        session_id=sessionID,
    )
    return ok(result)


@catch_errors
async def research_crypto_sentiment(sessionID: str = "_") -> dict[str, Any]:
    """Fetch crypto Fear & Greed sentiment through the Wayfinder Research Gateway."""
    return ok(await RESEARCH_CLIENT.crypto_sentiment(session_id=sessionID))


@catch_errors
async def research_social_x_search(
    query: str,
    allowedXHandles: str = "_",
    excludedXHandles: str = "_",
    fromDate: str = "_",
    toDate: str = "_",
    sessionID: str = "_",
) -> dict[str, Any]:
    """Search X through the Wayfinder Research Gateway.

    Args:
        query: Social/X search query.
        allowedXHandles: Optional comma/newline-separated handles to include.
        excludedXHandles: Optional comma/newline-separated handles to exclude.
        fromDate: Optional YYYY-MM-DD lower bound, or "_".
        toDate: Optional YYYY-MM-DD upper bound, or "_".
        sessionID: Optional OpenCode session id. Use "_" to resolve from runtime.
    """
    result = await RESEARCH_CLIENT.social_x_search(
        query=query,
        allowed_x_handles=_split_values(
            allowedXHandles,
            field_name="allowedXHandles",
            max_items=10,
        ),
        excluded_x_handles=_split_values(
            excludedXHandles,
            field_name="excludedXHandles",
            max_items=10,
        ),
        from_date=_optional_str(fromDate, field_name="fromDate"),
        to_date=_optional_str(toDate, field_name="toDate"),
        session_id=sessionID,
    )
    return ok(result)
