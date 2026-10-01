"""Contracts for the risk-balanced starter revision, not profitability tests."""

import pandas as pd
import pytest

from wayfinder_paths.jobs.execution.primitives import PositionRecord
from wayfinder_paths.jobs.strategies.risk_balanced_sleeves import build_strategy
from wayfinder_paths.tests.test_jobs_starters import _context


def _strategy():
    return build_strategy(
        {
            "symbols": ["A", "B", "C", "D"],
            "sleeves": [["A", "B"], ["C", "D"]],
            "momentum_bars": 2,
            "risk_window_bars": 3,
            "rebalance_bars": 1,
            "rebalance_offset": 0,
            "min_trade_notional": 0,
        }
    )


def _intent_context():
    strategy = _strategy()
    return strategy, _context(
        strategy,
        {
            "A": [1, 1.05, 1.1, 1.2, 1.35, 1.55, 1.8],
            "B": [1, 0.98, 0.95, 0.9, 0.85, 0.78, 0.7],
            "C": [1, 0.97, 0.94, 0.9, 0.84, 0.77, 0.7],
            "D": [1, 1.04, 1.08, 1.15, 1.27, 1.42, 1.6],
        },
        interval="15min",
    )


def test_pair_neutrality_caps_and_cash_residual() -> None:
    strategy = _strategy()
    scores = {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0}
    weights = strategy.risk_weights(scores, {"A": 1.0, "B": 1.0, "C": 2.0, "D": 2.0})
    assert weights["A"] == -weights["B"]
    assert weights["C"] == -weights["D"]
    assert sum(weights.values()) == pytest.approx(0)
    assert abs(weights["A"]) + abs(weights["B"]) == pytest.approx(0.35)
    # A cap leaves cash; it does not lever the other sleeve to compensate.
    assert sum(abs(value) for value in weights.values()) < 1
    assert strategy.risk_weights(scores, {"A": 0.0, "B": 0.0, "C": 2.0, "D": 2.0}) == {}


@pytest.mark.parametrize("leverage", [1, 2, 5])
def test_leverage_applied_once_and_openings_keep_native_stops(leverage) -> None:
    strategy, ctx = _intent_context()
    baseline = {row["symbol"]: row for row in strategy.decide(ctx)}
    ctx.params["leverage"] = leverage
    intents = strategy.decide(ctx)
    assert len(intents) == 4
    assert sum(row["notional"] for row in intents) <= 10_000 * leverage
    for row in intents:
        assert row["notional"] == pytest.approx(
            leverage * baseline[row["symbol"]]["notional"]
        )
        assert row["notional"] <= 10_000 * leverage * 0.35 / 2
        assert row["metadata"]["leverage_applied"] is True
        assert row["bracket"]["stop_loss_pct"] > 0
        assert row["bracket"]["native_required"] is True


def test_cooldown_does_not_cancel_reduce_only_flip_exit() -> None:
    strategy, ctx = _intent_context()
    ctx.ledger.positions["A"] = PositionRecord(
        symbol="A", side="short", size=1000, avg_price=1.8
    )
    ctx.strategy_state["protection_cooldowns"] = {"A": "2027-01-01T00:00:00Z"}
    intents = [row for row in strategy.decide(ctx) if row["symbol"] == "A"]
    assert len(intents) == 1
    assert intents[0]["action"] == "CLOSE"
    assert intents[0]["reduce_only"] is True
    assert intents[0]["size"] == 1000


def test_relative_deadband_prevents_churn_but_allows_material_reduction() -> None:
    strategy, ctx = _intent_context()
    target = next(row for row in strategy.decide(ctx) if row["symbol"] == "A")
    target_size = target["notional"] / 1.8
    ctx.ledger.positions["A"] = PositionRecord(
        symbol="A", side="long", size=target_size * 0.9, avg_price=1.8
    )
    assert not [row for row in strategy.decide(ctx) if row["symbol"] == "A"]
    ctx.ledger.positions["A"].size = target_size * 2
    reductions = [row for row in strategy.decide(ctx) if row["symbol"] == "A"]
    assert len(reductions) == 1
    assert reductions[0]["action"] == "CLOSE"
    assert reductions[0]["reduce_only"] is True
    assert reductions[0]["size"] == pytest.approx(target_size)


def test_precompute_is_causal_and_missing_peer_does_not_invent_volatility() -> None:
    strategy = _strategy()
    timestamps = pd.date_range("2026-01-01", periods=40, freq="15min", tz="UTC")
    frames = {
        symbol: pd.DataFrame(
            {
                "timestamp": timestamps,
                "close": [
                    100 + i * 0.07 + (i % 7) * (rank + 1) * 0.02 for i in range(40)
                ],
                "high": [102 + i * 0.1 for i in range(40)],
                "low": [98 + i * 0.05 for i in range(40)],
                "volume": 100.0,
            }
        )
        for rank, symbol in enumerate(strategy.params["symbols"])
    }
    full = strategy.precompute(frames)
    prefix = strategy.precompute({s: f.iloc[:35] for s, f in frames.items()})
    for symbol in frames:
        pd.testing.assert_frame_equal(full[symbol].iloc[:35], prefix[symbol])
    frames["B"] = frames["B"].drop(index=38)
    incomplete = strategy.precompute(frames)
    assert pd.notna(full["A"].starter_sleeve_risk.iloc[-1])
    assert pd.isna(incomplete["A"].starter_sleeve_risk.iloc[-1])


@pytest.mark.parametrize(
    "params",
    [{"risk_window_bars": 1}, {"max_sleeve_gross": 0}, {"max_sleeve_gross": 1.1}],
)
def test_invalid_risk_parameters_are_rejected(params) -> None:
    with pytest.raises(ValueError, match="Invalid risk"):
        build_strategy(params)
