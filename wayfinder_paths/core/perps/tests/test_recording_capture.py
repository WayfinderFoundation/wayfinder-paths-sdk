"""Verify the live-intent capture path: ActivePerpsStrategy._run_trigger wraps
each handler in RecordingHandler, and intents flow into the snapshot under
`state["orders"][venue]`. The reconciler then projects them back via
ReconcileHandler.recorded_live_intents.

This is a smoke test specifically aimed at fix C from the gap analysis — it
exercises the capture without requiring a real rebalance to fire.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pandas as pd
import pytest

from wayfinder_paths.core.perps.handlers.backtest import BacktestHandler
from wayfinder_paths.core.perps.handlers.reconcile import ReconcileHandler
from wayfinder_paths.core.perps.handlers.recording import RecordingHandler
from wayfinder_paths.core.perps.state import StateStore

STRATEGY_NAME = "__recording_capture_test__"


@pytest.fixture
def cleanup():
    yield
    shutil.rmtree(Path(".wayfinder/state") / STRATEGY_NAME, ignore_errors=True)


def test_record_then_reconcile_round_trip(cleanup):
    """Place orders → snapshot → load → recorded_live_intents matches."""
    idx = pd.date_range("2026-05-07 10:00:00+00:00", periods=3, freq="1h")
    prices = pd.DataFrame({"BTC": [100.0, 101.0, 102.0]}, index=idx)
    inner = BacktestHandler(
        "perp", prices, None, slippage_bps=0, fee_bps=0, min_order_usd=0.01
    )
    inner.set_bar(1)
    rec = RecordingHandler(inner)

    state = StateStore(STRATEGY_NAME, "live")

    async def fake_run_trigger():
        await rec.place_order("BTC", "buy", 0.5, "market")
        await rec.place_order("BTC", "sell", 0.2, "market")
        pos = await rec.get_positions()
        state.update(
            {
                "positions": {
                    "perp": {
                        sym: {
                            "size": p.size,
                            "entry_price": p.entry_price,
                            "mark_price": p.mark_price,
                        }
                        for sym, p in pos.items()
                    }
                },
                "orders": {"perp": list(rec.intents)},
                "mids": {"perp": {sym: rec.mid(sym) for sym in ["BTC"]}},
                "signal_row": {"BTC": 0.7},
                "trigger_ts": idx[1].isoformat(),
                "nav": 1000.0,
            }
        )
        state.write_snapshot(idx[1].to_pydatetime())

    asyncio.run(fake_run_trigger())

    # Two intents recorded into the wrapper.
    assert len(rec.intents) == 2

    # ReconcileHandler reconstructs them from the persisted snapshot.
    recon = ReconcileHandler(
        venue="perp",
        prices=prices,
        funding=None,
        strategy_name=STRATEGY_NAME,
    )
    recon.set_bar(1)
    snap = recon.load_snapshot_at(idx[1].to_pydatetime())

    assert snap["nav"] == 1000.0
    assert snap["signal_row"] == {"BTC": 0.7}
    assert snap["trigger_ts"] == idx[1].isoformat()

    intents = recon.recorded_live_intents
    assert len(intents) == 2
    assert {i["side"] for i in intents} == {"buy", "sell"}
    assert {i["symbol"] for i in intents} == {"BTC"}
    assert all(i["venue"] == "perp" for i in intents)

    # Snapshotted mid is honoured for deterministic replay.
    assert recon.mid("BTC") == 101.0


def test_load_snapshot_at_handles_misaligned_trigger_ts(cleanup):
    """Live snapshots are written at trigger time (e.g. T+8min), not bar-aligned.
    The reconciler iterates bar-aligned timestamps (T+0). The lookup must map
    a snapshot inside `[bar_t, bar_t + interval)` to bar_t — otherwise every
    snapshot is dropped and `recorded_live_intents` is silently empty.
    """
    from datetime import timedelta

    idx = pd.date_range("2026-05-13 00:00:00+00:00", periods=3, freq="1h")
    prices = pd.DataFrame({"BTC": [100.0, 101.0, 102.0]}, index=idx)
    inner = BacktestHandler(
        "perp", prices, None, slippage_bps=0, fee_bps=0, min_order_usd=0.01
    )
    inner.set_bar(0)
    rec = RecordingHandler(inner)

    state = StateStore(STRATEGY_NAME, "live")
    trigger_t = idx[0].to_pydatetime() + timedelta(minutes=8, seconds=44)

    async def fake_run_trigger():
        await rec.place_order("BTC", "buy", 0.3, "market")
        state.update({"orders": {"perp": list(rec.intents)}, "nav": 500.0})
        state.write_snapshot(trigger_t)

    asyncio.run(fake_run_trigger())

    recon = ReconcileHandler(
        venue="perp", prices=prices, funding=None, strategy_name=STRATEGY_NAME
    )
    recon.set_bar(0)
    snap = recon.load_snapshot_at(idx[0].to_pydatetime())

    assert snap.get("nav") == 500.0
    assert len(recon.recorded_live_intents) == 1
    assert recon.recorded_live_intents[0]["side"] == "buy"
