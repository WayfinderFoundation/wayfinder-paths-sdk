"""Token-price and yield feature feeds: name grammar, candle paging and
labelling, yield series hygiene, retries, and id resolution — all against
in-file fakes, never the network."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pandas as pd
import pytest

from wayfinder_paths.core.clients.delta_lab_types import DeltaLabAPIError
from wayfinder_paths.jobs.execution import feature_feeds as ff

HOUR_MS = 3_600_000


class FakeTokenClient:
    """Serves hourly candles newest-page-first the way the venue does: rows
    carry OPEN times in ms and string prices, the cursor is in seconds."""

    def __init__(
        self, candles: list[dict[str, Any]], *, page_size: int = 10, quirk: bool = False
    ):
        self.candles = sorted(candles, key=lambda row: int(row["t"]))
        self.page_size = page_size
        self.quirk = quirk
        self.calls: list[int | None] = []

    async def get_candles(self, coin, interval, *, chain_id, before_timestamp=None):
        self.calls.append(before_timestamp)
        assert coin.startswith("0x") and chain_id == 8453 and interval == "1h"
        if self.quirk and len(self.calls) == 1 and before_timestamp is not None:
            return []
        rows = [
            row
            for row in self.candles
            if before_timestamp is None or int(row["t"]) // 1000 <= before_timestamp
        ]
        return rows[-self.page_size :]


def _hourly_candles(count: int, *, now_ms: int) -> list[dict[str, Any]]:
    """`count` candles ending with the in-progress hour (open = this hour)."""
    this_open = now_ms - (now_ms % HOUR_MS)
    return [
        {
            "t": this_open - HOUR_MS * (count - 1 - index),
            "o": str(100 + index),
            "h": str(101 + index),
            "l": str(99 + index),
            "c": str(100.5 + index),
            "v": "1",
        }
        for index in range(count)
    ]


def _token_feed() -> dict[str, Any]:
    return {
        "kind": "token_price",
        "token_id": "ethereum-base",
        "chain_id": 8453,
        "address": "0x4200000000000000000000000000000000000006",
        "interval": "1h",
    }


def test_parse_feed_name_round_trips_every_kind_and_rejects_unknown() -> None:
    names = [
        "token_price:ethereum-base",
        "lend_supply_apr:aave-base:USDC",
        "lend_borrow_apr:morpho_ethereum:USDC:0xabc",
        "yield_apy:sUSDe",
        "pendle_implied_apy:pendle:230",
        "boros_fixed_rate:boros:927",
    ]
    for name in names:
        assert ff.feed_feature_name(ff.parse_feed_name(name)) == name
    assert ff.parse_feed_name("pendle_implied_apy:pendle:230")["market_id"] == 230
    for bad in [
        "funding:BTC",
        "token_price",
        "lend_supply_apr:aave-base",
        "boros_fixed_rate:boros:x",
    ]:
        with pytest.raises(ValueError):
            ff.parse_feed_name(bad)


def test_token_feed_interval_coarsens_to_the_supported_set() -> None:
    assert ff.token_feed_interval("5m") == "5m"
    assert ff.token_feed_interval("2h") == "1h"
    assert ff.token_feed_interval("3m") == "1m"
    assert ff.token_feed_interval("1d") == "1d"
    assert ff.token_feed_interval("30s") == "1m"
    with pytest.raises(ValueError):
        ff.token_feed_interval("soon")


def test_token_rows_are_close_labelled_floats_paged_backward() -> None:
    now_ms = int(time.time() * 1000)
    client = FakeTokenClient(_hourly_candles(30, now_ms=now_ms), page_size=10)
    rows, meta = asyncio.run(
        ff.fetch_token_price_rows([_token_feed()], days=1.0, client=client)
    )
    name = "token_price:ethereum-base"
    # a day of hourly candles minus the in-progress hour; older pages stop at start
    assert meta["per_series"][name] == 24 and meta["errors"] == {}
    assert meta["cadence"][name] == "1h" and meta["label_convention"]
    stamps = [pd.Timestamp(row["timestamp"]) for row in rows]
    assert stamps == sorted(stamps)
    newest_open = client.candles[-1]["t"]
    assert stamps[-1] == pd.Timestamp(
        newest_open, unit="ms", tz="UTC"
    )  # previous hour's close
    assert all(
        isinstance(row["value"], float) and row["symbol"] is None for row in rows
    )
    assert rows[-1]["value"] == 100.5 + 28
    assert (
        len(client.calls) >= 3 and client.calls[1] == client.calls[1] // 1
    )  # cursor is seconds
    assert client.calls[1] < client.calls[0]


def test_token_first_page_quirk_retries_unbounded_once() -> None:
    now_ms = int(time.time() * 1000)
    client = FakeTokenClient(_hourly_candles(5, now_ms=now_ms), quirk=True)
    rows, meta = asyncio.run(
        ff.fetch_token_price_rows([_token_feed()], days=1.0, client=client)
    )
    assert client.calls[0] is not None and client.calls[1] is None
    assert meta["per_series"]["token_price:ethereum-base"] == 4 and rows


def test_token_since_narrows_an_incremental_fetch() -> None:
    now_ms = int(time.time() * 1000)
    client = FakeTokenClient(_hourly_candles(30, now_ms=now_ms), page_size=10)
    since = pd.Timestamp(now_ms, unit="ms", tz="UTC") - pd.Timedelta(hours=3)
    rows, _ = asyncio.run(
        ff.fetch_token_price_rows(
            [_token_feed()], days=30.0, since=since, client=client
        )
    )
    assert 2 <= len(rows) <= 3 and all(
        pd.Timestamp(r["timestamp"]) >= since for r in rows
    )
    assert len(client.calls) == 1


def test_token_errors_are_isolated_per_series() -> None:
    now_ms = int(time.time() * 1000)
    client = FakeTokenClient(_hourly_candles(5, now_ms=now_ms))
    broken = {**_token_feed(), "token_id": "nothing-here", "interval": "9x"}
    rows, meta = asyncio.run(
        ff.fetch_token_price_rows([broken, _token_feed()], days=1.0, client=client)
    )
    assert meta["per_series"]["token_price:nothing-here"] == 0
    assert "token_price:nothing-here" in meta["errors"]
    assert meta["per_series"]["token_price:ethereum-base"] == 4 and len(rows) == 4


class FakeDeltaLab:
    def __init__(self, frame: pd.DataFrame, *, fail_first: int = 0, status: int = 500):
        self.frame = frame
        self.fail_first = fail_first
        self.status = status
        self.calls: list[tuple[Any, ...]] = []
        self.lending_rows: list[dict[str, Any]] = []

    async def _serve(self, label: str, *args: Any) -> pd.DataFrame:
        self.calls.append((label, *args))
        if self.fail_first > 0:
            self.fail_first -= 1
            raise DeltaLabAPIError("internal", "boom", status=self.status, url="x")
        return self.frame

    async def get_market_lending_ts(self, *, market_id, asset_id, lookback_days=30):
        return await self._serve("lending", market_id, asset_id, lookback_days)

    async def get_asset_yield_ts(self, *, asset_id, lookback_days=30):
        return await self._serve("yield", asset_id, lookback_days)

    async def get_market_pendle_ts(self, *, market_id, lookback_days=30):
        return await self._serve("pendle", market_id, lookback_days)

    async def get_market_boros_ts(self, *, market_id, lookback_days=30):
        return await self._serve("boros", market_id, lookback_days)

    async def get_asset_basis(self, *, symbol):
        if symbol == "NOPE":
            raise DeltaLabAPIError("not_found", "no asset", status=404, url="x")
        return {"asset_id": 1271, "symbol": symbol}

    async def screen_lending(self, *, asset_ids=None, venue=None, limit=100, **kwargs):
        rows = [
            row
            for row in self.lending_rows
            if (asset_ids is None or row["asset_id"] in asset_ids)
            and (venue is None or str(row["venue_name"]).startswith(venue))
        ]
        return {"data": rows, "count": len(rows)}


def _yield_frame(*, naive: bool = False) -> pd.DataFrame:
    stamps = pd.to_datetime(
        [
            "2026-01-01T00:00:00Z",
            "2026-01-01T01:00:00Z",
            "2026-01-01T01:00:00Z",  # duplicate observation, last wins
            "2026-01-01T02:00:00Z",
            "2026-01-01T03:00:00Z",
        ],
        utc=True,
    )
    if naive:
        stamps = stamps.tz_localize(None)
    frame = pd.DataFrame(
        {"supply_apr": [0.04, 0.05, 0.051, None, 0.06], "borrow_apr": [0.07] * 5},
        index=pd.DatetimeIndex(stamps, name="ts"),
    )
    return frame


def test_yield_rows_dedupe_last_wins_and_keep_decimals() -> None:
    client = FakeDeltaLab(_yield_frame(naive=True))
    feed = {
        "kind": "lend_supply_apr",
        "venue": "aave-base",
        "symbol": "USDC",
        "market_id": 911,
        "asset_id": 1271,
    }
    rows, meta = asyncio.run(ff.fetch_yield_rows([feed], days=30, client=client))
    name = "lend_supply_apr:aave-base:USDC"
    assert [row["value"] for row in rows] == [0.04, 0.051, 0.06]
    assert (
        rows[0]["timestamp"] == "2026-01-01T00:00:00+00:00"
        and rows[0]["symbol"] is None
    )
    assert meta["per_series"][name] == 3 and meta["cadence"][name] == "1h"
    assert meta["lookback_days"] == 30 and client.calls == [("lending", 911, 1271, 30)]


def test_yield_lookback_is_capped_and_since_narrows() -> None:
    client = FakeDeltaLab(_yield_frame())
    feed = {"kind": "yield_apy", "symbol": "sUSDe", "asset_id": 9}
    frame = _yield_frame().rename(columns={"supply_apr": "apy_base"})
    client.frame = frame
    rows, meta = asyncio.run(ff.fetch_yield_rows([feed], days=900, client=client))
    assert meta["lookback_days"] == ff.YIELD_RETENTION_DAYS
    since = pd.Timestamp("2026-01-01T02:00:00Z")
    rows, _ = asyncio.run(
        ff.fetch_yield_rows([feed], days=900, since=since, client=client)
    )
    assert [row["timestamp"] for row in rows] == ["2026-01-01T03:00:00+00:00"]


def test_yield_retries_on_5xx_then_succeeds_and_fails_fast_on_4xx(monkeypatch) -> None:
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(ff.asyncio, "sleep", _sleep)
    client = FakeDeltaLab(_yield_frame(), fail_first=2)
    feed = {
        "kind": "lend_borrow_apr",
        "venue": "aave-base",
        "symbol": "USDC",
        "market_id": 911,
        "asset_id": 1271,
    }
    rows, meta = asyncio.run(ff.fetch_yield_rows([feed], days=7, client=client))
    assert meta["errors"] == {} and len(rows) == 4 and sleeps == [2.0, 4.0]
    client = FakeDeltaLab(_yield_frame(), fail_first=1, status=404)
    rows, meta = asyncio.run(ff.fetch_yield_rows([feed], days=7, client=client))
    assert rows == [] and "boom" in meta["errors"]["lend_borrow_apr:aave-base:USDC"]
    assert len(client.calls) == 1


def test_observed_cadence_detects_daily_series() -> None:
    hourly = pd.DatetimeIndex(
        pd.date_range("2026-01-01", periods=5, freq="h", tz="UTC")
    )
    daily = pd.DatetimeIndex(pd.date_range("2026-01-01", periods=5, freq="D", tz="UTC"))
    assert ff.observed_cadence(hourly) == "1h" and ff.observed_cadence(daily) == "1d"


def test_resolve_yield_feed_pins_ids_and_names_alternatives_on_miss() -> None:
    client = FakeDeltaLab(_yield_frame())
    client.lending_rows = [
        {
            "market_id": 911,
            "asset_id": 1271,
            "venue_name": "aave-base",
            "chain_id": 8453,
            "market_external_id": "0xaave",
            "market_label": "Aave Base",
        },
        {
            "market_id": 20578,
            "asset_id": 1271,
            "venue_name": "morpho_ethereum",
            "chain_id": 1,
            "market_external_id": "0xm1",
            "market_label": "A/USDC",
        },
        {
            "market_id": 20579,
            "asset_id": 1271,
            "venue_name": "morpho_ethereum",
            "chain_id": 1,
            "market_external_id": "0xm2",
            "market_label": "B/USDC",
        },
    ]
    pinned = asyncio.run(
        ff.resolve_yield_feed(
            ff.parse_feed_name("lend_supply_apr:aave-base:USDC"), client=client
        )
    )
    assert (
        pinned["market_id"] == 911
        and pinned["asset_id"] == 1271
        and pinned["chain_id"] == 8453
    )
    with pytest.raises(ValueError, match="add :<market>"):
        asyncio.run(
            ff.resolve_yield_feed(
                ff.parse_feed_name("lend_supply_apr:morpho_ethereum:USDC"),
                client=client,
            )
        )
    chosen = asyncio.run(
        ff.resolve_yield_feed(
            ff.parse_feed_name("lend_supply_apr:morpho_ethereum:USDC:0xm2"),
            client=client,
        )
    )
    assert chosen["market_id"] == 20579
    with pytest.raises(
        ValueError, match="venues carrying it: \\['aave-base', 'morpho_ethereum'\\]"
    ):
        asyncio.run(
            ff.resolve_yield_feed(
                ff.parse_feed_name("lend_supply_apr:euler:USDC"), client=client
            )
        )
    with pytest.raises(ValueError, match="unknown asset symbol"):
        asyncio.run(
            ff.resolve_yield_feed(ff.parse_feed_name("yield_apy:NOPE"), client=client)
        )
    assert (
        asyncio.run(
            ff.resolve_yield_feed(ff.parse_feed_name("yield_apy:sUSDe"), client=client)
        )["asset_id"]
        == 1271
    )
    assert (
        asyncio.run(
            ff.resolve_yield_feed(
                ff.parse_feed_name("boros_fixed_rate:boros:927"), client=client
            )
        )["market_id"]
        == 927
    )


def test_resolve_feeds_are_noops_when_pinned() -> None:
    class _NoResolver:
        @classmethod
        async def resolve_token(cls, query, *, chain_id=None):
            raise AssertionError("must not resolve a pinned feed")

    pinned = asyncio.run(ff.resolve_token_feed(_token_feed(), resolver=_NoResolver))  # type: ignore[arg-type]
    assert pinned == _token_feed()
    client = FakeDeltaLab(_yield_frame())
    feed = {
        "kind": "lend_supply_apr",
        "venue": "aave-base",
        "symbol": "USDC",
        "market_id": 911,
        "asset_id": 1271,
    }
    assert (
        asyncio.run(ff.resolve_yield_feed(feed, client=client)) == feed
        and client.calls == []
    )


def test_resolve_token_feed_uses_the_resolver_for_a_token_id() -> None:
    class _Resolver:
        @classmethod
        async def resolve_token(cls, query, *, chain_id=None):
            assert query == "ethereum-base"
            return 8453, "0x4200000000000000000000000000000000000006"

    feed = {"kind": "token_price", "token_id": "ethereum-base", "interval": "1h"}
    pinned = asyncio.run(ff.resolve_token_feed(feed, resolver=_Resolver))  # type: ignore[arg-type]
    assert pinned["chain_id"] == 8453 and pinned["address"].startswith("0x4200")
