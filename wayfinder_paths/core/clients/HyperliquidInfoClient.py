from __future__ import annotations

from typing import Any

import httpx

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url

# Mirrors vault-backend QN_SUPPORTED_INFO_TYPES — types where the backend's
# HLInfo routes through QuickNode (with public fallback). Anything outside
# this set has no QN benefit, so we skip the proxy hop and hit HL public
# directly to save the extra round-trip.
QN_PROXIED_TYPES = frozenset(
    {
        "clearinghouseState",
        "spotClearinghouseState",
        "frontendOpenOrders",
        "maxBuilderFee",
        "meta",
        "openOrders",
        "outcomeMeta",
        "perpDexs",
        "spotMeta",
    }
)

_PUBLIC_INFO_URL = "https://api.hyperliquid.xyz/info"


class HyperliquidInfoClient(WayfinderClient):
    def __init__(self) -> None:
        super().__init__()
        self._backend_url = f"{get_api_base_url()}/blockchain/hyperliquid/info/"

    async def post(self, body: dict[str, Any]) -> Any:
        if body["type"] in QN_PROXIED_TYPES:
            resp = await self._authed_request("POST", self._backend_url, json=body)
        else:
            async with httpx.AsyncClient(timeout=15.0) as c:
                resp = await c.post(_PUBLIC_INFO_URL, json=body)
        resp.raise_for_status()
        return resp.json()


HYPERLIQUID_INFO_CLIENT = HyperliquidInfoClient()
