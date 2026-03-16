from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.AlphaLabClient import ALPHA_LAB_CLIENT


async def search_alpha(
    query: str = "_",
    scan_type: str = "all",
    created_after: str = "_",
    created_before: str = "_",
    limit: str = "20",
) -> dict[str, Any]:
    """Search Alpha Lab insights. Sorted by insightfulness score (highest first).

    Args:
        query: Text search (case-insensitive). Use "_" for no filter.
        scan_type: "twitter_post", "defi_llama_chain_flow", "defi_llama_overview",
                  "defi_llama_protocol", "delta_lab_top_apy",
                  "delta_lab_best_delta_neutral", or "all".
        created_after: ISO 8601 datetime lower bound (e.g. "2026-03-06T00:00:00Z"). Use "_" to skip.
        created_before: ISO 8601 datetime upper bound. Use "_" to skip.
        limit: Max results (default "20", max "200").
    """
    try:
        kwargs: dict[str, Any] = {
            "sort": "-insightfulness_score",
            "limit": min(200, max(1, int(limit))),
        }
        type_value = scan_type.strip().lower()
        if type_value not in ("all", ""):
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
        return await ALPHA_LAB_CLIENT.search(**kwargs)
    except Exception as exc:
        return {"error": str(exc)}


async def get_alpha_types() -> list[str]:
    """Get available Alpha Lab scan types."""
    try:
        return await ALPHA_LAB_CLIENT.get_types()
    except Exception as exc:
        return [f"error: {exc}"]
