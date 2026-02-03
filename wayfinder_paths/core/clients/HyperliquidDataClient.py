from __future__ import annotations

from typing import Required, TypedDict

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class FundingHistoryEntry(TypedDict):
    time: Required[int]
    fundingRate: Required[str]


class CandleEntry(TypedDict):
    t: Required[int]
    T: Required[int]
    o: Required[str | None]
    h: Required[str | None]
    l: Required[str | None]  # noqa: E741
    c: Required[str | None]


class HyperliquidDataClient(WayfinderClient):
    def __init__(self) -> None:
        super().__init__()
        self.api_base_url = f"{get_api_base_url()}/blockchain/hyperliquid"

    async def get_funding_history(
        self, coin: str, start_ms: int, end_ms: int
    ) -> list[FundingHistoryEntry]:
        url = f"{self.api_base_url}/funding/"
        params = {"coin": coin, "start_ms": start_ms, "end_ms": end_ms}
        resp = await self._authed_request("GET", url, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("rows", [])

    async def get_candles(
        self, coin: str, start_ms: int, end_ms: int, interval: str = "1h"
    ) -> list[CandleEntry]:
        url = f"{self.api_base_url}/candles/"
        params = {
            "coin": coin,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "interval": interval,
        }
        resp = await self._authed_request("GET", url, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("rows", [])


HYPERLIQUID_DATA_CLIENT = HyperliquidDataClient()
