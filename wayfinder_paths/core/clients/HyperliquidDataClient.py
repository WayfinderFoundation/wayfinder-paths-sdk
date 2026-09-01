from __future__ import annotations

import time
from typing import NotRequired, Required, TypedDict

import httpx

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class DataFeedError(RuntimeError):
    """A data-feed request failed for a reason the owner or agent can act on.

    `cause` is machine-readable: "out_of_credits" (owner action: top up API
    credits), "rate_limited" (transient: back off), or "http_<status>".
    Journal payloads stringify exceptions, so the cause rides the message
    too — downstream escalation can grep it from either place.
    """

    def __init__(self, cause: str, detail: str) -> None:
        super().__init__(f"{cause}: {detail}")
        self.cause = cause


def _feed_error(exc: httpx.HTTPStatusError) -> DataFeedError:
    status = exc.response.status_code
    if status == 402:
        cause = "out_of_credits"
    elif status == 429:
        cause = "rate_limited"
    else:
        cause = f"http_{status}"
    return DataFeedError(cause, str(exc)[:200])


class FundingHistoryEntry(TypedDict):
    time: Required[int]
    fundingRate: Required[str]
    premium: NotRequired[str]


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
        try:
            resp = await self._authed_request("GET", url, params=params)
        except httpx.HTTPStatusError as exc:
            raise _feed_error(exc) from exc
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
        try:
            resp = await self._authed_request("GET", url, params=params)
        except httpx.HTTPStatusError as exc:
            raise _feed_error(exc) from exc
        return resp.json()


HYPERLIQUID_DATA_CLIENT = HyperliquidDataClient()
