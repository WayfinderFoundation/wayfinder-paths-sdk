from __future__ import annotations

from typing import Any

import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)
from wayfinder_paths.jobs.strategies import (
    build_imx_atr_target,
    build_imx_momentum,
    build_snx_momentum,
)


def bars_from_closes(
    closes: list[float],
    *,
    symbol: str,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for index, close in enumerate(closes):
        hour = index
        rows.append(
            {
                "timestamp": (f"2026-01-{hour // 24 + 1:02}T{hour % 24:02}:00:00Z"),
                "symbol": symbol,
                "open": close,
                "high": highs[index] if highs else close * 1.002,
                "low": lows[index] if lows else close * 0.998,
                "close": close,
                "volume": 100,
            }
        )
    return rows


def _spec() -> ExecutionSpec:
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    return spec


def _run(builder, closes, *, symbol, params=None, highs=None, lows=None):
    dataset = PreparedExecutionDataset.from_rows(
        bars_from_closes(closes, symbol=symbol, highs=highs, lows=lows)
    )
    return simulate_execution(builder, dataset, _spec(), params or {})


def _intents(result, action: str) -> list[dict[str, Any]]:
    return [row for row in result.trace["intents"] if row["action"] == action]


# ── entry gate ───────────────────────────────────────────────────────────────


def test_snx_no_entry_without_new_low() -> None:
    closes = [10.0 + (i % 3) * 0.01 for i in range(30)]  # choppy, no new lows

    result = _run(build_snx_momentum, closes, symbol="SNX")

    assert _intents(result, "OPEN") == []


def test_snx_enters_on_new_low_5() -> None:
    closes = [10.0 + (i % 3) * 0.01 for i in range(28)] + [9.5]

    result = _run(build_snx_momentum, closes, symbol="SNX")

    opens = _intents(result, "OPEN")
    assert len(opens) == 1
    assert opens[0]["side"] == "sell"
    assert opens[0]["size"] == round(2500.0 / 9.5, 1)
    assert opens[0]["bracket"]["stop_loss"] == pytest.approx(9.5 * 1.07)


def test_imx_floor_filter_blocks_entry_near_sma50() -> None:
    # gentle downtrend keeps the close within 1% of SMA50 at the break
    closes = [0.130 - i * 0.00004 for i in range(55)]
    closes.append(closes[-1] * 0.999)  # a new low, but ~0.3% under SMA50

    result = _run(build_imx_momentum, closes, symbol="IMX")

    assert _intents(result, "OPEN") == []


def test_imx_enters_when_floor_buffer_exists() -> None:
    closes = [0.130] * 55 + [0.121]  # sharp break: ~7% under SMA50

    result = _run(build_imx_momentum, closes, symbol="IMX")

    opens = _intents(result, "OPEN")
    assert len(opens) == 1
    assert opens[0]["size"] == round(3000.0 / 0.121, 1)


# ── exit hook gating (direct unit checks) ────────────────────────────────────


def test_snx_st_floor_gated_but_sma20_bounce_ungated() -> None:
    strategy = build_snx_momentum({})
    kwargs = {
        "closes": [10.0] * 25,
        "sma20": [0.0] * 24 + [9.0],  # close > sma20 -> bounce
        "indicators": {"supertrend": [1] * 25},
        "state": {},
        "entry_price": 10.5,
    }

    assert strategy.exit_reason(**kwargs, bars_since_entry=1) == "sma20_bounce"

    floor_kwargs = {
        **kwargs,
        "sma20": [0.0] * 24 + [11.0],  # close < sma20 -> no bounce
        "indicators": {"supertrend": [1] * 24 + [-1]},
    }
    assert strategy.exit_reason(**floor_kwargs, bars_since_entry=1) is None
    assert strategy.exit_reason(**floor_kwargs, bars_since_entry=2) == "st_floor"


def test_imx_momentum_exits_gated_by_min_hold() -> None:
    strategy = build_imx_momentum({})
    kwargs = {
        "closes": [0.120] * 55,
        "sma20": [0.0] * 54 + [0.118],  # close > sma20 -> bounce condition true
        "indicators": {"sma_floor": [0.0] * 54 + [0.125]},  # close < floor too
        "state": {},
        "entry_price": 0.121,
    }

    assert strategy.exit_reason(**kwargs, bars_since_entry=1) is None
    assert strategy.exit_reason(**kwargs, bars_since_entry=2) == "sma50_floor"


def test_imx_atr_target_uses_entry_atr_and_ref() -> None:
    strategy = build_imx_atr_target({})
    kwargs = {
        "closes": [0.100] * 25,
        "sma20": [0.0] * 24 + [0.115],  # close < sma20 -> no bounce
        "indicators": {"atr": [0.0] * 24 + [0.004]},
        "state": {"entry_atr": 0.005},
        "entry_price": 0.112,  # target = 0.112 - 2*0.005 = 0.102 >= close
    }

    assert strategy.exit_reason(**kwargs, bars_since_entry=1) is None
    assert strategy.exit_reason(**kwargs, bars_since_entry=2) == "atr_target"

    shallow = {**kwargs, "state": {"entry_atr": 0.008}}  # target 0.096 < close
    assert shallow["state"]["entry_atr"] == 0.008
    assert strategy.exit_reason(**shallow, bars_since_entry=2) is None


# ── stop + rearm through the engine ─────────────────────────────────────────


def test_bracket_stop_fires_and_rearms_until_sma20_recovery() -> None:
    base = [10.0 + (i % 3) * 0.01 for i in range(28)]
    entry_bar = [9.5]  # NewLow5 -> OPEN queued, fills next bar
    fill_bar = [9.5]
    stop_bar = [9.6]  # high spike crosses 9.5*1.07 = 10.165 intrabar
    post_stop_lows = [9.4, 9.3, 9.2]  # fresh NewLow5s: must NOT re-enter (rearm)
    recovery = [10.6, 10.7]  # close > SMA20 lifts rearm
    re_entry = [9.0]  # NewLow5 after lift -> re-enter
    closes = (
        base + entry_bar + fill_bar + stop_bar + post_stop_lows + recovery + re_entry
    )
    highs = [c * 1.002 for c in closes]
    highs[len(base) + 2] = 10.30  # the stop bar's high

    result = _run(build_snx_momentum, closes, symbol="SNX", highs=highs)

    stops = [
        fill
        for fill in result.trace["fills"]
        if fill["raw"].get("intent_action") == "STOP_LOSS"
        and fill["status"] == "filled"
    ]
    assert len(stops) == 1, "the 7% bracket stop must fire on the spike bar"
    opens = _intents(result, "OPEN")
    assert len(opens) == 2, "no re-entry while rearmed; one re-entry after recovery"
    assert opens[1]["timestamp"] == result.trace["runs"][-1]["timestamp"]


def test_exit_sets_rearm_and_blocks_next_new_low() -> None:
    # Mirrors the recorded prod round-trip shape: entry below SMA50, Min-2
    # gate blocks the floor exit at entry+1, floor exit fires at entry+2,
    # rearm then blocks the very next NewLow5 until a close > SMA20.
    base = [0.130] * 55
    entry = [0.121]  # NewLow5 -> OPEN queued
    fill = [0.1205]  # fills at this bar's open; gate blocks (bars=1)
    exit_bar = [0.1207]  # bars=2: close < SMA50 -> floor exit
    blocked_low = [0.118]  # fresh NewLow5 while rearmed -> must NOT re-enter
    recovery = [0.1320]  # close > SMA20 lifts rearm
    closes = base + entry + fill + exit_bar + blocked_low + recovery

    result = _run(build_imx_momentum, closes, symbol="IMX")

    closes_intents = _intents(result, "CLOSE")
    assert len(closes_intents) == 1
    assert closes_intents[0]["metadata"]["exit_reason"] == "sma50_floor"
    assert len(_intents(result, "OPEN")) == 1, "rearm must block the next NewLow5"
