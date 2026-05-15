from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

BASE_URL = "https://api.llama.fi"
YIELDS_BASE_URL = "https://yields.llama.fi"
TIMEOUT_SECONDS = 20
ATTRIBUTION = "Data from DeFiLlama free API"


def _path_part(value: str, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if any(character in normalized for character in ("?", "#", "\n", "\r")):
        raise ValueError(f"{field_name} contains invalid characters")
    return quote(normalized, safe=":-_,")


class DefiLlamaFreeClient:
    """Direct DeFiLlama free API client.

    This intentionally does not call the Wayfinder backend.
    """

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        base_url: str = BASE_URL,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(f"{base_url}{path}", params=params or {})
            response.raise_for_status()
            body = response.json()

        return {
            "provider": "defillama_free",
            "url": str(response.url),
            "result": body,
            "evidence": [
                {
                    "provider": "defillama_free",
                    "sourceType": "api",
                    "url": str(response.url),
                    "clientDirect": True,
                    "attributionRequired": True,
                    "attribution": ATTRIBUTION,
                }
            ],
        }

    async def protocols(self) -> dict[str, Any]:
        return await self._get("/protocols")

    async def protocol(self, protocol_slug: str) -> dict[str, Any]:
        return await self._get(f"/protocol/{_path_part(protocol_slug, 'protocolSlug')}")

    async def tvl(self, protocol_slug: str) -> dict[str, Any]:
        return await self._get(f"/tvl/{_path_part(protocol_slug, 'protocolSlug')}")

    async def chains(self) -> dict[str, Any]:
        return await self._get("/v2/chains")

    async def stablecoins(self) -> dict[str, Any]:
        return await self._get("/stablecoins")

    async def yields_pools(self) -> dict[str, Any]:
        return await self._get("/pools", base_url=YIELDS_BASE_URL)

    async def current_prices(self, coins: str) -> dict[str, Any]:
        return await self._get(f"/prices/current/{_path_part(coins, 'coins')}")

    async def dex_overview(self, chain: str | None = None) -> dict[str, Any]:
        if chain:
            return await self._get(f"/overview/dexs/{_path_part(chain, 'chain')}")
        return await self._get("/overview/dexs")

    async def fees_overview(self, chain: str | None = None) -> dict[str, Any]:
        if chain:
            return await self._get(f"/overview/fees/{_path_part(chain, 'chain')}")
        return await self._get("/overview/fees")

    async def open_interest_overview(self) -> dict[str, Any]:
        return await self._get("/overview/open-interest")


DEFILLAMA_FREE_CLIENT = DefiLlamaFreeClient()
