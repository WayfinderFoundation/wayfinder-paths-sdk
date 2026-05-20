from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.ResearchClient import (
    RESEARCH_CLIENT,
    VALID_CONTENT_TYPES,
    VALID_LIVECRAWL_VALUES,
    VALID_SEARCH_CATEGORIES,
    VALID_SEARCH_TYPES,
)
from wayfinder_paths.mcp.arg_validation import (
    MCPArgumentError,
    normalize_enum,
    normalize_int,
    optional_int,
    optional_str,
    split_values,
)
from wayfinder_paths.mcp.utils import catch_errors, ok


def _search_type_and_category(
    search_type: Any, category: Any
) -> tuple[str, str | None]:
    category_value = optional_str(category)
    normalized_type = str(search_type).strip().lower()
    if category_value is None and normalized_type in VALID_SEARCH_CATEGORIES:
        return "auto", normalized_type
    try:
        resolved_type = normalize_enum(
            search_type,
            field_name="type",
            allowed_values=VALID_SEARCH_TYPES,
        )
    except MCPArgumentError as exc:
        if normalized_type in VALID_SEARCH_CATEGORIES:
            raise MCPArgumentError(
                "type is a search mode, not a content category. "
                f"Use type='auto' and category='{normalized_type}'.",
                field="type",
                received=search_type,
                allowed_values=VALID_SEARCH_TYPES,
                suggested_arguments={"type": "auto", "category": normalized_type},
            ) from exc
        raise
    if category_value is None:
        return resolved_type, None
    resolved_category = normalize_enum(
        category_value,
        field_name="category",
        allowed_values=VALID_SEARCH_CATEGORIES,
    )
    return resolved_type, resolved_category


@catch_errors
async def core_web_search(
    query: str,
    numResults: str | int = "8",
    type: str = "auto",
    category: str = "_",
    includeDomains: str | list[str] = "_",
    excludeDomains: str | list[str] = "_",
    startPublishedDate: str = "_",
    endPublishedDate: str = "_",
    maxAgeHours: str | int = "_",
    additionalQueries: str | list[str] = "_",
    contentType: str = "highlights",
    livecrawl: str = "fallback",
    contextMaxCharacters: str | int = "_",
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
    search_type, category_value = _search_type_and_category(type, category)
    context_max = optional_int(
        contextMaxCharacters,
        field_name="contextMaxCharacters",
        min_value=500,
        max_value=50_000,
    )
    result = await RESEARCH_CLIENT.search(
        query=query,
        num_results=normalize_int(
            numResults,
            field_name="numResults",
            min_value=1,
            max_value=100,
        ),
        search_type=search_type,  # type: ignore[arg-type]
        category=category_value,  # type: ignore[arg-type]
        include_domains=split_values(includeDomains, field_name="includeDomains"),
        exclude_domains=split_values(excludeDomains, field_name="excludeDomains"),
        start_published_date=optional_str(
            startPublishedDate,
            field_name="startPublishedDate",
        ),
        end_published_date=optional_str(
            endPublishedDate,
            field_name="endPublishedDate",
        ),
        max_age_hours=optional_int(
            maxAgeHours,
            field_name="maxAgeHours",
            min_value=0,
            max_value=720,
        ),
        additional_queries=split_values(
            additionalQueries,
            field_name="additionalQueries",
        ),
        content_type=normalize_enum(
            contentType,
            field_name="contentType",
            allowed_values=VALID_CONTENT_TYPES,
        ),  # type: ignore[arg-type]
        livecrawl=normalize_enum(
            livecrawl,
            field_name="livecrawl",
            allowed_values=VALID_LIVECRAWL_VALUES,
        ),  # type: ignore[arg-type]
        context_max_characters=context_max,
        session_id=sessionID,
    )
    return ok(result)


@catch_errors
async def core_web_fetch(
    urls: str | list[str],
    query: str = "_",
    contentType: str = "text",
    livecrawl: str = "fallback",
    maxAgeHours: str | int = "_",
    subpages: str | int = "_",
    subpageTarget: str | list[str] = "_",
    contextMaxCharacters: str | int = "_",
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
    parsed_urls = split_values(urls, field_name="urls")
    if not parsed_urls:
        raise MCPArgumentError("urls is required", field="urls", received=urls)
    context_max = optional_int(
        contextMaxCharacters,
        field_name="contextMaxCharacters",
        min_value=500,
        max_value=50_000,
    )
    result = await RESEARCH_CLIENT.fetch(
        urls=parsed_urls,
        query=optional_str(query, field_name="query"),
        content_type=normalize_enum(
            contentType,
            field_name="contentType",
            allowed_values=VALID_CONTENT_TYPES,
        ),  # type: ignore[arg-type]
        livecrawl=normalize_enum(
            livecrawl,
            field_name="livecrawl",
            allowed_values=VALID_LIVECRAWL_VALUES,
        ),  # type: ignore[arg-type]
        max_age_hours=optional_int(
            maxAgeHours,
            field_name="maxAgeHours",
            min_value=0,
            max_value=720,
        ),
        subpages=optional_int(
            subpages,
            field_name="subpages",
            min_value=0,
            max_value=10,
        ),
        subpage_target=split_values(subpageTarget, field_name="subpageTarget"),
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
    allowedXHandles: str | list[str] = "_",
    excludedXHandles: str | list[str] = "_",
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
        allowed_x_handles=split_values(
            allowedXHandles,
            field_name="allowedXHandles",
            max_items=10,
        ),
        excluded_x_handles=split_values(
            excludedXHandles,
            field_name="excludedXHandles",
            max_items=10,
        ),
        from_date=optional_str(fromDate, field_name="fromDate"),
        to_date=optional_str(toDate, field_name="toDate"),
        session_id=sessionID,
    )
    return ok(result)
