from __future__ import annotations

import asyncio
from typing import Any

from hyperliquid.info import Info
from hyperliquid.utils import constants

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url

QUICKNODE_PROXIED_TYPES = frozenset(
    {
        "activeAssetData",
        "clearinghouseState",
        "delegations",
        "delegatorSummary",
        "exchangeStatus",
        "extraAgents",
        "frontendOpenOrders",
        "leadingVaults",
        "liquidatable",
        "maxBuilderFee",
        "maxMarketOrderNtls",
        "meta",
        "openOrders",
        "outcomeMeta",
        "perpDeployAuctionStatus",
        "perpDexs",
        "perpsAtOpenInterestCap",
        "settledOutcome",
        "spotClearinghouseState",
        "spotDeployState",
        "spotMeta",
        "subAccounts",
        "userFees",
        "userRateLimit",
        "userRole",
        "userToMultiSigSigners",
        "userVaultEquities",
        "validatorL1Votes",
        "vaultSummaries",
        "webData2",
    }
)

_PUBLIC_INFO = Info(constants.MAINNET_API_URL, skip_ws=True)


class HyperliquidQuicknodeInfoClient(WayfinderClient):
    async def post(self, body: dict[str, Any]) -> Any:
        if body["type"] in QUICKNODE_PROXIED_TYPES:
            url = f"{get_api_base_url()}/blockchain/hyperliquid/qn-info/"
            resp = await self._authed_request("POST", url, json=body)
            return resp.json()
        return await asyncio.to_thread(_PUBLIC_INFO.post, "/info", body)

    async def portfolio_state(self, user: str) -> dict[str, Any]:
        url = f"{get_api_base_url()}/blockchain/hyperliquid/portfolio-state/"
        resp = await self._authed_request("GET", url, params={"user": user})
        return resp.json()


HYPERLIQUID_QUICKNODE_INFO_CLIENT = HyperliquidQuicknodeInfoClient()
HYPERLIQUID_QUICKNODE_CLIENT = HYPERLIQUID_QUICKNODE_INFO_CLIENT
