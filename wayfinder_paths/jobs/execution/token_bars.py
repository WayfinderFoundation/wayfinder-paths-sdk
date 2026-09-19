"""Completed OHLCV bars for on-chain tokens, read by window from the on-chain
data source's persisted store.

One consumer for every reader of token history: the `onchain` venue feed,
the dataset fetch, and `token_price` feature feeds. The source answers
`[start_ms, end_ms)` on candle OPEN with rows oldest first; bars come back
close-labelled (the convention every other bar source uses) and anything
whose close lies past the window end is dropped, so the forming candle never
reaches a strategy. Nothing here touches disk: the store lives server-side.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.constants.chains import CHAIN_CODE_TO_ID
from wayfinder_paths.core.constants.contracts import ZERO_ADDRESS
from wayfinder_paths.core.utils.token_refs import parse_token_id_to_chain_and_address
from wayfinder_paths.core.utils.token_resolver import TokenResolver
from wayfinder_paths.quant.pattern_match_context import INTERVAL_MS, SUPPORTED_INTERVALS

# The store refuses windows past 50,000 bars; chunk well under that.
MAX_BARS_PER_REQUEST = 20_000
_INTERVALS_TEXT = "|".join(SUPPORTED_INTERVALS)


@dataclass(frozen=True, slots=True, order=True)
class Bar:
    open_ms: int
    close_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None


@dataclass(frozen=True)
class TokenBarWindow:
    token_id: str
    chain_id: int
    address: str
    interval: str
    start_ms: int
    end_ms: int
    bars: tuple[Bar, ...]  # oldest first, every close_ms <= end_ms
    history_start_ms: int | None  # the source's earliest candle, when it knows
    requests: int


def is_token_symbol(symbol: Any) -> bool:
    """Token ids are `<coingecko_id>-<chain_code>` or `<chain_code|chain_id>_<address>`;
    perp coins (`BTC`), spot pairs (`HYPE/USDC`) and market slugs are not."""
    text = str(symbol or "").strip()
    if not text or "/" in text or ":" in text:
        return False
    if parse_token_id_to_chain_and_address(text)[0] is not None:
        return True
    prefix, separator, suffix = text.rpartition("-")
    return bool(separator and prefix) and suffix.lower() in CHAIN_CODE_TO_ID


def bar_window(interval: str, *, lookback_bars: int, end_ms: int) -> tuple[int, int]:
    """The `[start, end)` window on candle opens that yields `lookback_bars`
    completed bars ending at `end_ms`."""
    step = _step_ms(interval)
    return end_ms - max(1, int(lookback_bars)) * step, end_ms


def candle_coin(token_id: str, address: str) -> str:
    """What the source is asked for: the address, except that a chain's gas
    token resolves to the zero address, which no pool trades — the source
    maps the coingecko id to the wrapped token itself."""
    if address.lower() == ZERO_ADDRESS.lower():
        prefix, separator, suffix = token_id.rpartition("-")
        return prefix if separator and suffix.lower() in CHAIN_CODE_TO_ID else token_id
    return address


async def resolve_token_feed(
    feed: Mapping[str, Any], *, resolver: type[TokenResolver] = TokenResolver
) -> dict[str, Any]:
    """Pin chain_id and address for a token id; a no-op when already pinned."""
    resolved = dict(feed)
    if resolved.get("chain_id") and resolved.get("address"):
        return resolved
    chain_id, address = await resolver.resolve_token(str(resolved["token_id"]))
    resolved["chain_id"] = int(chain_id)
    resolved["address"] = str(address)
    return resolved


async def resolve_token_symbols(
    symbols: Sequence[str], *, resolver: type[TokenResolver] = TokenResolver
) -> dict[str, dict[str, Any]]:
    """Pin every declared token id once; the first bad id names itself."""
    pinned: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        if not is_token_symbol(symbol):
            raise ValueError(
                f"{symbol!r} is not a token id: use <coingecko_id>-<chain_code> "
                "(ethereum-robinhood) or <chain_code>_<address>"
            )
        resolved = await resolve_token_feed({"token_id": symbol}, resolver=resolver)
        pinned[str(symbol)] = {
            "chain_id": resolved["chain_id"],
            "address": resolved["address"],
        }
    return pinned


def _step_ms(interval: str) -> int:
    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(
            f"the on-chain data source serves {_INTERVALS_TEXT}; got {interval!r}"
        )
    return int(INTERVAL_MS[interval])


def _parse_rows(rows: Sequence[Mapping[str, Any]], step: int) -> dict[int, Bar]:
    """Rows keyed by open; a row whose close is missing, non-finite or zero is
    a source hole and is skipped. The close time is recomputed from the grid."""
    by_open: dict[int, Bar] = {}
    for row in rows:
        try:
            open_ms = int(row["t"])
            close = float(row["c"])
            open_ = float(row["o"])
            high = float(row["h"])
            low = float(row["l"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not all(math.isfinite(value) for value in (open_, high, low, close))
            or close <= 0
        ):
            continue
        raw_volume = row.get("v")
        volume = None if raw_volume is None or raw_volume == "" else float(raw_volume)
        by_open[open_ms] = Bar(open_ms, open_ms + step, open_, high, low, close, volume)
    return by_open


async def fetch_token_bar_window(
    token_id: str,
    interval: str,
    *,
    start_ms: int,
    end_ms: int,
    client: Any = TOKEN_CLIENT,
    resolver: type[TokenResolver] = TokenResolver,
    chain_id: int | None = None,
    address: str | None = None,
) -> TokenBarWindow:
    """Completed bars with opens in `[start_ms, end_ms)` and closes at or
    before `end_ms`, fetched in bounded chunks."""
    step = _step_ms(interval)
    if end_ms <= start_ms:
        raise ValueError(f"empty bar window: start {start_ms} >= end {end_ms}")
    if chain_id is None or address is None:
        pinned = await resolve_token_feed({"token_id": token_id}, resolver=resolver)
        chain_id, address = int(pinned["chain_id"]), str(pinned["address"])
    coin = candle_coin(token_id, address)
    by_open: dict[int, Bar] = {}
    history_start_ms: int | None = None
    requests = 0
    chunk = MAX_BARS_PER_REQUEST * step
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(end_ms, cursor + chunk)
        page = await client.get_candles_window(
            coin, interval, chain_id=int(chain_id), start_ms=cursor, end_ms=chunk_end
        )
        requests += 1
        by_open.update(_parse_rows(page.get("rows") or [], step))
        if page.get("history_start_ms") is not None:
            history_start_ms = int(page["history_start_ms"])
        cursor = chunk_end
    bars = tuple(
        bar
        for _, bar in sorted(by_open.items())
        if start_ms <= bar.open_ms and bar.close_ms <= end_ms
    )
    return TokenBarWindow(
        token_id=token_id,
        chain_id=int(chain_id),
        address=str(address),
        interval=interval,
        start_ms=start_ms,
        end_ms=end_ms,
        bars=bars,
        history_start_ms=history_start_ms,
        requests=requests,
    )


async def fetch_token_bars(
    token_id: str,
    interval: str,
    *,
    start_ms: int,
    end_ms: int,
    client: Any = TOKEN_CLIENT,
    resolver: type[TokenResolver] = TokenResolver,
) -> list[Bar]:
    window = await fetch_token_bar_window(
        token_id,
        interval,
        start_ms=start_ms,
        end_ms=end_ms,
        client=client,
        resolver=resolver,
    )
    return list(window.bars)


def close_labelled_rows(symbol: str, bars: Sequence[Bar]) -> list[dict[str, Any]]:
    """`CompletedBarsView` rows: the timestamp is the bar's close, as a
    tz-aware Timestamp (raw ms integers parse as nanoseconds)."""
    return [
        {
            "timestamp": pd.Timestamp(bar.close_ms, unit="ms", tz="UTC"),
            "symbol": symbol,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in bars
    ]


class TokenBarReader:
    """One per adapter or op: identical windows within its lifetime are
    answered from memory, so a tick over several symbols and the dataset
    fetch that follows it never ask the store twice."""

    def __init__(
        self,
        *,
        client: Any = TOKEN_CLIENT,
        resolver: type[TokenResolver] = TokenResolver,
    ) -> None:
        self.client = client
        self.resolver = resolver
        self._windows: dict[tuple[int, str, str, int, int], TokenBarWindow] = {}

    async def window(
        self,
        token_id: str,
        interval: str,
        *,
        start_ms: int,
        end_ms: int,
        chain_id: int | None = None,
        address: str | None = None,
    ) -> TokenBarWindow:
        if chain_id is None or address is None:
            pinned = await resolve_token_feed(
                {"token_id": token_id}, resolver=self.resolver
            )
            chain_id, address = int(pinned["chain_id"]), str(pinned["address"])
        key = (int(chain_id), str(address).lower(), interval, start_ms, end_ms)
        cached = self._windows.get(key)
        if cached is not None:
            return cached
        window = await fetch_token_bar_window(
            token_id,
            interval,
            start_ms=start_ms,
            end_ms=end_ms,
            client=self.client,
            resolver=self.resolver,
            chain_id=int(chain_id),
            address=str(address),
        )
        self._windows[key] = window
        return window
