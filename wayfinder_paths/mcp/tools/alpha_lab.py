from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from wayfinder_paths.core.clients.AlphaLabClient import ALPHA_LAB_CLIENT
from wayfinder_paths.mcp.arg_validation import (
    normalize_enum,
    normalize_int,
    optional_iso8601,
)
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
AlphaScanType = Literal[
    "twitter_post",
    "defi_llama_chain_flow",
    "defi_llama_overview",
    "defi_llama_protocol",
    "delta_lab_top_apy",
    "delta_lab_best_delta_neutral",
    "all",
    "_",
]
IsoDateFilter = Annotated[
    str,
    Field(description="ISO-8601 date/timestamp, or '_' to omit this bound."),
]


@catch_errors
async def research_search_alpha(
    query: str = "_",
    scan_type: AlphaScanType = "all",
    created_after: IsoDateFilter = "_",
    created_before: IsoDateFilter = "_",
    limit: str | int = "20",
) -> dict[str, Any]:
    """Search Alpha Lab insights, highest insightfulness first.

    Use "_" for omitted filters. `scan_type` accepts twitter_post,
    defi_llama_chain_flow/overview/protocol, delta_lab_top_apy,
    delta_lab_best_delta_neutral, or all. Date bounds are ISO-8601; limit caps at 200.
    """
    kwargs: dict[str, Any] = {
        "sort": "-insightfulness_score",
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
    after = optional_iso8601(created_after, field_name="created_after")
    if after:
        kwargs["created_after"] = after
    before = optional_iso8601(created_before, field_name="created_before")
    if before:
        kwargs["created_before"] = before
    return ok(await ALPHA_LAB_CLIENT.search(**kwargs))


@catch_errors
async def research_get_alpha_types() -> dict[str, Any]:
    """Get available Alpha Lab scan types."""
    return ok(await ALPHA_LAB_CLIENT.get_types())
