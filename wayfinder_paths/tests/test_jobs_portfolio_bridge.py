"""Weights→intents bridge: legacy target-weight strategies under jobs_v1."""

from __future__ import annotations

from typing import Any

import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec, FillEvent, PositionLedger
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionContext,
    StateSnapshot,
)
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)
from wayfinder_paths.jobs.strategies.portfolio import target_weights_to_intents
from wayfinder_paths.tests.test_jobs_strategies_scenarios import bars_from_closes

CAPITAL = 1000.0


def _ledger(*positions: tuple[str, str, float, float]) -> PositionLedger:
    ledger = PositionLedger()
    for symbol, side, size, price in positions:
        ledger.apply_fill(
            FillEvent(
                status="filled",
                venue="hyperliquid",
                symbol=symbol,
                side="buy" if side == "long" else "sell",
                filled_size=size,
                avg_price=price,
            )
        )
    return ledger


def _ctx(
    closes: dict[str, float],
    ledger: PositionLedger | None = None,
) -> ExecutionContext:
    rows = []
    for symbol, close in closes.items():
        rows.extend(bars_from_closes([close], symbol=symbol))
    return ExecutionContext(
        view=CompletedBarsView.from_rows(rows),
        ledger=ledger or PositionLedger(),
        state_snapshot=StateSnapshot(status="valid"),
        capacity=None,
        params={"initial_capital": CAPITAL},
        timestamp="2026-01-01T00:00:00+00:00",
        execution_spec=ExecutionSpec(),
    )


def test_opens_new_positions_long_and_short() -> None:
    intents = target_weights_to_intents(
        _ctx({"AAA": 10.0, "BBB": 20.0}), {"AAA": 0.5, "BBB": -0.5}
    )
    by_symbol = {intent["symbol"]: intent for intent in intents}
    assert by_symbol["AAA"]["action"] == "OPEN"
    assert by_symbol["AAA"]["side"] == "buy"
    assert by_symbol["AAA"]["notional"] == pytest.approx(500.0)
    assert by_symbol["BBB"]["side"] == "sell"
    assert by_symbol["BBB"]["notional"] == pytest.approx(500.0)
    assert by_symbol["AAA"]["metadata"]["target_weight"] == 0.5


def test_target_zero_closes_fully() -> None:
    ctx = _ctx({"AAA": 10.0}, _ledger(("AAA", "long", 50.0, 10.0)))
    intents = target_weights_to_intents(ctx, {"AAA": 0.0})
    assert len(intents) == 1
    assert intents[0]["action"] == "CLOSE"
    assert intents[0]["reduce_only"] is True
    assert intents[0]["size"] == pytest.approx(50.0)
    assert intents[0]["side"] == "sell"


def test_sign_flip_emits_close_then_open() -> None:
    ctx = _ctx({"AAA": 10.0}, _ledger(("AAA", "long", 50.0, 10.0)))
    intents = target_weights_to_intents(ctx, {"AAA": -0.5})
    assert [intent["action"] for intent in intents] == ["CLOSE", "OPEN"]
    assert intents[0]["size"] == pytest.approx(50.0)
    assert intents[1]["side"] == "sell"
    assert intents[1]["notional"] == pytest.approx(500.0)


def test_same_sign_shrink_partially_closes() -> None:
    # held 0.5 (50 units @ 10 / 1000 equity), target 0.25 -> close 25 units.
    ctx = _ctx({"AAA": 10.0}, _ledger(("AAA", "long", 50.0, 10.0)))
    intents = target_weights_to_intents(ctx, {"AAA": 0.25})
    assert len(intents) == 1
    assert intents[0]["action"] == "CLOSE"
    assert intents[0]["size"] == pytest.approx(25.0)


def test_same_sign_grow_opens_the_delta() -> None:
    ctx = _ctx({"AAA": 10.0}, _ledger(("AAA", "long", 50.0, 10.0)))
    intents = target_weights_to_intents(ctx, {"AAA": 0.75})
    assert len(intents) == 1
    assert intents[0]["action"] == "OPEN"
    assert intents[0]["notional"] == pytest.approx(250.0)


def test_gross_normalization_on_and_off() -> None:
    ctx = _ctx({"AAA": 10.0, "BBB": 20.0})
    normalized = target_weights_to_intents(ctx, {"AAA": 1.0, "BBB": -1.0})
    assert {intent["notional"] for intent in normalized} == {500.0}
    raw = target_weights_to_intents(
        ctx, {"AAA": 1.0, "BBB": -1.0}, normalize_gross=False
    )
    assert {intent["notional"] for intent in raw} == {1000.0}


def test_threshold_and_min_notional_suppression() -> None:
    ctx = _ctx({"AAA": 10.0}, _ledger(("AAA", "long", 50.0, 10.0)))
    # drift 0.02 below the 0.05 threshold -> hold
    assert (
        target_weights_to_intents(ctx, {"AAA": 0.52}, rebalance_threshold=0.05) == []
    )
    # delta notional 20 below min_trade_notional 50 -> hold
    assert (
        target_weights_to_intents(ctx, {"AAA": 0.52}, min_trade_notional=50.0) == []
    )


def test_zero_or_negative_equity_emits_nothing() -> None:
    ctx = _ctx({"AAA": 10.0})
    assert target_weights_to_intents(ctx, {"AAA": 0.5}, sizing_equity=0.0) == []
    assert target_weights_to_intents(ctx, {"AAA": 0.5}, sizing_equity=-10.0) == []


def test_sizing_equity_override_scales_notional() -> None:
    ctx = _ctx({"AAA": 10.0})
    intents = target_weights_to_intents(
        ctx, {"AAA": 0.5}, sizing_equity=CAPITAL * 2  # e.g. equity * leverage
    )
    assert intents[0]["notional"] == pytest.approx(1000.0)


def test_pure_function_identical_ctx_identical_intents() -> None:
    ledger_a = _ledger(("AAA", "long", 50.0, 10.0))
    ledger_b = _ledger(("AAA", "long", 50.0, 10.0))
    first = target_weights_to_intents(
        _ctx({"AAA": 10.0, "BBB": 20.0}, ledger_a), {"AAA": 0.2, "BBB": -0.3}
    )
    second = target_weights_to_intents(
        _ctx({"AAA": 10.0, "BBB": 20.0}, ledger_b), {"AAA": 0.2, "BBB": -0.3}
    )
    assert first == second


class ScheduledWeights:
    """Rebalances to a scheduled weight target at fixed bar indices."""

    def __init__(self, schedule: dict[int, dict[str, float]]) -> None:
        self.schedule = schedule

    def decide(self, ctx: Any) -> list[dict[str, Any]]:
        index = len(ctx.view.timestamps) - 1
        weights = self.schedule.get(index)
        if weights is None:
            return []
        return target_weights_to_intents(ctx, weights)


def test_end_to_end_rebalancing_through_simulator() -> None:
    bars = []
    bars.extend(bars_from_closes([10.0] * 8, symbol="AAA"))
    bars.extend(bars_from_closes([20.0] * 8, symbol="BBB"))
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"

    result = simulate_execution(
        lambda params: ScheduledWeights(
            {1: {"AAA": 0.5, "BBB": -0.5}, 4: {"AAA": 0.0, "BBB": -0.25}}
        ),
        PreparedExecutionDataset.from_rows(bars),
        spec,
        {"initial_capital": CAPITAL},
    )

    assert result.validation["execution_valid"] is True
    fills = [f for f in result.trace["fills"] if f["status"] == "filled"]
    # bar1 targets fill at bar2 open: long 50 AAA @10, short 25 BBB @20.
    opens = [f for f in fills if not f["reduce_only"]]
    assert {(f["symbol"], f["filled_size"]) for f in opens} == {
        ("AAA", 50.0),
        ("BBB", 25.0),
    }
    # bar4 rebalance fills at bar5 open: AAA fully closed, BBB shrunk to 12.5.
    closes = [f for f in fills if f["reduce_only"]]
    assert {(f["symbol"], f["filled_size"]) for f in closes} == {
        ("AAA", 50.0),
        ("BBB", 12.5),
    }
    final_positions = result.positions[-1]["positions"]
    assert "AAA" not in final_positions
    assert final_positions["BBB"]["size"] == pytest.approx(12.5)
