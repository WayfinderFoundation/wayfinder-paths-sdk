from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.core.backtesting import backtester, helpers, offload
from wayfinder_paths.core.backtesting.backtester import run_backtest
from wayfinder_paths.core.backtesting.types import BacktestConfig
from wayfinder_paths.jobs import backtest_runner
from wayfinder_paths.jobs.backtest_runner import RunnerConfig
from wayfinder_paths.tests.sprite_lease_fake import FakeSprites

SYMBOLS = ["BTC", "ETH"]


def _market(bars: int = 240) -> tuple[pd.DataFrame, pd.DataFrame]:
    index = pd.date_range("2026-08-01", periods=bars, freq="h", tz="UTC")
    rng = np.random.default_rng(7)
    prices = pd.DataFrame(
        {
            symbol: start * np.exp(np.cumsum(rng.normal(0, 0.004, bars)))
            for symbol, start in zip(SYMBOLS, (60000.0, 3000.0), strict=True)
        },
        index=index,
    )
    funding = pd.DataFrame(
        {symbol: rng.normal(0.00001, 0.00002, bars) for symbol in SYMBOLS},
        index=index,
    )
    return prices, funding


@pytest.fixture
def market(monkeypatch, tmp_path):
    prices, funding = _market()

    async def fetch_prices(*args, **kwargs):
        return prices

    async def fetch_funding_rates(*args, **kwargs):
        return funding

    monkeypatch.setattr(helpers, "fetch_prices", fetch_prices)
    monkeypatch.setattr(helpers, "fetch_funding_rates", fetch_funding_rates)
    monkeypatch.setattr(backtester, "find_repo_root", lambda: tmp_path)
    return prices


def _runner(tmp_path: Path, **overrides) -> RunnerConfig:
    settings = {
        "provider": "sprites",
        "runs_dir": tmp_path / "runs",
        "configured": True,
        "timeout_seconds": 60,
        "backend": "https://backend.example",
        "api_key": "owner-key",
        "offload_operations": True,
    }
    return RunnerConfig(**(settings | overrides))


def _override(monkeypatch, tmp_path: Path, **overrides) -> None:
    monkeypatch.setattr(
        offload, "load_runner_config", lambda **_: _runner(tmp_path, **overrides)
    )


def _no_sprites(monkeypatch) -> None:
    monkeypatch.setattr(
        backtest_runner,
        "SpriteBacktestsClient",
        lambda *args, **kwargs: pytest.fail("booked a Sprite"),
    )


async def _delta_neutral(threshold: float = 0.00001):
    return await helpers.backtest_delta_neutral(
        SYMBOLS, "2026-08-01", "2026-08-11", funding_threshold=threshold, leverage=2.0
    )


def _hold(prices: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(0.5, index=prices.index, columns=prices.columns)


async def test_the_override_sends_run_backtest_to_a_sprite_with_the_same_result(
    market, tmp_path, monkeypatch
):
    local = await _delta_neutral()
    fake = FakeSprites(tmp_path / "fake")
    client = fake.client()
    monkeypatch.setattr(
        backtest_runner, "SpriteBacktestsClient", lambda *args, **kwargs: client
    )
    _override(monkeypatch, tmp_path)
    remote = await _delta_neutral()
    # The data is fetched here; only the simulation ran on the Sprite.
    assert len(fake.bookings()) == 1
    assert len(fake.worker_requests("/workspace")) == 1
    pd.testing.assert_series_equal(pd.Series(remote.stats), pd.Series(local.stats))
    assert remote.trades == local.trades and remote.trades
    for name in ("equity_curve", "returns"):
        pd.testing.assert_series_equal(
            getattr(remote, name), getattr(local, name), check_freq=False
        )
    for name in ("metrics_by_period", "positions_over_time"):
        pd.testing.assert_frame_equal(
            getattr(remote, name), getattr(local, name), check_freq=False
        )
    assert (remote.liquidated, remote.liquidation_timestamp) == (False, None)
    # A sweep reuses the lease; the staged inputs never outlive a run.
    run_backtest(market, _hold(market), BacktestConfig(leverage=1.5))
    assert len(fake.bookings()) == 1
    assert len(fake.worker_requests("/workspace")) == 2
    assert not any((tmp_path / ".wayfinder" / "backtest_inputs").iterdir())
    client.http.close()


async def test_quick_backtest_stays_local_with_the_override_on(
    market, tmp_path, monkeypatch
):
    _override(monkeypatch, tmp_path)
    _no_sprites(monkeypatch)
    result = await helpers.quick_backtest(
        lambda prices, context: _hold(prices), SYMBOLS, "2026-08-01", "2026-08-11"
    )
    assert len(result.equity_curve) == len(market)


@pytest.mark.parametrize(
    "overrides",
    [
        # The default: no remote override.
        {"provider": "local", "configured": False},
        {"provider": "local"},
        {"offload_operations": False},
    ],
)
def test_without_the_override_run_backtest_runs_here(
    market, tmp_path, monkeypatch, overrides
):
    _override(monkeypatch, tmp_path, **overrides)
    _no_sprites(monkeypatch)
    assert len(run_backtest(market, _hold(market)).equity_curve) == len(market)


def test_only_a_top_level_call_offloads(market, tmp_path, monkeypatch):
    _override(monkeypatch, tmp_path)
    _no_sprites(monkeypatch)
    # A worker thread of a local computation stays with it.
    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(run_backtest, market, _hold(market)).result().stats
    # So does every call inside an agent operation, which offloads (or not) as a whole.
    monkeypatch.setenv("WAYFINDER_OP_STATUS_PATH", str(tmp_path / "op.status.json"))
    assert run_backtest(market, _hold(market)).stats


async def test_switched_off_offloading_runs_the_backtest_here(
    market, tmp_path, monkeypatch
):
    local = await _delta_neutral()
    fake = FakeSprites(tmp_path / "fake")
    fake.switched_off = "backtests_disabled"
    client = fake.client(lease_dir=tmp_path / "runs" / "sprite-leases")
    monkeypatch.setattr(
        backtest_runner, "SpriteBacktestsClient", lambda *args, **kwargs: client
    )
    # fallback: none still computes here: switched off is a setting, not a shortage.
    _override(monkeypatch, tmp_path, fallback="none")
    first = await _delta_neutral()
    pd.testing.assert_series_equal(pd.Series(first.stats), pd.Series(local.stats))
    assert len(fake.bookings()) == 1
    # Remembered: the next backtest never asks the backend.
    second = await _delta_neutral()
    pd.testing.assert_series_equal(pd.Series(second.stats), pd.Series(local.stats))
    assert len(fake.bookings()) == 1
    client.http.close()
