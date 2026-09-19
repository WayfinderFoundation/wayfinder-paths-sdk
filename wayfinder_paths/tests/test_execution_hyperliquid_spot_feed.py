"""The Hyperliquid spot feed serves real candles per pair."""

from __future__ import annotations

import asyncio
from typing import Any

import pandas as pd
import pytest

from wayfinder_paths.jobs.execution.hyperliquid_spot import (
    HyperliquidSpotFeed,
    spot_candle_coin,
)
from wayfinder_paths.tests.test_execution_hyperliquid_prediction import (
    BASE_MS,
    _candles,
)

HOUR_MS = 3_600_000


class FakeSpotIndex:
    def __init__(self, pairs: dict[str, int] | None = None, *, error: bool = False):
        self.pairs = pairs if pairs is not None else {"HYPE/USDC": 107, "PURR/USDC": 0}
        self.error = error
        self.calls = 0

    async def index_for(self, pair):
        self.calls += 1
        if self.error:
            raise RuntimeError("spotMeta unreachable")
        return self.pairs.get(pair)


class RecordingCandleClient:
    def __init__(
        self, rows: list[dict[str, Any]] | None = None, *, error: bool = False
    ):
        self.rows = rows if rows is not None else []
        self.error = error
        self.coins: list[str] = []

    async def get_candles(
        self, coin, start_ms=None, end_ms=None, interval="1h", *, lookback_hours=None
    ):
        self.coins.append(coin)
        if self.error:
            raise RuntimeError("gateway rejected the spot coin")
        return list(self.rows)


async def _no_mids(symbols):
    raise AssertionError("mids must not be consulted when candles exist")


def test_spot_candle_coin_rule() -> None:
    assert spot_candle_coin(0) == "PURR/USDC"
    assert spot_candle_coin(107) == "@107"


def test_pairs_resolve_to_their_candle_coin_and_rows_carry_the_pair() -> None:
    client = RecordingCandleClient(_candles(5))
    feed = HyperliquidSpotFeed(_no_mids, client=client, spot_index=FakeSpotIndex())

    view = asyncio.run(
        feed.get_completed_bars(
            ["HYPE/USDC", "BTC", "ethereum-base"], "1h", lookback_bars=5
        )
    )

    assert client.coins == ["@107"]
    frame = view.to_frame()
    assert list(frame["symbol"].unique()) == ["HYPE/USDC"]
    assert len(frame) == 5
    assert frame["timestamp"].iloc[0] == pd.Timestamp(
        BASE_MS + HOUR_MS - 1, unit="ms", tz="UTC"
    )
    assert view.latest("HYPE/USDC")["close"] == pytest.approx(0.15 + 4 * 0.001 + 0.001)


def test_purr_uses_its_pair_name_and_the_index_is_read_once() -> None:
    client = RecordingCandleClient(_candles(3))
    index = FakeSpotIndex()
    feed = HyperliquidSpotFeed(_no_mids, client=client, spot_index=index)

    asyncio.run(feed.get_completed_bars(["PURR/USDC"], "1h", lookback_bars=3))
    asyncio.run(feed.get_completed_bars(["PURR/USDC"], "1h", lookback_bars=3))

    assert client.coins == ["PURR/USDC", "PURR/USDC"] and index.calls == 1


@pytest.mark.parametrize(
    "client", [RecordingCandleClient(error=True), RecordingCandleClient([])]
)
def test_direct_snapshot_answers_when_the_gateway_fails_or_is_empty(client) -> None:
    fallback = RecordingCandleClient(_candles(4))
    feed = HyperliquidSpotFeed(
        _no_mids, client=client, fallback=fallback, spot_index=FakeSpotIndex()
    )

    view = asyncio.run(feed.get_completed_bars(["HYPE/USDC"], "1h", lookback_bars=4))

    assert fallback.coins == ["@107"] and len(view.to_frame()) == 4
    assert view.latest("HYPE/USDC")["close"] == pytest.approx(0.15 + 3 * 0.001 + 0.001)


def test_mids_stand_in_when_no_candles_exist() -> None:
    async def mids(symbols):
        return {"NEW/USDC": 2.5}

    feed = HyperliquidSpotFeed(
        mids,
        client=RecordingCandleClient([]),
        fallback=RecordingCandleClient([]),
        spot_index=FakeSpotIndex({"NEW/USDC": 900}),
    )
    view = asyncio.run(feed.get_completed_bars(["NEW/USDC"], "5m", lookback_bars=2))
    assert view.latest("NEW/USDC")["close"] == pytest.approx(2.5)

    unreachable = HyperliquidSpotFeed(
        mids,
        client=RecordingCandleClient(_candles(2)),
        spot_index=FakeSpotIndex(error=True),
    )
    view = asyncio.run(
        unreachable.get_completed_bars(["NEW/USDC"], "5m", lookback_bars=2)
    )
    assert view.latest("NEW/USDC")["close"] == pytest.approx(2.5)
