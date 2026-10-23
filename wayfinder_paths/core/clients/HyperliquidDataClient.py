from __future__ import annotations

import time
from typing import NotRequired, Required, TypedDict

from wayfinder_paths.adapters.hyperliquid_adapter.utils import (
    spot_asset_ids,
    spot_index_from_asset_id,
    spot_info_coin,
)
from wayfinder_paths.core.clients.HyperliquidInfoClient import HYPERLIQUID_INFO_CLIENT
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
        self, coin: str, start_ms: int, end_ms: int, interval: str = "1h"
    ) -> list[CandleEntry]:
        data = await self.get_candles_response(coin, start_ms, end_ms, interval)
        return data.get("rows", [])

    async def get_candles_response(
        self, coin: str, start_ms: int, end_ms: int, interval: str = "1h"
    ) -> dict:
        if "/" in coin:
            # Port the jobs-v1 spot feed's public source, not its synthetic mid bar.
            # Resolve the exact pair; never substitute the underlying's perp history.
            meta = await HYPERLIQUID_INFO_CLIENT.post({"type": "spotMeta"})
            asset_id = spot_asset_ids(meta).get(coin)
            if asset_id is None:
                raise ValueError(f"Unknown Hyperliquid spot pair: {coin}")
            candle_coin = spot_info_coin(spot_index_from_asset_id(asset_id))
            rows = await HYPERLIQUID_INFO_CLIENT.post(
                {
                    "type": "candleSnapshot",
                    "req": {
                        "coin": candle_coin,
                        "interval": interval,
                        "startTime": start_ms,
                        "endTime": end_ms,
                    },
                }
            )
            if not isinstance(rows, list):
                raise ValueError("Hyperliquid spot candle response was not a list")
            cutoff = min(end_ms, int(time.time() * 1000))
            return {
                "coin": candle_coin,
                "asset_name": coin,
                "asset_id": asset_id,
                "quote_currency": coin.rsplit("/", 1)[1],
                "interval": interval,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "source": "Hyperliquid public spot candleSnapshot",
                "history_note": "Completed observations only; latest 5000 candles available. Gaps are not filled.",
                "rows": sorted(
                    (r for r in rows if start_ms <= int(r["t"]) < int(r["T"]) < cutoff),
                    key=lambda r: int(r["t"]),
                ),
            }
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
