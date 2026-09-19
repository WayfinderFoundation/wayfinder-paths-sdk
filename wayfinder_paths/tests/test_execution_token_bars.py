"""Token bars: one window-based consumer of the on-chain data source's store."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pandas as pd
import pytest

from wayfinder_paths.core.constants.contracts import ZERO_ADDRESS
from wayfinder_paths.jobs.execution import token_bars as tb

HOUR_MS = 3_600_000
WETH_BASE = "0x4200000000000000000000000000000000000006"


class FakeCandleClient:
    """Rows oldest first, opens in `[start_ms, end_ms)`, string values, and
    the store's history floor; records every request."""

    def __init__(self, candles: list[dict[str, Any]], *, history_start: bool = True):
        self.candles = sorted(candles, key=lambda row: int(row["t"]))
        self.history_start = history_start
        self.calls: list[dict[str, Any]] = []

    async def get_candles_window(self, coin, interval, *, chain_id, start_ms, end_ms):
        self.calls.append(
            {
                "coin": coin,
                "interval": interval,
                "chain_id": chain_id,
                "start": start_ms,
                "end": end_ms,
            }
        )
        return {
            "rows": [row for row in self.candles if start_ms <= int(row["t"]) < end_ms],
            "chain_id": chain_id,
            "address": coin,
            "history_start_ms": int(self.candles[0]["t"])
            if self.history_start
            else None,
        }


class _Pinned:
    calls = 0

    @classmethod
    async def resolve_token(cls, query, *, chain_id=None):
        cls.calls += 1
        return 8453, WETH_BASE


class _Refusing:
    @classmethod
    async def resolve_token(cls, query, *, chain_id=None):
        raise AssertionError("pinned ids must not resolve again")


def _hourly(
    count: int, *, end_open_ms: int, start_price: float = 100.0
) -> list[dict[str, Any]]:
    return [
        {
            "t": end_open_ms - HOUR_MS * (count - 1 - index),
            "o": str(start_price + index),
            "h": str(start_price + 1 + index),
            "l": str(start_price - 1 + index),
            "c": str(start_price + 0.5 + index),
            "v": "1",
        }
        for index in range(count)
    ]


def _this_open_ms() -> int:
    now_ms = int(time.time() * 1000)
    return now_ms - (now_ms % HOUR_MS)


def test_is_token_symbol_table() -> None:
    assert tb.is_token_symbol("ethereum-robinhood")
    assert tb.is_token_symbol("usd-coin-polygon")
    assert tb.is_token_symbol("base_" + WETH_BASE)
    assert tb.is_token_symbol("8453_" + WETH_BASE)
    assert not tb.is_token_symbol("BTC")
    assert not tb.is_token_symbol("HYPE/USDC")
    assert not tb.is_token_symbol("polymarket:slug:YES")
    assert not tb.is_token_symbol("bitcoin")
    assert not tb.is_token_symbol("")


def test_window_is_fetched_in_chunks_and_close_labelled(monkeypatch) -> None:
    monkeypatch.setattr(tb, "MAX_BARS_PER_REQUEST", 10)
    this_open = _this_open_ms()
    client = FakeCandleClient(_hourly(30, end_open_ms=this_open))
    start_ms, end_ms = this_open - 24 * HOUR_MS, this_open + 30 * 60_000

    window = asyncio.run(
        tb.fetch_token_bar_window(
            "ethereum-base",
            "1h",
            start_ms=start_ms,
            end_ms=end_ms,
            client=client,
            resolver=_Pinned,
        )
    )

    assert window.requests == 3 and [c["start"] for c in client.calls] == [
        start_ms,
        start_ms + 10 * HOUR_MS,
        start_ms + 20 * HOUR_MS,
    ]
    assert client.calls[0]["coin"] == WETH_BASE and client.calls[0]["chain_id"] == 8453
    assert window.chain_id == 8453 and window.address == WETH_BASE
    assert window.history_start_ms == int(client.candles[0]["t"])
    # 24 completed bars; the forming one (open = this hour) closes after end_ms
    assert len(window.bars) == 24
    assert window.bars[0].open_ms == start_ms
    assert window.bars[-1].close_ms == this_open and window.bars[-1].close == 100.5 + 28
    assert all(bar.close_ms == bar.open_ms + HOUR_MS for bar in window.bars)
    assert window.bars[0].volume == 1.0


def test_source_holes_and_duplicates_are_dropped() -> None:
    this_open = _this_open_ms()
    rows = _hourly(5, end_open_ms=this_open - HOUR_MS)
    rows[1]["c"] = "0"
    rows[2]["c"] = "nan"
    rows.append(dict(rows[3], c="999"))  # a restated candle: last one wins
    client = FakeCandleClient(rows)

    bars = asyncio.run(
        tb.fetch_token_bars(
            "ethereum-base",
            "1h",
            start_ms=this_open - 6 * HOUR_MS,
            end_ms=this_open,
            client=client,
            resolver=_Pinned,
        )
    )

    assert [bar.close for bar in bars] == [100.5, 999.0, 104.5]


def test_pinned_ids_skip_the_resolver_and_natives_send_the_coingecko_id() -> None:
    this_open = _this_open_ms()
    client = FakeCandleClient(_hourly(3, end_open_ms=this_open - HOUR_MS))

    window = asyncio.run(
        tb.fetch_token_bar_window(
            "ethereum-robinhood",
            "1h",
            start_ms=this_open - 3 * HOUR_MS,
            end_ms=this_open,
            client=client,
            resolver=_Refusing,
            chain_id=4663,
            address=ZERO_ADDRESS,
        )
    )

    assert client.calls[0]["coin"] == "ethereum" and client.calls[0]["chain_id"] == 4663
    assert window.address == ZERO_ADDRESS and len(window.bars) == 3
    assert tb.candle_coin("base_" + WETH_BASE, WETH_BASE) == WETH_BASE
    assert tb.candle_coin("weird", ZERO_ADDRESS) == "weird"


def test_refusals_name_the_fix() -> None:
    client = FakeCandleClient([])
    with pytest.raises(ValueError, match="1m\\|5m\\|15m\\|1h\\|4h\\|1d"):
        asyncio.run(
            tb.fetch_token_bar_window(
                "ethereum-base",
                "2h",
                start_ms=0,
                end_ms=10,
                client=client,
                resolver=_Pinned,
            )
        )
    with pytest.raises(ValueError, match="empty bar window"):
        asyncio.run(
            tb.fetch_token_bar_window(
                "ethereum-base",
                "1h",
                start_ms=10,
                end_ms=10,
                client=client,
                resolver=_Pinned,
            )
        )
    with pytest.raises(ValueError, match="not a token id"):
        asyncio.run(tb.resolve_token_symbols(["BTC"], resolver=_Pinned))


def test_bar_window_and_close_labelled_rows() -> None:
    start_ms, end_ms = tb.bar_window("5m", lookback_bars=4, end_ms=1_000_000)
    assert (start_ms, end_ms) == (1_000_000 - 4 * 300_000, 1_000_000)
    bar = tb.Bar(HOUR_MS, 2 * HOUR_MS, 1.0, 2.0, 0.5, 1.5, None)
    (row,) = tb.close_labelled_rows("ethereum-base", [bar])
    assert row["timestamp"] == pd.Timestamp(2 * HOUR_MS, unit="ms", tz="UTC")
    assert row["timestamp"].tzinfo is not None
    assert row == {
        "timestamp": row["timestamp"],
        "symbol": "ethereum-base",
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": None,
    }


def test_reader_memoises_identical_windows_and_resolves_once() -> None:
    this_open = _this_open_ms()
    client = FakeCandleClient(_hourly(6, end_open_ms=this_open - HOUR_MS))
    _Pinned.calls = 0
    reader = tb.TokenBarReader(client=client, resolver=_Pinned)
    kwargs = {"start_ms": this_open - 6 * HOUR_MS, "end_ms": this_open}

    first = asyncio.run(reader.window("ethereum-base", "1h", **kwargs))
    second = asyncio.run(reader.window("ethereum-base", "1h", **kwargs))
    pinned = asyncio.run(
        reader.window("ethereum-base", "1h", chain_id=8453, address=WETH_BASE, **kwargs)
    )

    assert first is second is pinned and len(client.calls) == 1
    assert _Pinned.calls == 2, "unpinned calls resolve; the pinned one does not"
    assert asyncio.run(
        tb.resolve_token_symbols(["ethereum-base"], resolver=_Pinned)
    ) == {"ethereum-base": {"chain_id": 8453, "address": WETH_BASE}}
