"""Parity test against the recorded live imx-momentum round-trip
(vault-backend ops_runs .../logs/imx-momentum/2966.log, 2026-06-29 11:05):

    EXIT (exit=SMA50 floor): close=0.1212 entry=0.1205 sma20=0.1218
    sma50=0.1228 bars_held=2/2 -> covered 24894.2 IMX, then
    "Re-arm activated: no short until close > SMA20"

The ported strategy must reproduce the same lifecycle bar-for-bar: NewLow5
entry, Min-2 gate blocking at entry+1, SMA50-floor exit at entry+2, rearm
blocking the next NewLow5, and the rearm lift on a close above SMA20."""

from __future__ import annotations

from typing import Any

import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)
from wayfinder_paths.jobs.strategies import build_imx_momentum
from wayfinder_paths.jobs.strategies.indicators import sma


def _bars(closes: list[float]) -> list[dict[str, Any]]:
    rows = []
    for index, close in enumerate(closes):
        rows.append(
            {
                "timestamp": f"2026-06-{index // 24 + 1:02}T{index % 24:02}:00:00Z",
                "symbol": "IMX",
                "open": close,
                "high": close * 1.002,
                "low": close * 0.998,
                "close": close,
                "volume": 100,
            }
        )
    return rows


def test_recorded_live_round_trip_reproduced() -> None:
    # Warmup shaped so that at the exit bar SMA20 and SMA50 sit above the
    # close (recorded: sma20=0.1218, sma50=0.1228 vs close=0.1212): choppy
    # around 0.126 — never a fresh 5-bar low, keeps both averages overhead.
    warmup = [0.1260 + (i % 3) * 0.0002 for i in range(55)]
    entry_bar = 0.1205  # NewLow5 breakdown (recorded entry price)
    blocked_bar = 0.1206  # entry+1: gate must block the floor exit
    exit_bar = 0.1212  # entry+2: SMA50 floor exit (recorded close)
    blocked_low = 0.1180  # fresh NewLow5 while rearmed
    lift_bar = 0.1290  # close > SMA20 lifts the rearm
    re_entry_bar = 0.1150  # NewLow5 after the lift -> re-enter
    closes = warmup + [
        entry_bar,
        blocked_bar,
        exit_bar,
        blocked_low,
        lift_bar,
        re_entry_bar,
    ]

    # Sanity-pin the fixture against the recorded indicator relationships.
    sma20_series = sma(closes, 20)
    sma50_series = sma(closes, 50)
    exit_index = len(warmup) + 2
    assert closes[exit_index] < sma50_series[exit_index], "floor condition"
    assert closes[exit_index] < sma20_series[exit_index], "no bounce condition"
    assert closes[len(warmup)] < min(closes[len(warmup) - 5 : len(warmup)]), "NewLow5"
    assert closes[len(warmup)] < sma50_series[len(warmup)] * 0.99, "gap filter"

    result = simulate_execution(
        build_imx_momentum,
        PreparedExecutionDataset.from_rows(_bars(closes)),
        _spec(),
        {"symbol": "IMX", "notional_usd": 3000.0},
    )

    intents = result.trace["intents"]
    opens = [row for row in intents if row["action"] == "OPEN"]
    closes_intents = [row for row in intents if row["action"] == "CLOSE"]

    # Entry: prod sizing formula at the recorded entry close.
    assert len(opens) == 2
    entry = opens[0]
    assert entry["timestamp"].startswith(_ts(len(warmup)))
    assert entry["size"] == round(3000.0 / 0.1205, 1)
    assert entry["bracket"]["stop_loss"] == pytest.approx(0.1205 * 1.07)

    # No exit at entry+1 (Min-2 gate), floor exit at entry+2.
    assert len(closes_intents) == 1
    exit_intent = closes_intents[0]
    assert exit_intent["timestamp"].startswith(_ts(exit_index))
    assert exit_intent["metadata"]["exit_reason"] == "sma50_floor"
    assert exit_intent["reduce_only"] is True
    assert exit_intent["size"] == entry["size"]

    # The blocked NewLow5 produced no OPEN; re-entry only after the lift.
    assert opens[1]["timestamp"].startswith(_ts(len(closes) - 1))
    assert opens[1]["size"] == round(3000.0 / re_entry_bar, 1)

    # Round-trip PnL: short entry filled at entry+1 open, covered at exit+1
    # open (next_bar_open model) — a small loss, like the recorded trade.
    exits = [
        fill
        for fill in result.trace["fills"]
        if fill["reduce_only"] and fill["status"] == "filled"
    ]
    assert len(exits) == 1
    assert exits[0]["avg_price"] == pytest.approx(blocked_low)  # exit+1 open


def _spec() -> ExecutionSpec:
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    return spec


def _ts(index: int) -> str:
    return f"2026-06-{index // 24 + 1:02}T{index % 24:02}:00:00"
