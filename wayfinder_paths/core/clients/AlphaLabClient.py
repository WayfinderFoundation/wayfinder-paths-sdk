from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url

VALID_TYPES = {
    "twitter_post",
    "defi_llama_chain_flow",
    "delta_lab_top_apy",
    "delta_lab_best_delta_neutral",
}

VALID_SORT_FIELDS = {
    "insightfulness_score",
    "created",
    "-insightfulness_score",
    "-created",
}


class AlphaLabClient(WayfinderClient):
    async def search(
        self,
        *,
        scan_type: str | None = None,
        search: str | None = None,
        min_score: float | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        sort: str = "-insightfulness_score",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        url = f"{get_api_base_url()}/alpha-lab/results/"
        params: dict[str, str | int | float] = {
            "sort": sort,
            "limit": min(limit, 200),
            "offset": offset,
        }
        if scan_type is not None:
            if scan_type not in VALID_TYPES:
                raise ValueError(
                    f"Invalid type '{scan_type}'. Choose from: {sorted(VALID_TYPES)}"
                )
            params["type"] = scan_type
        if search is not None:
            params["search"] = search
        if min_score is not None:
            params["min_score"] = min_score
        if created_after is not None:
            params["created_after"] = created_after
        if created_before is not None:
            params["created_before"] = created_before
        if sort not in VALID_SORT_FIELDS:
            raise ValueError(
                f"Invalid sort '{sort}'. Choose from: {sorted(VALID_SORT_FIELDS)}"
            )
        response = await self._authed_request("GET", url, params=params)
        return response.json()


ALPHA_LAB_CLIENT = AlphaLabClient()
