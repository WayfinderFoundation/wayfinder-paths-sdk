from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    bar_interval_seconds,
)
from wayfinder_paths.jobs.execution.venues import MarketEvent

MAX_PAGES = 200
PAGE_LIMIT = 1000
RETRYABLE_ERRORS = {
    "RateLimitExceeded",
    "NetworkError",
    "DDoSProtection",
    "RequestTimeout",
    "ExchangeNotAvailable",
}


class CcxtMarketFeed:
    """Dataset-building MarketDataFeed over ccxt.async_support.

    NOT a venue: never registered in VENUE_REGISTRY and never listed in
    spec.venues — the live driver merges bars AND brokers from every venue
    adapter, and CCXT has no broker. Reachable only through dataset building
    (build_live_dataset --source ccxt), so backtests can use long CCXT history
    while forward execution stays on Hyperliquid.

    Bars are relabeled to CLOSE time (ccxt labels by open) so datasets align
    with HyperliquidMarketFeed output, and the symbol column keeps the coin
    name ("SNX", not "SNX/USDT:USDT") so datasets are drop-in for HL-symbol
    strategies.
    """

    def __init__(
        self,
        *,
        exchange_id: str = "binance",
        market_type: str = "swap",  # "swap" -> COIN/USDT:USDT, "spot" -> COIN/USDT
        quote: str = "USDT",
        exchange: Any | None = None,  # injectable fake for tests
        retries: int = 3,
    ) -> None:
        self.exchange_id = exchange_id
        self.market_type = market_type
        self.quote = quote
        self.retries = retries
        self.symbol_map: dict[str, str] = {}
        self._exchange = exchange
        self._adapter: Any | None = None
        self._markets: dict[str, Any] | None = None

    async def _get_exchange(self) -> Any:
        if self._exchange is None:
            # lazy: CCXTAdapter imports the full ccxt exchange registry (~0.5s);
            # injected test fakes and non-ccxt paths never pay it
            from wayfinder_paths.adapters.ccxt_adapter.adapter import CCXTAdapter

            self._adapter = CCXTAdapter(
                exchanges={self.exchange_id: {"enableRateLimit": True}}
            )
            self._exchange = getattr(self._adapter, self.exchange_id)
        return self._exchange

    async def resolve_market_symbol(self, coin: str) -> str:
        if coin in self.symbol_map:
            return self.symbol_map[coin]
        exchange = await self._get_exchange()
        if self._markets is None:
            self._markets = await exchange.load_markets()
        swap_pair = f"{coin}/{self.quote}:{self.quote}"
        spot_pair = f"{coin}/{self.quote}"
        candidates = (
            [swap_pair, spot_pair] if self.market_type == "swap" else [spot_pair]
        )
        for pair in candidates:
            match self._markets.get(pair):
                case {"active": active} if not active:
                    continue
                case dict() as market if market:
                    self.symbol_map[coin] = pair
                    return pair
        raise ValueError(
            f"no active {self.exchange_id} market for {coin!r}; tried {candidates}"
        )

    async def get_completed_bars(
        self,
        symbols: Sequence[str],
        interval: str,
        *,
        lookback_bars: int,
        as_of: datetime | None = None,
    ) -> CompletedBarsView:
        bar_seconds = bar_interval_seconds(interval)
        if not bar_seconds:
            raise ValueError(f"unsupported bar interval: {interval!r}")
        interval_ms = bar_seconds * 1000
        end_ms = int((as_of.timestamp() if as_of else time.time()) * 1000)
        start_ms = end_ms - lookback_bars * interval_ms
        rows: list[dict[str, Any]] = []
        for coin in symbols:
            pair = await self.resolve_market_symbol(coin)
            exchange = await self._get_exchange()
            candles: dict[int, list[float]] = {}
            cursor = start_ms
            pages = 0
            while cursor < end_ms and pages < MAX_PAGES:
                batch = await self._fetch_with_retry(
                    exchange, pair, interval, since=cursor
                )
                if not batch:
                    break
                for row in batch:
                    candles[int(row[0])] = list(row)
                last_ts = int(batch[-1][0])
                if last_ts <= cursor:
                    break
                cursor = last_ts + interval_ms
                pages += 1
            for open_ms, open_, high, low, close, volume in (
                candles[key] for key in sorted(candles)
            ):
                close_ms = int(open_ms) + interval_ms
                if close_ms > end_ms:
                    continue  # in-progress bar
                rows.append(
                    {
                        "timestamp": pd.Timestamp(close_ms, unit="ms", tz="UTC"),
                        "symbol": coin,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                    }
                )
        if not rows:
            raise RuntimeError(
                f"ccxt returned no completed bars for {list(symbols)} on "
                f"{self.exchange_id}"
            )
        return CompletedBarsView.from_rows(rows)

    async def get_events(
        self, symbols: Sequence[str], *, since: datetime | None = None
    ) -> list[MarketEvent]:
        return []

    async def close(self) -> None:
        if self._adapter is not None:
            await self._adapter.close()
            self._adapter = None
            self._exchange = None

    async def _fetch_with_retry(
        self, exchange: Any, pair: str, timeframe: str, *, since: int
    ) -> list[list[float]]:
        last_error: Exception | None = None
        for attempt in range(max(1, self.retries)):
            try:
                return await exchange.fetch_ohlcv(
                    pair, timeframe, since=since, limit=PAGE_LIMIT
                )
            except Exception as exc:
                last_error = exc
                retryable = type(exc).__name__ in RETRYABLE_ERRORS or "429" in str(exc)
                if not retryable or attempt >= self.retries - 1:
                    break
                await asyncio.sleep(0.25 * (2**attempt))
        raise RuntimeError(f"ccxt candle fetch failed for {pair}: {last_error}")


async def fetch_ccxt_dataset_rows(
    symbols: Sequence[str],
    interval: str,
    *,
    days: int,
    exchange_id: str = "binance",
    market_type: str = "swap",
    quote: str = "USDT",
    exchange: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch OHLCV rows for a backtest dataset, plus a metadata fragment
    recording exactly where the data came from (auditable substitution when
    the perp market is missing and spot was used)."""
    bar_seconds = bar_interval_seconds(interval)
    if not bar_seconds:
        raise ValueError(f"unsupported bar interval: {interval!r}")
    lookback_bars = max(2, math.ceil(days * 86_400 / bar_seconds))
    feed = CcxtMarketFeed(
        exchange_id=exchange_id,
        market_type=market_type,
        quote=quote,
        exchange=exchange,
    )
    try:
        view = await feed.get_completed_bars(
            symbols, interval, lookback_bars=lookback_bars
        )
    finally:
        await feed.close()
    metadata = {
        "exchange": exchange_id,
        "market_type": market_type,
        "quote": quote,
        "symbol_map": dict(feed.symbol_map),
        "label_convention": "close_time",
    }
    return view.to_rows(), metadata


async def fetch_ccxt_funding_rows(
    symbols: Sequence[str],
    *,
    days: int,
    exchange_id: str = "binance",
    quote: str = "USDT",
    exchange: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Historical funding rates as canonical feature rows.

    First-class carry data for perp strategies: rows come back in the
    features.jsonl shape ({timestamp, name: "funding", value, symbol}) so they
    as-of merge onto the bars like any other feature — no scratch scripts, no
    hand-copied JSONL. Works for any ccxt exchange with funding history
    (binance, hyperliquid, bybit, ...); perp market resolution and the
    injectable fake follow fetch_ccxt_dataset_rows.
    """
    feed = CcxtMarketFeed(
        exchange_id=exchange_id, market_type="swap", quote=quote, exchange=exchange
    )
    now_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    start_ms = now_ms - int(days) * 86_400_000
    rows: list[dict[str, Any]] = []
    per_symbol: dict[str, int] = {}
    errors: dict[str, str] = {}
    try:
        client = await feed._get_exchange()
        for coin in symbols:
            try:
                market = await feed.resolve_market_symbol(str(coin))
                since = start_ms
                count = 0
                for _page in range(200):  # hard cap: no infinite pagination
                    page = await client.fetch_funding_rate_history(
                        market, since=since, limit=500
                    )
                    if not page:
                        break
                    for item in page:
                        rate = item.get("fundingRate")
                        ts = item.get("timestamp")
                        if rate is None or ts is None:
                            continue
                        rows.append(
                            {
                                "timestamp": pd.Timestamp(
                                    int(ts), unit="ms", tz="UTC"
                                ).isoformat(),
                                "name": "funding",
                                "value": float(rate),
                                "symbol": str(coin),
                            }
                        )
                        count += 1
                    next_since = int(page[-1]["timestamp"]) + 1
                    if next_since <= since or next_since >= now_ms or len(page) < 2:
                        break
                    since = next_since
                per_symbol[str(coin)] = count
            except Exception as exc:  # noqa: BLE001 - preserve partial coverage
                per_symbol[str(coin)] = 0
                errors[str(coin)] = str(exc)
    finally:
        await feed.close()
    metadata = {
        "exchange": exchange_id,
        "quote": quote,
        "symbol_map": dict(feed.symbol_map),
        "feature": "funding",
        "days": int(days),
        "per_symbol": per_symbol,
        "errors": errors,
    }
    return rows, metadata
