"""Opt-in liquidation simulation (legacy total-wipe port).

The model is the legacy check from core/backtesting/backtester.py: at each
bar close, breach iff `equity > 0 and mm > 0 and equity < mm * (1 + buffer)`
with `mm = Σ |size×close| × rate(sym)`. On breach all positions force-close
at the bar close and equity pins to exactly 0 for the rest of the run.
Default-off is bit-identical to before the feature existed, and the config
never reaches the live/paper driver (venues do real liquidations).
"""

from __future__ import annotations

from typing import Any

import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.engine import EngineState, LiquidationConfig
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)
from wayfinder_paths.jobs.strategies import build_snx_momentum
from wayfinder_paths.tests.test_jobs_strategies_scenarios import bars_from_closes

CAPITAL = 1000.0
SIZE = 10.0


class ShortOnce:
    """Shorts 10 units on the first bar and never exits."""

    def decide(self, ctx: Any) -> list[dict[str, Any]]:
        if ctx.strategy_state.get("entered"):
            return []
        ctx.strategy_state["entered"] = True
        return [
            {
                "action": "OPEN",
                "venue": "hyperliquid",
                "symbol": "SNX",
                "side": "sell",
                "size": SIZE,
            }
        ]


def _spec() -> ExecutionSpec:
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    return spec


def _run(closes: list[float], params: dict[str, Any]):
    return simulate_execution(
        lambda _params: ShortOnce(),
        PreparedExecutionDataset.from_rows(bars_from_closes(closes, symbol="SNX")),
        _spec(),
        {"initial_capital": CAPITAL, **params},
    )


# Entry decided bar0, fills bar1 open (=100). Short bleeds as price pumps:
# bar4 close 195 -> equity 50 < mm 97.5 * 1.001 -> breach.
PUMP = [100.0, 100.0, 150.0, 185.0, 195.0, 210.0]


def test_default_off_is_bit_identical() -> None:
    fixture = [10.0 + (i % 3) * 0.01 for i in range(28)] + [9.0, 8.9, 11.5]

    def _snx(params: dict[str, Any]):
        return simulate_execution(
            build_snx_momentum,
            PreparedExecutionDataset.from_rows(bars_from_closes(fixture, symbol="SNX")),
            _spec(),
            {"symbol": "SNX", "notional_usd": 500.0, **params},
        )

    implicit = _snx({})
    explicit = _snx({"enable_liquidation": False})
    assert implicit.stats == explicit.stats
    assert implicit.trades == explicit.trades
    assert implicit.equity_curve == explicit.equity_curve
    assert implicit.stats["liquidation_count"] == 0
    assert implicit.stats["liquidated_at"] is None


def test_short_squeeze_liquidates_and_pins_equity_to_zero() -> None:
    result = _run(PUMP, {"enable_liquidation": True})

    liquidations = [
        e for e in result.trace["guard_events"] if e["kind"] == "liquidation"
    ]
    assert len(liquidations) == 1
    assert result.stats["liquidation_count"] == 1
    breach_ts = liquidations[0]["timestamp"]
    assert result.stats["liquidated_at"] == breach_ts

    # Forced close is a reduce-only fill at the breach bar's close, tagged.
    forced = [
        t
        for t in result.trades
        if t["reduce_only"] and t["raw"]["intent_metadata"].get("liquidation")
    ]
    assert len(forced) == 1
    assert forced[0]["timestamp"] == breach_ts
    assert forced[0]["avg_price"] == pytest.approx(195.0)

    # Equity pins to exactly 0 on the breach bar; later bars are skipped
    # (legacy `break` equivalent) so the curve ends there.
    assert result.equity_curve[-1]["timestamp"] == breach_ts
    assert result.equity_curve[-1]["equity"] == 0.0
    assert result.stats["net_return"] == pytest.approx(-1.0)


def test_breach_bar_matches_embedded_legacy_formula() -> None:
    result = _run(PUMP, {"enable_liquidation": True})
    entry = result.trades[0]
    avg = float(entry["avg_price"])
    buffer = 0.001
    rate = 0.05
    expected_breach_index = None
    for index, close in enumerate(PUMP):
        if index < 1:  # position exists from bar1 onward
            continue
        equity = CAPITAL + (avg - close) * SIZE  # short direction
        mm = SIZE * close * rate
        if equity > 0 and mm > 0 and equity < mm * (1 + buffer):
            expected_breach_index = index
            break
    assert expected_breach_index == 4
    breach_ts = result.stats["liquidated_at"]
    assert breach_ts == result.equity_curve[-1]["timestamp"]
    assert breach_ts.startswith("2026-01-01T04")


def test_equity_below_zero_never_liquidates() -> None:
    # Legacy gate: portfolio_value > 0. A gap straight through zero equity
    # produces a negative account, not a liquidation event.
    result = _run([100.0, 100.0, 250.0], {"enable_liquidation": True})
    assert result.stats["liquidation_count"] == 0
    assert result.equity_curve[-1]["equity"] < 0


def test_buffer_boundary() -> None:
    # bar2 close 185: equity 150, mm 92.5. buffer 0 -> no breach; buffer 1.0
    # -> threshold 185 -> breach. Also proves buffer=0.0 is honored (not
    # silently replaced by the 0.001 default).
    closes = [100.0, 100.0, 185.0]
    safe = _run(closes, {"enable_liquidation": True, "liquidation_buffer": 0.0})
    wide = _run(closes, {"enable_liquidation": True, "liquidation_buffer": 1.0})
    assert safe.stats["liquidation_count"] == 0
    assert wide.stats["liquidation_count"] == 1


def test_per_symbol_maintenance_margin_override() -> None:
    # bar2 close 150: equity 500. Default rate 0.05 -> mm 75 (safe);
    # override SNX to 0.5 -> mm 750 -> breach.
    closes = [100.0, 100.0, 150.0]
    default = _run(closes, {"enable_liquidation": True})
    override = _run(
        closes,
        {
            "enable_liquidation": True,
            "maintenance_margin_by_symbol": {"SNX": 0.5},
        },
    )
    assert default.stats["liquidation_count"] == 0
    assert override.stats["liquidation_count"] == 1


def test_config_from_params_defaults_and_legacy_symbol_table() -> None:
    assert LiquidationConfig.from_params({}) is None
    assert LiquidationConfig.from_params({"enable_liquidation": False}) is None
    config = LiquidationConfig.from_params({"enable_liquidation": True})
    assert config is not None
    assert config.maintenance_margin_rate == 0.05
    assert config.rate_for("SNX") == 0.05  # not in the legacy table
    assert config.rate_for("BTC") == pytest.approx(1 / 100.0)
    assert config.rate_for("AVNT/USDC:USDC") == pytest.approx(1 / 10.0)


def test_engine_state_round_trips_liquidated_at() -> None:
    state = EngineState()
    state.liquidated_at = "2026-01-01T04:00:00+00:00"
    restored = EngineState.from_dict(state.to_dict())
    assert restored.liquidated_at == "2026-01-01T04:00:00+00:00"
    # Old state files without the key load as not-liquidated.
    legacy_payload = {k: v for k, v in state.to_dict().items() if k != "liquidated_at"}
    assert EngineState.from_dict(legacy_payload).liquidated_at is None
