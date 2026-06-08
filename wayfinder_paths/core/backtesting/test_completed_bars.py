from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from wayfinder_paths.core.backtesting.backtester import run_backtest
from wayfinder_paths.core.backtesting.data import (
    _lookback_days_for_window,
    drop_incomplete_bars,
)
from wayfinder_paths.core.backtesting.perps import backtest_perps_trigger
from wayfinder_paths.core.backtesting.ref import (
    BacktestRef,
    CodeEntry,
    CodeRefs,
    DataRefs,
    DataWindow,
    ExecutionAssumptions,
    ProducedBy,
    VenueRefs,
)
from wayfinder_paths.core.backtesting.types import BacktestConfig
from wayfinder_paths.core.perps.context import SignalFrame
from wayfinder_paths.core.perps.handlers.protocol import OrderResult
from wayfinder_paths.core.perps.state import StateStore
from wayfinder_paths.core.strategies.active_perps import ActivePerpsStrategy


def _hourly_index_with_current_open() -> pd.DatetimeIndex:
    current_open = pd.Timestamp.now(tz="UTC").floor("h")
    return pd.DatetimeIndex(
        [
            current_open - pd.Timedelta(hours=2),
            current_open - pd.Timedelta(hours=1),
            current_open,
        ]
    )


def test_drop_incomplete_bars_open_labeled_hourly():
    idx = pd.date_range("2026-06-08 17:00:00+00:00", periods=3, freq="1h")
    df = pd.DataFrame({"BTC": [100.0, 101.0, 102.0]}, index=idx)

    out = drop_incomplete_bars(
        df,
        "1h",
        as_of="2026-06-08T19:28:00+00:00",
        timestamp_label="open",
    )

    assert out.index.tolist() == idx[:2].tolist()


def test_drop_incomplete_bars_close_labeled_hourly():
    idx = pd.date_range("2026-06-08 18:00:00+00:00", periods=3, freq="1h")
    df = pd.DataFrame({"BTC": [100.0, 101.0, 102.0]}, index=idx)

    out = drop_incomplete_bars(
        df,
        "1h",
        as_of="2026-06-08T19:28:00+00:00",
        timestamp_label="close",
    )

    assert out.index.tolist() == idx[:2].tolist()


def test_lookback_days_for_subday_window_is_positive():
    start = datetime(2026, 6, 8, 13, 0, tzinfo=UTC)
    end = datetime(2026, 6, 8, 19, 0, tzinfo=UTC)

    assert _lookback_days_for_window(start, end) == 1


def test_run_backtest_drops_incomplete_final_bar():
    idx = _hourly_index_with_current_open()
    prices = pd.DataFrame({"BTC": [100.0, 101.0, 102.0]}, index=idx)
    target = pd.DataFrame({"BTC": [0.0, 1.0, 1.0]}, index=idx)

    result = run_backtest(
        prices,
        target,
        BacktestConfig(
            fee_rate=0.0,
            slippage_rate=0.0,
            periods_per_year=8760,
        ),
    )

    assert result.equity_curve.index.tolist() == idx[:2].tolist()


def test_next_bar_open_does_not_capture_entry_bar_move():
    idx = pd.date_range("2026-06-01 17:00:00+00:00", periods=3, freq="1h")
    prices = pd.DataFrame({"BTC": [100.0, 200.0, 200.0]}, index=idx)
    target = pd.DataFrame({"BTC": [1.0, 0.0, 0.0]}, index=idx)

    result = run_backtest(
        prices,
        target,
        BacktestConfig(
            fee_rate=0.0,
            slippage_rate=0.0,
            periods_per_year=8760,
            fill_model="next_bar_open",
        ),
    )

    assert result.stats["final_equity"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_backtest_perps_trigger_filters_before_signal_fn():
    idx = _hourly_index_with_current_open()
    prices = pd.DataFrame({"BTC": [100.0, 101.0, 102.0]}, index=idx)
    seen: dict[str, pd.Timestamp] = {}

    def signal_fn(prices_arg, funding_arg, params):
        seen["last_signal_bar"] = prices_arg.index[-1]
        return SignalFrame(
            targets=pd.DataFrame(
                0.0, index=prices_arg.index, columns=prices_arg.columns
            )
        )

    async def decide_fn(ctx):
        return None

    result = await backtest_perps_trigger(
        signal_fn=signal_fn,
        decide_fn=decide_fn,
        symbols=["BTC"],
        start="2026-06-08",
        end="2026-06-09",
        prices=prices,
        funding=None,
        include_funding=False,
    )

    assert seen["last_signal_bar"] == idx[1]
    assert result.equity_curve.index.tolist() == idx[:2].tolist()


def _test_ref(symbols: list[str]) -> BacktestRef:
    return BacktestRef(
        schema_version="0.1",
        produced=ProducedBy(at="", skill="", git_sha="test"),
        code=CodeRefs(
            signal=CodeEntry(module="", entrypoint="", source_sha256="test")
        ),
        venues=VenueRefs(perp=True),
        data=DataRefs(
            symbols=symbols,
            interval="1h",
            window=DataWindow(start="", end=""),
            fingerprint="test",
        ),
        params={"min_order_usd": 10.0},
        execution_assumptions=ExecutionAssumptions(),
        performance={},
    )


class _FakeHandler:
    venue = "perp"

    def __init__(self, prices: pd.DataFrame, now: datetime):
        self.prices = prices
        self._now = now

    async def place_order(
        self,
        symbol,
        side,
        size,
        order_type,
        limit_price=None,
        reduce_only=False,
    ):
        return OrderResult(
            ok=True,
            venue=self.venue,
            symbol=symbol,
            side=side,
            size=size,
            order_type=order_type,
            limit_price=limit_price,
            reduce_only=reduce_only,
            fill_size=size,
            timestamp=self._now,
        )

    async def get_positions(self):
        return {}

    def mid(self, symbol):
        return float(self.prices.loc[self.prices.index[-2], symbol])

    async def get_margin_balance(self):
        return 1_000.0

    def now(self):
        return self._now


def test_active_perps_live_trigger_uses_completed_signal_bar():
    strategy_name = "__completed_bar_guard_test__"
    shutil.rmtree(Path(".wayfinder/state") / strategy_name, ignore_errors=True)
    idx = pd.date_range("2026-06-08 17:00:00+00:00", periods=3, freq="1h")
    prices = pd.DataFrame(
        {"APEX": [10.0, 11.0, 12.0], "GMX": [20.0, 21.0, 22.0]}, index=idx
    )
    fake_handler = _FakeHandler(
        prices,
        datetime(2026, 6, 8, 19, 28, tzinfo=UTC),
    )

    strategy = object.__new__(ActivePerpsStrategy)
    strategy.name = strategy_name
    strategy._ref = _test_ref(["APEX", "GMX"])
    strategy._state = StateStore(strategy_name, "live")
    strategy._risk_limits = None

    def signal_fn(prices_arg, funding_arg, params):
        assert prices_arg.index[-1] == idx[1]
        targets = pd.DataFrame(0.0, index=prices_arg.index, columns=prices_arg.columns)
        targets.loc[idx[1], "APEX"] = 2.0
        return SignalFrame(targets=targets)

    strategy._signal_fn = signal_fn

    from wayfinder_paths.core.backtesting.perps import default_decide

    strategy._decide_fn = default_decide

    async def build_handlers():
        return fake_handler, {}

    async def ensure_venue_leverage(perp, hip3):
        return None

    async def fetch_recent_data(perp):
        return prices, pd.DataFrame()

    strategy._build_handlers = build_handlers
    strategy._ensure_venue_leverage = ensure_venue_leverage
    strategy._fetch_recent_data = fetch_recent_data

    try:
        ok, msg = asyncio.run(strategy._run_trigger())
        snapshot = strategy._state.snapshot()
    finally:
        shutil.rmtree(Path(".wayfinder/state") / strategy_name, ignore_errors=True)

    assert ok, msg
    assert snapshot["latest_raw_bar_ts"] == idx[2].isoformat()
    assert snapshot["signal_bar_ts"] == idx[1].isoformat()
    assert snapshot["dropped_incomplete_bars"] == 1
    assert len(snapshot["orders"]["perp"]) == 1
