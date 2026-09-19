"""The onchain venue serves real completed bars, and the registry knows what
each venue can honor."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from wayfinder_paths.jobs import forward_artifacts
from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution import venues as venues_module
from wayfinder_paths.jobs.execution.driver import tick_job
from wayfinder_paths.jobs.execution.hyperliquid import HyperliquidMarketFeed
from wayfinder_paths.jobs.execution.onchain import (
    ONCHAIN_CAPABILITIES,
    OnchainMarketFeed,
    OnchainVenueAdapter,
)
from wayfinder_paths.jobs.execution.venues import (
    VENUE_CAPABILITIES,
    HistoryProvenanceFeed,
    venue_capabilities,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.tests.test_execution_token_bars import FakeCandleClient

HOUR_MS = 3_600_000
WETH_BASE = "0x4200000000000000000000000000000000000006"
TOKEN = "ethereum-base"

STRATEGY = """
from wayfinder_paths.jobs.execution import OrderIntent


class Strategy:
    def __init__(self, params):
        self.params = params

    def decide(self, ctx):
        latest = ctx.view.latest("ethereum-base")
        if "ethereum-base" not in ctx.ledger.positions and float(latest["close"]) > 100.0:
            return [
                OrderIntent(
                    action="OPEN",
                    venue="onchain",
                    symbol="ethereum-base",
                    side="long",
                    size=1,
                )
            ]
        return []


def build_strategy(params):
    return Strategy(params)
"""


class _Pinned:
    calls = 0

    @classmethod
    async def resolve_token(cls, query, *, chain_id=None):
        cls.calls += 1
        return 8453, WETH_BASE


class _Refusing:
    @classmethod
    async def resolve_token(cls, query, *, chain_id=None):
        raise AssertionError("pinned symbols must not resolve")


def _hourly(count: int, *, end_open_ms: int) -> list[dict[str, Any]]:
    return [
        {
            "t": end_open_ms - HOUR_MS * (count - 1 - index),
            "o": str(100 + index),
            "h": str(101 + index),
            "l": str(99 + index),
            "c": str(100.5 + index),
            "v": "1",
        }
        for index in range(count)
    ]


def _this_open_ms() -> int:
    now_ms = int(time.time() * 1000)
    return now_ms - (now_ms % HOUR_MS)


def test_registry_carries_every_venue_contract() -> None:
    assert set(VENUE_CAPABILITIES) >= {
        "hyperliquid",
        "hyperliquid_spot",
        "hyperliquid_prediction",
        "polymarket",
        "onchain",
    }
    assert venue_capabilities("onchain") is ONCHAIN_CAPABILITIES
    assert venue_capabilities("onchain").supports_shorts is False
    assert venue_capabilities("hyperliquid").supports_shorts is True
    assert venues_module.DEFAULT_TAKER_FEE_BPS["onchain"] == 30.0
    with pytest.raises(ValueError, match="unknown venue"):
        venue_capabilities("nowhere")


def test_feed_serves_close_labelled_bars_and_skips_foreign_symbols() -> None:
    this_open = _this_open_ms()
    client = FakeCandleClient(_hourly(60, end_open_ms=this_open))
    feed = OnchainMarketFeed(client=client, resolver=_Pinned)
    as_of = pd.Timestamp(this_open + 600_000, unit="ms", tz="UTC")

    view = asyncio.run(
        feed.get_completed_bars(
            [TOKEN, "BTC", "HYPE/USDC"], "1h", lookback_bars=48, as_of=as_of
        )
    )

    frame = view.to_frame()
    assert list(frame["symbol"].unique()) == [TOKEN]
    assert len(frame) == 48
    assert frame["timestamp"].iloc[-1] == pd.Timestamp(this_open, unit="ms", tz="UTC")
    assert frame["timestamp"].dt.tz is not None
    # candle 59 opened this hour and is forming; 58 is the last completed bar
    assert view.latest(TOKEN)["close"] == pytest.approx(100.5 + 58)
    assert isinstance(feed, HistoryProvenanceFeed)
    provenance = feed.history_provenance()[TOKEN]
    assert provenance["chain_id"] == 8453 and provenance["address"] == WETH_BASE
    assert (
        provenance["earliest_available"]
        == pd.Timestamp(client.candles[0]["t"], unit="ms", tz="UTC").isoformat()
    )
    assert (
        provenance["requested_start"]
        == pd.Timestamp(this_open - 48 * HOUR_MS, unit="ms", tz="UTC").isoformat()
    )
    # ten minutes into the forming hour, the window ends at that hour's open
    assert (client.calls[0]["start"], client.calls[0]["end"]) == (
        this_open - 48 * HOUR_MS,
        this_open,
    )


def test_feed_refuses_unsupported_intervals_and_honors_pins() -> None:
    this_open = _this_open_ms()
    client = FakeCandleClient(_hourly(5, end_open_ms=this_open - HOUR_MS))
    with pytest.raises(ValueError, match="1m\\|5m\\|15m\\|1h\\|4h\\|1d"):
        asyncio.run(
            OnchainMarketFeed(client=client, resolver=_Pinned).get_completed_bars(
                [TOKEN], "2h", lookback_bars=5
            )
        )

    pinned = OnchainMarketFeed(
        client=client,
        resolver=_Refusing,
        token_resolution={TOKEN: {"chain_id": 8453, "address": WETH_BASE}},
    )
    view = asyncio.run(pinned.get_completed_bars([TOKEN], "1h", lookback_bars=5))
    assert len(view.to_frame()) == 5 and client.calls[-1]["chain_id"] == 8453


def test_adapter_reads_token_resolution_from_the_spec() -> None:
    spec = ExecutionSpec()
    spec.data_contract["token_resolution"] = {
        TOKEN: {"chain_id": 8453, "address": WETH_BASE}
    }
    adapter = OnchainVenueAdapter(mode="paper", params={"fee_bps": 30.0}, spec=spec)
    assert adapter.feed.token_resolution == {
        TOKEN: {"chain_id": 8453, "address": WETH_BASE}
    }
    assert adapter.capabilities is ONCHAIN_CAPABILITIES
    bare = OnchainVenueAdapter(mode="paper", params={}, spec=None)
    assert bare.feed.token_resolution == {}


def test_perp_feed_skips_token_ids_and_pairs() -> None:
    class _NoCandles:
        async def get_candles(self, *args, **kwargs):
            raise AssertionError(
                "no perp candles should be fetched for foreign symbols"
            )

    feed = HyperliquidMarketFeed(_NoCandles())  # type: ignore[arg-type]
    view = asyncio.run(
        feed.get_completed_bars([TOKEN, "HYPE/USDC"], "1h", lookback_bars=3)
    )
    assert len(view.to_frame()) == 0


def _token_job(tmp_path: Path) -> tuple[JobStore, WayfinderJob, Path]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "token-driver-demo",
        script=".wayfinder/jobs/token-driver-demo/workspace/src/strategy.py",
        interval_seconds=3600,
        execution_contract="jobs_v1",
    )
    job.script_loop.mode = "paper"
    spec = ExecutionSpec()
    spec.market_kind = "spot"
    spec.venues = ["onchain"]
    spec.data_contract["bar_interval"] = "1h"
    spec.data_contract["token_resolution"] = {
        TOKEN: {"chain_id": 8453, "address": WETH_BASE}
    }
    job.execution_spec = spec.to_dict()
    job.execution_params = {"symbols": [TOKEN], "venue": "onchain", "fee_bps": 30.0}
    store.save(job)
    root = store.job_dir(job.id)
    script = root / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(STRATEGY.lstrip(), encoding="utf-8")
    return store, job, root


async def test_paper_tick_over_the_onchain_adapter_fills_at_next_bar_open(
    tmp_path: Path,
) -> None:
    store, job, root = _token_job(tmp_path)
    this_open = _this_open_ms()
    candles = _hourly(6, end_open_ms=this_open - HOUR_MS)  # six closed bars
    client = FakeCandleClient(candles)
    adapter = OnchainVenueAdapter(
        mode="paper", params=job.execution_params, spec=job.execution_spec
    )
    adapter.feed = OnchainMarketFeed(
        client=client,
        resolver=_Refusing,
        token_resolution={TOKEN: {"chain_id": 8453, "address": WETH_BASE}},
    )
    fifth_close = pd.Timestamp(candles[4]["t"] + HOUR_MS, unit="ms", tz="UTC")
    sixth_close = pd.Timestamp(candles[5]["t"] + HOUR_MS, unit="ms", tz="UTC")

    first = await tick_job(
        job, root, "paper", store=store, adapters={"onchain": adapter}, now=fifth_close
    )
    assert first["ok"] is True, first
    assert first["intents"] and first["fills"] == []

    second = await tick_job(
        job, root, "paper", store=store, adapters={"onchain": adapter}, now=sixth_close
    )
    assert second["fills"], "the queued buy fills at the next bar's open"
    assert second["fills"][0]["avg_price"] == pytest.approx(
        float(candles[5]["o"]), rel=0.01
    )
    assert (root / "state" / "engine_state.json").exists()


def test_forward_chart_fetches_bars_through_the_named_venue(
    monkeypatch, tmp_path: Path
) -> None:
    seen: list[str] = []
    this_open = _this_open_ms()
    rows = [
        {
            "timestamp": pd.Timestamp(
                this_open - HOUR_MS * (3 - index), unit="ms", tz="UTC"
            ),
            "symbol": TOKEN,
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 1.0,
        }
        for index in range(3)
    ]

    class _Feed:
        async def get_completed_bars(
            self, symbols, interval, *, lookback_bars, as_of=None
        ):
            from wayfinder_paths.jobs.execution.primitives import CompletedBarsView

            return CompletedBarsView.from_rows(rows)

    def fake_build_adapter(venue, *, mode, spec=None, params=None):
        seen.append(venue)
        return SimpleNamespace(feed=_Feed())

    monkeypatch.setattr(venues_module, "build_adapter", fake_build_adapter)
    store = JobStore(repo_root=tmp_path)
    job = SimpleNamespace(id="chart-demo", execution_params={})
    ticks = [{"ts": rows[0]["timestamp"].isoformat()}]

    points = forward_artifacts._fetch_hyperliquid_bars(
        job, [TOKEN], ticks, store=store, venue="onchain"
    )

    assert seen == ["onchain"]
    assert [point["close"] for point in points[TOKEN]] == [1.5, 1.5, 1.5]
