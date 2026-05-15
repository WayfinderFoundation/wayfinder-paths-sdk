from __future__ import annotations

from typing import Any, Literal, NotRequired, Required, TypedDict

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url

PolymarketSort = Literal["trending", "volume24h", "liquidity", "fresh"]
PolymarketStatus = Literal["active", "closed", "all"]


class PolymarketMarket(TypedDict):
    id: Required[str]
    type: Required[str]
    symbol: Required[str]
    imageUrl: Required[str]
    lastPrice: Required[float]
    change24h: NotRequired[float | None]
    volume24h: Required[float]
    yesPrice: Required[float]
    noPrice: Required[float]
    yesLabel: Required[str]
    noLabel: Required[str]
    liquidity: Required[float]
    resolvesAt: Required[str]
    slug: Required[str]
    eventSlug: Required[str]
    conditionId: Required[str]
    yesTokenId: Required[str]
    noTokenId: Required[str]


class PolymarketClient(WayfinderClient):
    async def search_markets(
        self,
        *,
        query: str | None = None,
        limit: int = 20,
        sort: PolymarketSort = "trending",
        status: PolymarketStatus = "active",
    ) -> list[PolymarketMarket]:
        url = f"{get_api_base_url()}/blockchain/polymarket/markets/"
        params: dict[str, Any] = {"limit": limit, "sort": sort, "status": status}
        if query:
            params["query"] = query
        response = await self._authed_request("GET", url, params=params)
        data = response.json()
        if not isinstance(data, list):
            raise ValueError(
                f"Unexpected polymarket markets response: {type(data).__name__}"
            )
        return data


POLYMARKET_CLIENT = PolymarketClient()
