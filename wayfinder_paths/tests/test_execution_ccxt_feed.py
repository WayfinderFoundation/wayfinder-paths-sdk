from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.ccxt_feed import (
    CcxtMarketFeed,
    fetch_ccxt_dataset_rows,
)
from wayfinder_paths.jobs.execution.preflight import build_live_dataset
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore

HOUR_MS = 3_600_000
BASE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z


class FakeCcxtExchange:
    def __init__(
        self,
        *,
        markets: dict[str, dict[str, Any]] | None = None,
        candles: dict[str, list[list[float]]] | None = None,
        rate_limit_failures: int = 0,
    ) -> None:
        self.markets = markets or {
            "SNX/USDT:USDT": {"active": True},
            "SNX/USDT": {"active": True},
            "IMX/USDT": {"active": True},
        }
        self.candles = candles or {}
        self.rate_limit_failures = rate_limit_failures
        self.fetch_calls: list[tuple[str, int]] = []
        self.load_markets_calls = 0
        self.closed = False

    async def load_markets(self) -> dict[str, Any]:
        self.load_markets_calls += 1
        return self.markets

    async def fetch_ohlcv(
        self, pair: str, timeframe: str, since: int | None = None, limit: int = 1000
    ) -> list[list[float]]:
        if self.rate_limit_failures > 0:
            self.rate_limit_failures -= 1
            raise RuntimeError("binance 429 too many requests")
        self.fetch_calls.append((pair, int(since or 0)))
        rows = self.candles.get(pair, [])
        return [row for row in rows if row[0] >= (since or 0)][:limit]

    async def close(self) -> None:
        self.closed = True


def _candles(count: int, *, start_ms: int = BASE_MS) -> list[list[float]]:
    rows = []
    for index in range(count):
        open_ms = start_ms + index * HOUR_MS
        price = 10.0 + index * 0.1
        rows.append(
            [open_ms, price, price + 0.5, price - 0.5, price + 0.2, 100 + index]
        )
    return rows


async def test_symbol_resolution_prefers_perp_then_spot() -> None:
    exchange = FakeCcxtExchange()
    feed = CcxtMarketFeed(exchange=exchange)

    assert await feed.resolve_market_symbol("SNX") == "SNX/USDT:USDT"
    assert await feed.resolve_market_symbol("IMX") == "IMX/USDT"  # no perp market
    assert feed.symbol_map == {"SNX": "SNX/USDT:USDT", "IMX": "IMX/USDT"}
    assert exchange.load_markets_calls == 1  # cached


async def test_symbol_resolution_missing_market_raises() -> None:
    feed = CcxtMarketFeed(exchange=FakeCcxtExchange(markets={}))

    with pytest.raises(ValueError, match="DOGE"):
        await feed.resolve_market_symbol("DOGE")


async def test_pagination_stitches_and_dedupes() -> None:
    candles = _candles(2500)
    exchange = FakeCcxtExchange(candles={"SNX/USDT:USDT": candles})
    feed = CcxtMarketFeed(exchange=exchange)
    as_of = pd.Timestamp(BASE_MS + 2500 * HOUR_MS, unit="ms", tz="UTC")

    view = await feed.get_completed_bars(["SNX"], "1h", lookback_bars=2500, as_of=as_of)

    frame = view.to_frame()
    assert len(frame) == 2500
    assert frame["timestamp"].is_unique
    assert len(exchange.fetch_calls) >= 3  # paged in 1000-row batches


async def test_close_time_labeling_and_drops_in_progress_bar() -> None:
    candles = _candles(5)
    exchange = FakeCcxtExchange(candles={"SNX/USDT:USDT": candles})
    feed = CcxtMarketFeed(exchange=exchange)
    # as_of lands mid-way through the 5th bar -> its close is in the future
    as_of = pd.Timestamp(BASE_MS + 4 * HOUR_MS + HOUR_MS // 2, unit="ms", tz="UTC")

    view = await feed.get_completed_bars(["SNX"], "1h", lookback_bars=10, as_of=as_of)

    frame = view.to_frame()
    assert len(frame) == 4  # in-progress bar dropped
    for index, row in frame.iterrows():
        open_ms = candles[int(index)][0]
        assert row["timestamp"] == pd.Timestamp(open_ms + HOUR_MS, unit="ms", tz="UTC")
    assert str(frame["timestamp"].dt.tz) == "UTC"


async def test_symbol_column_is_coin_not_pair() -> None:
    exchange = FakeCcxtExchange(candles={"SNX/USDT:USDT": _candles(3)})
    feed = CcxtMarketFeed(exchange=exchange)
    as_of = pd.Timestamp(BASE_MS + 3 * HOUR_MS, unit="ms", tz="UTC")

    view = await feed.get_completed_bars(["SNX"], "1h", lookback_bars=5, as_of=as_of)

    assert view.symbols == ["SNX"]


async def test_retries_on_rate_limit_then_succeeds() -> None:
    exchange = FakeCcxtExchange(
        candles={"SNX/USDT:USDT": _candles(3)}, rate_limit_failures=2
    )
    feed = CcxtMarketFeed(exchange=exchange)
    as_of = pd.Timestamp(BASE_MS + 3 * HOUR_MS, unit="ms", tz="UTC")

    view = await feed.get_completed_bars(["SNX"], "1h", lookback_bars=5, as_of=as_of)

    assert len(view.to_frame()) == 3


async def test_fetch_dataset_rows_returns_metadata() -> None:
    import time

    # fetch_ccxt_dataset_rows uses wall-clock now; candles must be recent
    recent_start = (int(time.time() * 1000) // HOUR_MS - 50) * HOUR_MS
    exchange = FakeCcxtExchange(
        candles={"SNX/USDT:USDT": _candles(48, start_ms=recent_start)}
    )

    rows, metadata = await fetch_ccxt_dataset_rows(
        ["SNX"], "1h", days=2, exchange=exchange
    )

    assert rows
    assert metadata["exchange"] == "binance"
    assert metadata["market_type"] == "swap"
    assert metadata["symbol_map"] == {"SNX": "SNX/USDT:USDT"}
    assert metadata["label_convention"] == "close_time"


def test_build_live_dataset_ccxt_source_writes_metadata(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "ccxt-demo",
        script=".wayfinder/jobs/ccxt-demo/workspace/src/strategy.py",
        interval_seconds=3600,
        execution_contract="jobs_v1",
    )
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    job.execution_spec = spec.to_dict()
    job.execution_params = {"symbols": ["SNX"]}
    store.save(job)
    root = store.job_dir(job.id)
    (root / "workspace" / "src").mkdir(parents=True, exist_ok=True)
    (root / "workspace" / "src" / "strategy.py").write_text(
        "def decide(ctx):\n    return []\n", encoding="utf-8"
    )
    end_ms = BASE_MS + 60 * HOUR_MS
    feed = CcxtMarketFeed(
        exchange=FakeCcxtExchange(candles={"SNX/USDT:USDT": _candles(60)})
    )

    class FrozenFeed:
        """Wraps the feed pinning as_of so the fake candles are 'recent'."""

        symbol_map = feed.symbol_map

        async def get_completed_bars(
            self, symbols, interval, *, lookback_bars, as_of=None
        ):
            return await feed.get_completed_bars(
                symbols,
                interval,
                lookback_bars=lookback_bars,
                as_of=pd.Timestamp(end_ms, unit="ms", tz="UTC"),
            )

    result = build_live_dataset(
        job.id, days=2, store=store, source="ccxt", feed=FrozenFeed()
    )

    assert result["metadata"]["source"] == "ccxt"
    assert result["metadata"]["label_convention"] == "close_time"
    payload = json.loads(
        (root / "results" / "backtest" / "input_bars.json").read_text(encoding="utf-8")
    )
    assert payload["metadata"]["source"] == "ccxt"
    assert payload["bars"], "bars must be persisted"
    assert payload["bars"][0]["symbol"] == "SNX"


def test_cli_fetch_dataset_source_option(monkeypatch) -> None:
    from click.testing import CliRunner

    from wayfinder_paths.jobs import cli as cli_module

    captured: dict[str, Any] = {}

    def fake_build(job_id, **kwargs):
        captured.update({"job_id": job_id, **kwargs})
        return {"path": "x", "bars": 1, "metadata": {"source": kwargs["source"]}}

    # Patch both the defining module and the CLI's module-top binding.
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.preflight.build_live_dataset", fake_build
    )
    monkeypatch.setattr(cli_module, "build_live_dataset", fake_build)
    runner = CliRunner()

    result = runner.invoke(
        cli_module.job_cli,
        [
            "fetch-dataset",
            "demo",
            "--source",
            "ccxt",
            "--exchange",
            "bybit",
            "--market-type",
            "spot",
            "--days",
            "30",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert captured["source"] == "ccxt"
    assert captured["exchange"] == "bybit"
    assert captured["market_type"] == "spot"
    assert captured["days"] == 30
