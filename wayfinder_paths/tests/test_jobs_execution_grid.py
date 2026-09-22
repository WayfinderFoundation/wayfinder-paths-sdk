from __future__ import annotations

import pandas as pd
import pytest

from wayfinder_paths.jobs.execution_grid import (
    DEFAULT_GRID,
    GridCosts,
    _Market,
    _simulate_row,
    grid_rows,
    passive_entry_grid,
)


def _frame(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    opens, highs, lows, closes = zip(*rows, strict=True)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2026-01-01", periods=len(rows), freq="5min", tz="UTC"
            ),
            "symbol": "HYPE",
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": 100.0,
        }
    )


# Twelve-bar block with ATR ~1: four quiet bars, the signal bar at 100, a
# dip that fills resting bids and stops shallow tight-stopped entries, then
# a rebound past the targets, then three bars back to 100.
_BLOCK = [
    (100.0, 100.5, 99.5, 100.0),
    (100.0, 100.5, 99.5, 100.0),
    (100.0, 100.5, 99.5, 100.0),
    (100.0, 100.5, 99.5, 100.0),
    (100.0, 100.5, 99.5, 100.0),  # signal bar (index 4 of the block)
    (99.6, 99.7, 98.4, 98.8),  # fills offsets 0.5–1.5
    (98.7, 98.9, 96.0, 97.4),  # stops a shallow entry's 2-ATR stop; fills deeper bids
    (97.6, 100.0, 97.5, 99.8),
    (99.9, 101.6, 99.8, 101.2),  # targets
    (101.0, 101.2, 100.2, 100.4),
    (100.3, 100.6, 99.8, 100.0),
    (100.0, 100.5, 99.5, 100.0),
]


def _blocks(count: int) -> tuple[pd.DataFrame, pd.Series]:
    frame = _frame(_BLOCK * count)
    signal = pd.Series([index % 12 == 4 for index in range(len(frame))])
    return frame, signal


def test_grid_rows_span_the_default_grid() -> None:
    rows = grid_rows()
    assert len(rows) == 5 * 3 * 5 * 5 * 5
    assert len(grid_rows({"entry_ttl_bars": (1,)})) == 5 * 5 * 5 * 5
    assert set(DEFAULT_GRID) == {
        "entry_offset_atr",
        "entry_ttl_bars",
        "target_atr",
        "hold_bars",
        "stop_atr",
    }


def test_deep_wide_rows_win_where_shallow_tight_rows_lose() -> None:
    frame, signal = _blocks(120)
    market = _Market(frame, signal, side="long", atr_period=14)
    # The block is built for an ATR in this band: a 0.5-ATR bid with a 2-ATR
    # stop is stopped on the second dip bar, a 1.5-ATR bid is not.
    assert 1.14 < market.atr[16] < 1.6
    result = passive_entry_grid(frame, signal, bar_seconds=300, min_trades=20, top=1875)
    # The first signal falls inside the ATR warmup.
    assert result["evaluated"] == 1875 and result["signals"] == 119
    rows = {
        (
            r["entry_offset_atr"],
            r["stop_atr"],
            r["target_atr"],
            r["hold_bars"],
            r["entry_ttl_bars"],
        ): r
        for r in result["top"]
    }
    deep = rows[(1.5, 2.0, 1.0, 6, 3)]
    assert deep["train"]["trades"] >= 20 and deep["train"]["return"] > 0
    assert deep["train"]["win_rate"] == 1.0 and deep["validation"]["sharpe"] > 0
    assert deep["score"] > 0
    assert (0.5, 2.0, 1.0, 6, 1) not in rows
    shallow, _, fills, wins = _simulate_row(
        market,
        grid_rows(
            {
                "entry_offset_atr": (0.5,),
                "stop_atr": (2.0,),
                "target_atr": (1.0,),
                "hold_bars": (6,),
                "entry_ttl_bars": (1,),
            }
        )[0],
        GridCosts(),
        start=0,
        end=len(frame),
    )
    assert fills > 100 and wins == 0 and shallow.sum() < 0
    assert result["top"][0]["score"] >= deep["score"]
    assert 0 < result["viable"] < result["evaluated"]


def test_fill_needs_trade_through_and_expires_with_the_ttl() -> None:
    # Signal at bar 2 (close 100, ATR ~1): the next bar's low equals the
    # limit exactly (no trade-through), the bar after trades through.
    rows = [(100.0, 100.5, 99.5, 100.0)] * 20
    rows[21:] = []
    frame = _frame(
        rows
        + [
            (100.0, 100.4, 99.0, 100.0),
            (100.0, 100.4, 98.9, 99.6),
            (99.6, 100.8, 99.5, 100.6),
        ]
        + [(100.0, 100.5, 99.5, 100.0)] * 10
    )
    signal = pd.Series([index == 19 for index in range(len(frame))])
    market = _Market(frame, signal, side="long", atr_period=14)
    row = grid_rows(
        {
            "entry_offset_atr": (1.0,),
            "stop_atr": (3.0,),
            "target_atr": (0.5,),
            "hold_bars": (4,),
            "entry_ttl_bars": (1,),
        }
    )[0]
    limit = market.close[19] - market.atr[19]
    assert market.low[20] == pytest.approx(99.0)
    # A one-bar order sees only bar 20, whose low sits at the limit without
    # the margin (or above it): no fill.
    returns, accepted, fills, _ = _simulate_row(
        market, row, GridCosts(), start=0, end=len(frame)
    )
    assert accepted == 1 and (fills == 0 or market.low[20] < limit * (1 - 1e-4))
    # A two-bar order reaches bar 21, which trades through: one fill.
    row2 = grid_rows(
        {
            "entry_offset_atr": (1.0,),
            "stop_atr": (3.0,),
            "target_atr": (0.5,),
            "hold_bars": (4,),
            "entry_ttl_bars": (2,),
        }
    )[0]
    _, accepted2, fills2, _ = _simulate_row(
        market, row2, GridCosts(), start=0, end=len(frame)
    )
    assert accepted2 == 1 and fills2 == 1


def test_stop_has_same_bar_precedence_over_the_target() -> None:
    quiet = [(100.0, 100.5, 99.5, 100.0)] * 20
    # Signal at bar 19; bar 20 fills at 99 (offset 1.0); bar 21 crosses both
    # the 0.5-ATR target above and the 2-ATR stop below: the stop wins.
    frame = _frame(
        quiet
        + [(100.0, 100.4, 98.8, 99.2), (99.2, 100.5, 96.5, 97.0)]
        + [(97.0, 97.5, 96.5, 97.0)] * 10
    )
    signal = pd.Series([index == 19 for index in range(len(frame))])
    market = _Market(frame, signal, side="long", atr_period=14)
    row = grid_rows(
        {
            "entry_offset_atr": (1.0,),
            "stop_atr": (2.0,),
            "target_atr": (0.5,),
            "hold_bars": (6,),
            "entry_ttl_bars": (1,),
        }
    )[0]
    returns, accepted, fills, wins = _simulate_row(
        market, row, GridCosts(), start=0, end=len(frame)
    )
    assert accepted == 1 and fills == 1 and wins == 0
    assert returns.sum() < -0.015


def test_one_position_at_a_time_and_the_short_side_mirrors() -> None:
    frame, signal = _blocks(40)
    # Signals every bar: only one order or position is live at a time, so
    # far fewer fills than signals.
    dense = pd.Series([index >= 16 for index in range(len(frame))])
    market = _Market(frame, dense, side="long", atr_period=14)
    row = grid_rows(
        {
            "entry_offset_atr": (1.0,),
            "stop_atr": (3.0,),
            "target_atr": (1.0,),
            "hold_bars": (6,),
            "entry_ttl_bars": (1,),
        }
    )[0]
    _, accepted, fills, _ = _simulate_row(
        market, row, GridCosts(), start=0, end=len(frame)
    )
    assert fills < accepted < len(frame) - 16
    # Mirrored bars turn the long dip-and-bounce into a short pop-and-drop.
    mirrored = frame.copy()
    for column in ("open", "high", "low", "close"):
        mirrored[column] = 200.0 - frame[column]
    mirrored["high"], mirrored["low"] = mirrored["low"].copy(), mirrored["high"].copy()
    long_result = passive_entry_grid(
        frame,
        signal,
        bar_seconds=300,
        min_trades=5,
        side="long",
        grid={
            "entry_offset_atr": (1.5,),
            "stop_atr": (3.0,),
            "target_atr": (1.0,),
            "hold_bars": (6,),
            "entry_ttl_bars": (3,),
        },
    )
    short_result = passive_entry_grid(
        mirrored,
        signal,
        bar_seconds=300,
        min_trades=5,
        side="short",
        grid={
            "entry_offset_atr": (1.5,),
            "stop_atr": (3.0,),
            "target_atr": (1.0,),
            "hold_bars": (6,),
            "entry_ttl_bars": (3,),
        },
    )
    assert long_result["top"] and short_result["top"]
    assert (
        long_result["top"][0]["train"]["trades"]
        == short_result["top"][0]["train"]["trades"]
    )
    assert short_result["top"][0]["train"]["return"] == pytest.approx(
        long_result["top"][0]["train"]["return"], rel=0.10
    )


def test_filters_drop_underpopulated_rows_and_the_split_is_reported() -> None:
    frame, signal = _blocks(30)
    result = passive_entry_grid(frame, signal, bar_seconds=300, min_trades=500, top=5)
    assert result["viable"] == 0 and result["top"] == []
    assert result["split"]["train_bars"] == int(len(frame) * 0.7)
    assert result["filters"]["min_trades"] == 500
    with pytest.raises(ValueError, match="side must be long or short"):
        passive_entry_grid(frame, signal, bar_seconds=300, side="sideways")
    with pytest.raises(ValueError, match="not enough bars"):
        passive_entry_grid(frame.iloc[:20], signal.iloc[:20], bar_seconds=300)


def test_short_returns_are_position_value_not_compounded_negations() -> None:
    # Fill at 100 (offset 0), then closes 90 and 80, time exit at the open
    # of the third bar (80): a short is +20% before fees, not +22.2%.
    quiet = [(100.0, 100.5, 99.5, 100.0)] * 20
    frame = _frame(
        quiet
        + [
            (100.0, 100.5, 99.0, 100.0),
            (100.0, 100.5, 89.0, 90.0),
            (90.0, 90.5, 79.0, 80.0),
            (80.0, 80.5, 79.5, 80.0),
        ]
        + [(80.0, 80.5, 79.5, 80.0)] * 6
    )
    signal = pd.Series([index == 19 for index in range(len(frame))])
    row = grid_rows(
        {
            "entry_offset_atr": (0.0,),
            "stop_atr": (100.0,),
            "target_atr": (100.0,),
            "hold_bars": (3,),
            "entry_ttl_bars": (1,),
        }
    )[0]
    costs = GridCosts(maker_fee_bps=0.0, taker_fee_bps=0.0, slippage_bps=0.0)
    short_market = _Market(frame, signal, side="short", atr_period=14)
    short_returns, _, fills, _ = _simulate_row(
        short_market, row, costs, start=0, end=len(frame)
    )
    assert fills == 1
    assert float((1.0 + short_returns).prod()) == pytest.approx(1.20, abs=1e-9)
    long_market = _Market(frame, signal, side="long", atr_period=14)
    long_returns, _, _, _ = _simulate_row(
        long_market, row, costs, start=0, end=len(frame)
    )
    assert float((1.0 + long_returns).prod()) == pytest.approx(0.80, abs=1e-9)
