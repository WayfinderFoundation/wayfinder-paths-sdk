"""Liquidation-flush starters: the open-interest flush signal, both execution
styles, the side filter, and the ride-then-expire hold."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.jobs.execution.features import apply_precompute
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionContext,
    ExecutionSpec,
    PositionLedger,
    PositionRecord,
    StateSnapshot,
)
from wayfinder_paths.jobs.strategies.mixed_liquidation_flush import (
    MixedLiquidationFlushStrategy,
)

BARS = 400


def _rows(
    *,
    move: float,
    oi_change: float,
    with_oi: bool = True,
    quiet_tail: int = 0,
) -> list[dict[str, Any]]:
    """A flat book whose last day moved by ``move`` while open interest changed
    by ``oi_change``; ``quiet_tail`` bars at the end carry neither."""
    stamps = pd.date_range("2026-01-01T00:00:00Z", periods=BARS, freq="15min")
    rows: list[dict[str, Any]] = []
    for symbol in ("AAA", "BBB"):
        close = np.full(BARS, 100.0)
        close[-96 - quiet_tail : BARS - quiet_tail] = 100.0 * (
            1.0 + move * np.linspace(0.0, 1.0, 96)
        )
        if quiet_tail:
            close[-quiet_tail:] = close[-quiet_tail - 1]
        oi = np.full(BARS, 1.0e6)
        oi[-96 - quiet_tail : BARS - quiet_tail] = 1.0e6 * (
            1.0 + oi_change * np.linspace(0.0, 1.0, 96)
        )
        if quiet_tail:
            oi[-quiet_tail:] = oi[-quiet_tail - 1]
        for i in range(BARS):
            row: dict[str, Any] = {
                "timestamp": stamps[i],
                "symbol": symbol,
                "open": float(close[i]),
                "high": float(close[i]) + 0.3,
                "low": float(close[i]) - 0.3,
                "close": float(close[i]),
                "volume": 1.0,
            }
            if with_oi:
                row["open_interest"] = float(oi[i])
            rows.append(row)
    return rows


def _context(
    strategy: MixedLiquidationFlushStrategy, rows: list[dict[str, Any]]
) -> ExecutionContext:
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "15m"
    view = apply_precompute(strategy, CompletedBarsView.from_rows(rows))
    return ExecutionContext(
        view=view,
        ledger=PositionLedger(),
        state_snapshot=StateSnapshot(status="valid"),
        capacity=None,
        params={"initial_capital": 10_000.0},
        timestamp=pd.Timestamp(rows[-1]["timestamp"]).isoformat(),
        execution_spec=spec,
    )


def _strategy(**overrides: Any) -> MixedLiquidationFlushStrategy:
    return MixedLiquidationFlushStrategy(
        {
            "symbols": ["AAA", "BBB"],
            "flush_return_min": 0.08,
            "flush_oi_drop_min": 0.10,
            "hold_after_signal_bars": 12,
            "min_trade_notional": 0.0,
            **overrides,
        }
    )


def test_long_liquidation_flush_is_bought_at_market() -> None:
    strategy = _strategy(entry_order_type="market")
    intents = strategy.decide(_context(strategy, _rows(move=-0.10, oi_change=-0.15)))
    assert {intent["symbol"] for intent in intents} == {"AAA", "BBB"}
    entry = intents[0]
    assert entry["action"] == "OPEN" and entry["side"] == "buy"
    assert "limit_price" not in entry
    assert entry["notional"] == 500.0  # 5% of a 10,000 book
    assert entry["metadata"]["entry_reason"] == "liquidation_flush"
    assert entry["metadata"]["signal_oi_change"] < -0.10


def test_short_squeeze_flush_is_sold_and_maker_rests_beyond_the_close() -> None:
    strategy = _strategy(entry_order_type="maker", entry_offset_atr=0.5)
    ctx = _context(strategy, _rows(move=0.10, oi_change=-0.15))
    entry = strategy.decide(ctx)[0]
    close = float(ctx.view.symbol_frame(entry["symbol"]).iloc[-1]["close"])
    assert entry["side"] == "sell"
    assert entry["limit_price"] > close
    assert entry["time_in_force"] == "ALO"
    assert entry["expires_after_bars"] == 1


def test_a_move_that_open_interest_survived_is_not_a_flush() -> None:
    strategy = _strategy(entry_order_type="market")
    assert strategy.decide(_context(strategy, _rows(move=-0.10, oi_change=0.05))) == []
    assert strategy.decide(_context(strategy, _rows(move=-0.03, oi_change=-0.15))) == []


def test_without_open_interest_the_book_stands_down() -> None:
    strategy = _strategy(entry_order_type="market")
    ctx = _context(strategy, _rows(move=-0.10, oi_change=-0.15, with_oi=False))
    assert ctx.view.symbol_frame("AAA")["starter_oi_change"].isna().all()
    assert strategy.decide(ctx) == []


def test_side_filter_keeps_only_the_requested_leg() -> None:
    longs_only = _strategy(entry_order_type="market", sides="long")
    assert (
        longs_only.decide(_context(longs_only, _rows(move=0.10, oi_change=-0.15))) == []
    )
    assert (
        len(longs_only.decide(_context(longs_only, _rows(move=-0.10, oi_change=-0.15))))
        == 2
    )


def test_positions_ride_the_flush_and_expire_after_the_hold() -> None:
    strategy = _strategy(entry_order_type="market", hold_after_signal_bars=12)
    # the flush is still active: the long is kept
    live = _context(strategy, _rows(move=-0.10, oi_change=-0.15))
    live.ledger.positions["AAA"] = PositionRecord(
        symbol="AAA", side="long", size=5.0, avg_price=100.0, bars_held=40
    )
    assert [i for i in strategy.decide(live) if i["action"] == "CLOSE"] == []
    # The flush condition outlives the move: the one-day return window keeps
    # spanning it for a while. Grow the quiet tail until the signal has been
    # gone for eight bars (inside the hold), then for twelve (expired).
    ages: dict[int, ExecutionContext] = {}
    for tail in range(1, 140):
        ctx = _context(strategy, _rows(move=-0.10, oi_change=-0.15, quiet_tail=tail))
        age = int(ctx.view.symbol_frame("AAA").iloc[-1]["starter_signal_age"])
        ages.setdefault(age, ctx)
        if 8 in ages and 12 in ages:
            break
    inside, expired = ages[8], ages[12]
    inside.ledger.positions["AAA"] = PositionRecord(
        symbol="AAA", side="long", size=5.0, avg_price=100.0, bars_held=48
    )
    assert [i for i in strategy.decide(inside) if i["action"] == "CLOSE"] == []
    expired.ledger.positions["AAA"] = PositionRecord(
        symbol="AAA", side="long", size=5.0, avg_price=100.0, bars_held=52
    )
    closes = [i for i in strategy.decide(expired) if i["action"] == "CLOSE"]
    assert closes and closes[0]["metadata"]["exit_reason"] == "hold_expired"
    assert closes[0]["side"] == "sell" and closes[0]["reduce_only"] is True


def test_a_flipped_flush_closes_the_position_immediately() -> None:
    strategy = _strategy(entry_order_type="market")
    ctx = _context(strategy, _rows(move=0.10, oi_change=-0.15))
    ctx.ledger.positions["AAA"] = PositionRecord(
        symbol="AAA", side="long", size=5.0, avg_price=100.0, bars_held=3
    )
    closes = [i for i in strategy.decide(ctx) if i["action"] == "CLOSE"]
    assert closes[0]["symbol"] == "AAA"
    assert closes[0]["metadata"]["exit_reason"] == "signal_flipped"


def test_cascade_guards_hold_entries_until_the_bar_calms_or_confirms() -> None:
    rows = _rows(move=-0.10, oi_change=-0.15)
    # widen every symbol's last bar far beyond the flat book's ATR and close
    # it lower (rows are grouped by symbol, so pick the bars by timestamp)
    last_bars = [row for row in rows if row["timestamp"] == rows[-1]["timestamp"]]
    assert len(last_bars) == 2
    for row in last_bars:
        row["high"] = row["close"] + 8.0
        row["low"] = row["close"] - 8.0
        row["open"] = row["close"] + 0.5
    guarded = _strategy(entry_order_type="market", entry_max_bar_range_atr=2.0)
    assert guarded.decide(_context(guarded, rows)) == []
    confirming = _strategy(entry_order_type="market", entry_confirm_bar=True)
    assert confirming.decide(_context(confirming, rows)) == []
    ungated = _strategy(entry_order_type="market")
    assert len(ungated.decide(_context(ungated, rows))) == 2
    # a bar that closes in the fade direction satisfies the confirmation
    for row in last_bars:
        row["open"] = row["close"] - 0.5
    assert len(confirming.decide(_context(confirming, rows))) == 2


def test_precompute_is_the_shared_indicator() -> None:
    from wayfinder_paths.jobs.indicators import (
        bars_since_signal,
        liquidation_flush_signal,
    )

    strategy = _strategy(entry_order_type="market")
    ctx = _context(strategy, _rows(move=-0.10, oi_change=-0.15, quiet_tail=40))
    frame = ctx.view.symbol_frame("AAA").reset_index(drop=True)
    shared = liquidation_flush_signal(
        frame, return_bars=96, return_min=0.08, oi_bars=96, oi_drop_min=0.10
    )
    for starter_column, shared_column in (
        ("starter_flush_return", "flush_return"),
        ("starter_oi_change", "oi_change"),
        ("starter_signal", "signal"),
    ):
        # the view stores derived columns as objects; compare the values
        pd.testing.assert_series_equal(
            pd.to_numeric(frame[starter_column], errors="coerce").reset_index(
                drop=True
            ),
            shared[shared_column].astype(float).reset_index(drop=True),
            check_names=False,
        )
    pd.testing.assert_series_equal(
        pd.to_numeric(frame["starter_signal_age"], errors="coerce").reset_index(
            drop=True
        ),
        bars_since_signal(shared["signal"]).astype(float).reset_index(drop=True),
        check_names=False,
    )


def test_rejects_invalid_modes_and_thresholds() -> None:
    with pytest.raises(ValueError):
        MixedLiquidationFlushStrategy({"entry_order_type": "limit"})
    with pytest.raises(ValueError):
        MixedLiquidationFlushStrategy({"sides": "up"})
    with pytest.raises(ValueError):
        MixedLiquidationFlushStrategy({"flush_oi_drop_min": 0.0})
    with pytest.raises(ValueError):
        MixedLiquidationFlushStrategy({"hold_after_signal_bars": 0})
