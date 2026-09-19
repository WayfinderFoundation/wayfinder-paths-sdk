"""A backtest on a spot venue rejects exactly what paper rejects: shorts,
brackets and limit orders never slip through the replay to be refused live."""

from __future__ import annotations

from typing import Any

import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec, OrderIntent
from wayfinder_paths.jobs.execution.engine import EngineState, _validate_intent
from wayfinder_paths.jobs.execution.hyperliquid import _paper_broker
from wayfinder_paths.jobs.execution.onchain import ONCHAIN_CAPABILITIES
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)

SYMBOL = "ethereum-base"
INTENTS: dict[str, dict[str, Any]] = {
    "short": {
        "action": "OPEN",
        "venue": "onchain",
        "symbol": SYMBOL,
        "side": "sell",
        "size": 1.0,
    },
    "bracket": {
        "action": "OPEN",
        "venue": "onchain",
        "symbol": SYMBOL,
        "side": "buy",
        "size": 1.0,
        "bracket": {"stop_loss": 90.0, "take_profit": 120.0},
    },
    "limit": {
        "action": "OPEN",
        "venue": "onchain",
        "symbol": SYMBOL,
        "side": "buy",
        "size": 1.0,
        "limit_price": 99.0,
        "time_in_force": "ALO",
    },
}


class _Emitter:
    def __init__(self, params=None):
        self.params = dict(params or {})

    def decide(self, ctx):
        if ctx.strategy_state.get("fired"):
            return []
        ctx.strategy_state["fired"] = True
        return [dict(INTENTS[self.params["shape"]], metadata={})]


def _build(params=None):
    return _Emitter(params)


def _bars(n: int = 6, price: float = 100.0) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": f"2026-05-01T00:0{i}:00Z",
            "symbol": SYMBOL,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 100,
        }
        for i in range(n)
    ]


def _spot_spec() -> ExecutionSpec:
    spec = ExecutionSpec()
    spec.market_kind = "spot"
    spec.venues = ["onchain"]
    spec.data_contract["bar_interval"] = "1m"
    return spec


def _paper_reason(shape: str) -> str | None:
    intent = OrderIntent(**INTENTS[shape])
    return _validate_intent(
        intent,
        brokers={"onchain": _paper_broker(ONCHAIN_CAPABILITIES, {}, venue="onchain")},
        auto_limits=None,
        state=EngineState(),
        ref_price=100.0,
        bar_iso="2026-05-01T00:01:00+00:00",
    )


@pytest.mark.parametrize("shape", ["short", "bracket", "limit"])
def test_backtest_rejects_what_paper_rejects(shape: str) -> None:
    result = simulate_execution(
        _build,
        PreparedExecutionDataset.from_rows(_bars()),
        _spot_spec(),
        {"symbol": SYMBOL, "initial_capital": 10_000.0, "shape": shape},
    )

    assert result.stats["trade_count"] == 0 and not result.trades
    rejections = [
        event
        for event in result.trace["guard_events"]
        if event.get("kind") == "intent_rejected"
    ]
    assert len(rejections) == 1, result.trace["guard_events"]
    paper_reason = _paper_reason(shape)
    assert paper_reason and "onchain" in paper_reason
    assert rejections[0]["reason"] == paper_reason


def test_plain_long_fills_and_pays_the_venue_default_fee() -> None:
    INTENTS["plain"] = {
        "action": "OPEN",
        "venue": "onchain",
        "symbol": SYMBOL,
        "side": "buy",
        "size": 1.0,
    }
    result = simulate_execution(
        _build,
        PreparedExecutionDataset.from_rows(_bars()),
        _spot_spec(),
        {"symbol": SYMBOL, "initial_capital": 10_000.0, "shape": "plain"},
    )
    (fill,) = [
        event for event in result.trace["fills"] if event.get("status") == "filled"
    ]
    assert fill["symbol"] == SYMBOL and fill["side"] == "buy"
    assert result.stats["total_fees"] == pytest.approx(100.0 * 30.0 / 10_000)
