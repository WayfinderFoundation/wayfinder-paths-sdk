from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.AlphaLabClient import ALPHA_LAB_CLIENT


async def search_alpha(
    search: str = "",
    scan_type: str = "all",
    min_score: str = "0",
    created_after: str = "_",
    created_before: str = "_",
    sort: str = "-insightfulness_score",
    limit: str = "50",
    offset: str = "0",
) -> dict[str, Any]:
    """Search Alpha Lab for alpha insights (tweets, chain flows, APY, delta-neutral).

    Args:
        search: Text search query (case-insensitive, matches insight field). Use "_" for no filter.
        scan_type: Filter by type: "twitter_post", "defi_llama_chain_flow",
                  "delta_lab_top_apy", "delta_lab_best_delta_neutral". Use "all" for no filter.
        min_score: Minimum insightfulness score 0-1 (default: "0")
        created_after: ISO 8601 datetime lower bound (e.g. "2026-03-01T00:00:00Z"). Use "_" to skip.
        created_before: ISO 8601 datetime upper bound. Use "_" to skip.
        sort: Sort field (default: "-insightfulness_score"). Options:
              "insightfulness_score", "-insightfulness_score", "created", "-created"
        limit: Max results (default: "50", max: "200")
        offset: Pagination offset (default: "0")

    Returns:
        Dict with "count" (total matching) and "results" list of alpha insights
    """
    try:
        kwargs: dict[str, Any] = {
            "sort": sort.strip(),
            "limit": min(200, max(1, int(limit))),
            "offset": max(0, int(offset)),
        }
        type_value = scan_type.strip().lower()
        if type_value not in ("all", ""):
            kwargs["scan_type"] = type_value
        search_value = search.strip()
        if search_value and search_value != "_":
            kwargs["search"] = search_value
        score = float(min_score)
        if score > 0:
            kwargs["min_score"] = score
        after = created_after.strip()
        if after and after != "_":
            kwargs["created_after"] = after
        before = created_before.strip()
        if before and before != "_":
            kwargs["created_before"] = before
        return await ALPHA_LAB_CLIENT.search(**kwargs)
    except Exception as exc:
        return {"error": str(exc)}
