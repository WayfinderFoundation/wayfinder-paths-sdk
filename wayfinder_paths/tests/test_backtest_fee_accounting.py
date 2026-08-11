"""Fees must reduce backtest PnL, not just appear as a separate stat.

Regression for the fee-blind backtest bug: the engine computed a per-fill fee
but never subtracted it from realized_pnl / equity, and defaulted fee_bps to 0.
So net_return, win_rate, and per-trade PnL were all reported GROSS of fees —
flattering small-edge (esp. Hyperliquid) strategies that bleed fees live.
"""

from __future__ import annotations

import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)

SYMBOL = "X"
NOTIONAL = 1000.0
INITIAL = 100_000.0


class _OneShot:
    """Opens one long, closes it next bar — on flat prices, so gross PnL is
    exactly zero and any net_return change is pure fee."""

    def __init__(self, params=None):
        self.params = {"symbol": SYMBOL, "venue": "hyperliquid", "notional": NOTIONAL}
        self.params.update(params or {})

    def min_bars(self):
        return 2

    def decide(self, ctx):
        sym = self.params["symbol"]
        frame = ctx.view.symbol_frame(sym)
        if len(frame) < 2:
            return []
        pos = ctx.ledger.positions.get(sym)
        st = ctx.strategy_state
        close = float(frame["close"].to_numpy(dtype=float)[-1])
        if pos is None and not st.get("done"):
            return [
                {
                    "action": "OPEN",
                    "venue": self.params["venue"],
                    "symbol": sym,
                    "side": "buy",
                    "size": round(self.params["notional"] / close, 6),
                    "metadata": {},
                }
            ]
        if pos is not None:
            st["done"] = True
            return [
                {
                    "action": "CLOSE",
                    "venue": self.params["venue"],
                    "symbol": sym,
                    "side": "sell",
                    "size": pos.size,
                    "reduce_only": True,
                    "metadata": {},
                }
            ]
        return []


def _build(params=None):
    return _OneShot(params)


def _flat_bars(n=5, price=100.0):
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


def _spec():
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1m"
    return spec


def _run(extra):
    ds = PreparedExecutionDataset.from_rows(_flat_bars())
    params = {"symbol": SYMBOL, "initial_capital": INITIAL, "slippage_bps": 0.0}
    params.update(extra)
    return simulate_execution(_build, ds, _spec(), params).stats


def test_zero_fee_is_flat_on_flat_prices():
    s = _run({"fee_bps": 0.0})
    assert s["total_fees"] == pytest.approx(0.0)
    assert s["net_return"] == pytest.approx(0.0, abs=1e-9)


def test_fees_reduce_net_return_and_equity():
    # 1% per side on $1000 notional = $10 per fill, charged on entry AND exit.
    s = _run({"fee_bps": 100.0})
    assert s["total_fees"] == pytest.approx(20.0, rel=1e-6)
    assert s["net_return"] == pytest.approx(-20.0 / INITIAL, rel=1e-6)
    assert s["ending_equity"] == pytest.approx(INITIAL - 20.0, rel=1e-6)


def test_hyperliquid_venue_applies_a_default_fee():
    # No fee_bps set + venue=hyperliquid -> 4.5bps taker default ($0.45/side).
    s = _run({})
    assert s["total_fees"] == pytest.approx(0.90, rel=1e-6)
    assert s["net_return"] < 0.0


def test_explicit_zero_fee_overrides_venue_default():
    s = _run({"fee_bps": 0.0})
    assert s["total_fees"] == pytest.approx(0.0)
