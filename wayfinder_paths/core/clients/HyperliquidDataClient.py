from __future__ import annotations

import time
from typing import NotRequired, Required, TypedDict

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
    v: NotRequired[str | None]
    n: NotRequired[int | None]


class HyperliquidDataClient(WayfinderClient):
    def __init__(self) -> None:
        super().__init__()
        self.api_base_url = f"{get_api_base_url()}/blockchain/hyperliquid"

    async def get_funding_history(
        self, coin: str, start_ms: int, end_ms: int
    ) -> list[FundingHistoryEntry]:
        data = await self.get_funding_history_response(coin, start_ms, end_ms)
        return data.get("rows", [])

    async def get_funding_history_response(
        self, coin: str, start_ms: int, end_ms: int
    ) -> dict:
        url = f"{self.api_base_url}/funding/"
        params = {"coin": coin, "start_ms": start_ms, "end_ms": end_ms}
        resp = await self._authed_request("GET", url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_candles(
        self,
        coin: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        interval: str = "1h",
        *,
        lookback_hours: int | None = None,
    ) -> list[CandleEntry]:
        data = await self.get_candles_response(
            coin,
            start_ms=start_ms,
            end_ms=end_ms,
            interval=interval,
            lookback_hours=lookback_hours,
        )
        return data.get("rows", [])

    async def get_candles_response(
        self,
        coin: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        interval: str = "1h",
        *,
        lookback_hours: int | None = None,
    ) -> dict:
        if start_ms is None or end_ms is None:
            if lookback_hours is None:
                raise TypeError(
                    "get_candles requires start_ms/end_ms or lookback_hours"
                )
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - int(lookback_hours) * 60 * 60 * 1000
        url = f"{self.api_base_url}/candles/"
        params = {
            "coin": coin,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "interval": interval,
        }
        resp = await self._authed_request("GET", url, params=params)
        resp.raise_for_status()
        return resp.json()


HYPERLIQUID_DATA_CLIENT = HyperliquidDataClient()
