from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.AlphaLabClient import (
    ALPHA_LAB_CLIENT,
    VALID_SORT_FIELDS,
)
from wayfinder_paths.mcp.arg_validation import normalize_enum, normalize_int
from wayfinder_paths.mcp.utils import catch_errors, ok

SCAN_TYPES = {
    "twitter_post",
    "defi_llama_chain_flow",
    "defi_llama_overview",
    "defi_llama_protocol",
    "delta_lab_top_apy",
    "delta_lab_best_delta_neutral",
    "all",
    "_",
    "",
}


@catch_errors
async def research_search_alpha(
    query: str = "_",
    scan_type: str = "all",
    created_after: str = "_",
    created_before: str = "_",
    sort: str = "-insightfulness_score",
    limit: str | int = "20",
) -> dict[str, Any]:
    """Search Alpha Lab insights. Sorted by insightfulness score (highest first).

    Args:
        query: Text search (case-insensitive). Use "_" for no filter.
        scan_type: "twitter_post", "defi_llama_chain_flow", "defi_llama_overview",
                  "defi_llama_protocol", "delta_lab_top_apy",
                  "delta_lab_best_delta_neutral", or "all".
        created_after: ISO 8601 datetime lower bound (e.g. "2026-03-06T00:00:00Z"). Use "_" to skip.
        created_before: ISO 8601 datetime upper bound. Use "_" to skip.
        sort: "-insightfulness_score" (default, best first) or "-created" (newest
              first — use this for fresh/latest insights; the score sort surfaces
              all-time top scorers regardless of age).
        limit: Max results (default "20", max "200").
    """
    kwargs: dict[str, Any] = {
        "sort": normalize_enum(
            sort, field_name="sort", allowed_values=VALID_SORT_FIELDS
        ),
        "limit": min(200, normalize_int(limit, field_name="limit", min_value=1)),
    }
    type_value = normalize_enum(
        scan_type,
        field_name="scan_type",
        allowed_values=SCAN_TYPES,
    )
    if type_value not in ("all", "", "_"):
        kwargs["scan_type"] = type_value
    search_value = query.strip()
    if search_value and search_value != "_":
        kwargs["search"] = search_value
    after = created_after.strip()
    if after and after != "_":
        kwargs["created_after"] = after
    before = created_before.strip()
    if before and before != "_":
        kwargs["created_before"] = before
    return ok(await ALPHA_LAB_CLIENT.search(**kwargs))


@catch_errors
async def research_get_alpha_types() -> dict[str, Any]:
    """Get available Alpha Lab scan types."""
    return ok(await ALPHA_LAB_CLIENT.get_types())
