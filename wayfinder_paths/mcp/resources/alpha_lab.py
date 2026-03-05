from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.AlphaLabClient import ALPHA_LAB_CLIENT


async def search_alpha(
    search: str = "",
    scan_type: str = "all",
    min_score: str = "0",
    sort: str = "-insightfulness_score",
    limit: str = "50",
    offset: str = "0",
) -> dict[str, Any]:
    """Search Alpha Lab for alpha insights (tweets, chain flows, APY, delta-neutral).

    Args:
        search: Text search query (case-insensitive, matches insight field). Empty = no filter.
        scan_type: Filter by type: "twitter_post", "defi_llama_chain_flow",
                  "delta_lab_top_apy", "delta_lab_best_delta_neutral". Use "all" for no filter.
        min_score: Minimum insightfulness score 0-1 (default: "0")
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
        if search_value:
            kwargs["search"] = search_value
        score = float(min_score)
        if score > 0:
            kwargs["min_score"] = score
        return await ALPHA_LAB_CLIENT.search(**kwargs)
    except Exception as exc:
        return {"error": str(exc)}
