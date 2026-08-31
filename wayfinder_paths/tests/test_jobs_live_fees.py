"""Live fee capture + funding attribution + equity reconciliation.

Live HL fills recorded fee=0.0 (the order ack carries no fee), making live
trade PnL gross of fees — observed live: trades +18.2c while venue equity
gained +8.8c (~5c fees + ~4c funding invisible). These pin the fee lookup,
the estimate fallback, funding recording, and the reconciliation identity.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

import wayfinder_paths.jobs.execution.hyperliquid as hl_module
from wayfinder_paths.jobs.execution.hyperliquid import (
    HyperliquidPerpBroker,
    _attach_fill_fee,
)
from wayfinder_paths.jobs.execution.primitives import FillEvent, PositionLedger
from wayfinder_paths.jobs.forward import ForwardRecorder


def _filled(size: float = 0.48, px: float = 57.32, **kw) -> FillEvent:
    return FillEvent(
        status="filled",
        venue="hyperliquid",
        symbol="HYPE",
        side="sell",
        filled_size=size,
        avg_price=px,
        order_id="12345",
        client_order_id="0xabc",
        **kw,
    )


def _no_sleep(monkeypatch) -> None:
    async def instant(_delay):
        return None

    monkeypatch.setattr(hl_module.asyncio, "sleep", instant)


@pytest.mark.asyncio
async def test_fee_from_user_fills_by_oid(monkeypatch) -> None:
    _no_sleep(monkeypatch)

    async def fake_fills(wallet_label, start_ms):
        return [
            {
                "oid": 12345,
                "sz": "0.48",
                "fee": "0.0125",
                "builderFee": "0.001",
                "time": start_ms + 100,
            },
            # decoy: different oid
            {"oid": 999, "sz": "1.0", "fee": "5.0", "time": start_ms + 100},
        ]

    monkeypatch.setattr(hl_module, "_user_fills_result", fake_fills)
    fill = _filled()
    await _attach_fill_fee(fill, wallet_label="w", submit_ms=1000, fee_bps=4.5)
    assert fill.fee == pytest.approx(0.0135)
    assert fill.raw["fee_source"] == "user_fills"


@pytest.mark.asyncio
async def test_fee_fallback_estimate_when_ledger_lags(monkeypatch) -> None:
    _no_sleep(monkeypatch)

    async def empty(wallet_label, start_ms):
        return []

    monkeypatch.setattr(hl_module, "_user_fills_result", empty)
    fill = _filled(size=0.48, px=57.32)
    await _attach_fill_fee(fill, wallet_label="w", submit_ms=1000, fee_bps=4.5)
    assert fill.fee == pytest.approx(abs(0.48 * 57.32) * 4.5 / 10_000)
    assert fill.raw["fee_source"] == "estimate"

    # Lookup blowing up entirely also degrades to the estimate, never raises.
    async def boom(wallet_label, start_ms):
        raise RuntimeError("venue down")

    monkeypatch.setattr(hl_module, "_user_fills_result", boom)
    fill2 = _filled()
    await _attach_fill_fee(fill2, wallet_label="w", submit_ms=1000, fee_bps=4.5)
    assert fill2.raw["fee_source"] == "estimate"


@pytest.mark.asyncio
async def test_fee_pro_rata_on_partial_ledger_coverage(monkeypatch) -> None:
    _no_sleep(monkeypatch)

    async def half(wallet_label, start_ms):
        # Ledger has caught up with only half the filled size.
        return [{"oid": 12345, "sz": "0.24", "fee": "0.00625", "time": 1100}]

    monkeypatch.setattr(hl_module, "_user_fills_result", half)
    fill = _filled(size=0.48)
    await _attach_fill_fee(fill, wallet_label="w", submit_ms=1000, fee_bps=4.5)
    assert fill.fee == pytest.approx(0.0125)  # pro-rata doubled
    assert fill.raw["fee_source"] == "user_fills"


def test_exit_fee_nets_into_realized_pnl() -> None:
    ledger = PositionLedger()
    entry = _filled(size=1.0, px=100.0)
    entry.side = "sell"
    entry.fee = 0.05
    ledger.apply_fill(entry)
    exit_fill = _filled(size=1.0, px=90.0, reduce_only=True)
    exit_fill.side = "buy"
    exit_fill.fee = 0.04
    before = ledger.realized_pnl
    ledger.apply_fill(exit_fill)
    # Short 100 -> 90 = +10 price pnl, minus the exit fee only in this delta.
    assert ledger.realized_pnl - before == pytest.approx(10.0 - 0.04)
    # Whole-trade realized includes BOTH fees.
    assert ledger.realized_pnl == pytest.approx(10.0 - 0.05 - 0.04)


@pytest.mark.asyncio
async def test_fetch_state_stamps_cum_funding(monkeypatch) -> None:
    async def fake_state(wallet_label):
        return {
            "perp_positions": [
                {
                    "coin": "HYPE",
                    "szi": "-0.48",
                    "entryPx": "57.32",
                    "cumFunding": {"sinceOpen": "-0.00892"},
                }
            ],
            "summary": {"unified_usdc_equity": 109.86},
        }

    monkeypatch.setattr(hl_module, "_hl_state_result", fake_state)
    broker = HyperliquidPerpBroker(wallet_label="w")
    state = await broker.fetch_state()
    assert state.positions["HYPE"].metadata["cum_funding_since_open"] == pytest.approx(
        -0.00892
    )


def test_forward_funding_and_reconciliation_summary(tmp_path) -> None:
    recorder = ForwardRecorder(job_id="j", job_dir=tmp_path, mode="live")
    recorder.record_fill({"symbol": "HYPE", "fee": 0.0125})
    recorder.record_fill({"symbol": "HYPE", "fee": 0.011, "reduce_only": True})
    recorder.record_funding({"time_ms": 1, "coin": "HYPE", "usdc": -0.004})
    recorder.record_funding({"time_ms": 2, "coin": "HYPE", "usdc": 0.009})
    recon = {"drift": 0.0, "venue_equity_now": 109.95}
    recorder.record_tick(reconciliation=recon)

    summary = recorder.summary()
    assert summary["fills"]["fees_total"] == pytest.approx(0.0235)
    assert summary["funding"]["count"] == 2
    assert summary["funding"]["total_usd"] == pytest.approx(0.005)
    assert summary["reconciliation"] == recon
    funding_path = tmp_path / "results" / "forward" / "funding.jsonl"
    assert len(funding_path.read_text().splitlines()) == 2


@pytest.mark.asyncio
async def test_collect_funding_cursor_dedupe(tmp_path) -> None:
    from wayfinder_paths.jobs.execution.driver import _collect_funding

    calls: list[int] = []

    class FakeBroker:
        async def get_funding_payments(self, since_ms):
            calls.append(since_ms)
            return [
                {"time_ms": 5_000, "coin": "HYPE", "usdc": -0.004},
                {"time_ms": 6_000, "coin": "HYPE", "usdc": -0.001},
            ]

    brokers = {"hyperliquid": FakeBroker()}
    now = pd.Timestamp("2026-08-17T00:00:00Z")

    # First call seeds the cursor at now and returns nothing (pre-go-live
    # funding is not the job's).
    rows = await _collect_funding(brokers, tmp_path, "live", now)
    assert rows == []
    assert not calls
    cursor = json.loads((tmp_path / "state" / "funding_state.json").read_text())
    assert cursor["cursor_ms"] == int(now.timestamp() * 1000)

    # Rewind the cursor: rows past it are returned and advance it.
    (tmp_path / "state" / "funding_state.json").write_text(
        json.dumps({"cursor_ms": 4_000})
    )
    rows = await _collect_funding(brokers, tmp_path, "live", now)
    assert [r["time_ms"] for r in rows] == [5_000, 6_000]
    cursor = json.loads((tmp_path / "state" / "funding_state.json").read_text())
    assert cursor["cursor_ms"] == 6_000

    # Same rows again: all at/below cursor -> deduped to nothing.
    rows = await _collect_funding(brokers, tmp_path, "live", now)
    assert rows == []

    # Paper mode records nothing and never touches the cursor.
    assert await _collect_funding(brokers, tmp_path, "paper", now) == []


def test_reconciliation_block_identity(tmp_path) -> None:
    from wayfinder_paths.jobs.execution.driver import _reconciliation_block

    recorder = ForwardRecorder(job_id="j", job_dir=tmp_path, mode="live")
    recorder.record_funding({"time_ms": 1, "coin": "HYPE", "usdc": 0.009})

    class FakeView:
        def latest(self, symbol):
            return {"close": 55.0}

    tick = SimpleNamespace(
        snapshot=SimpleNamespace(data={"account_value": 110.0}),
        ledger_snapshot={
            "realized_pnl": 0.15,
            "positions": {"HYPE": {"side": "short", "size": 0.48, "avg_price": 57.32}},
        },
        guard_events=[],
    )
    block = _reconciliation_block(
        tick, root=tmp_path, recorder=recorder, mode="live", view=FakeView()
    )
    # First tick seeds: start=110, realized_at_seed=0.15 -> delta 0.
    unrealized = (57.32 - 55.0) * 0.48
    assert block["venue_equity_start"] == 110.0
    assert block["ledger_realized_delta"] == 0.0
    assert block["unrealized"] == pytest.approx(unrealized, abs=1e-6)
    assert block["funding_total"] == pytest.approx(0.009)
    assert block["drift"] == pytest.approx(
        110.0 - (110.0 + 0.0 + 0.009 + unrealized), abs=1e-6
    )

    # Later tick: realized moved +0.10, venue equity fully explains it.
    tick2 = SimpleNamespace(
        snapshot=SimpleNamespace(
            data={"account_value": 110.0 + 0.10 + 0.009 + unrealized}
        ),
        ledger_snapshot={
            "realized_pnl": 0.25,
            "positions": {"HYPE": {"side": "short", "size": 0.48, "avg_price": 57.32}},
        },
        guard_events=[],
    )
    block2 = _reconciliation_block(
        tick2, root=tmp_path, recorder=recorder, mode="live", view=FakeView()
    )
    assert block2["drift"] == pytest.approx(0.0, abs=1e-6)

    # Paper mode / missing account_value -> no block.
    tick3 = SimpleNamespace(
        snapshot=SimpleNamespace(data={}), ledger_snapshot={}, guard_events=[]
    )
    assert (
        _reconciliation_block(
            tick3, root=tmp_path, recorder=recorder, mode="live", view=FakeView()
        )
        is None
    )


def test_reconciliation_recovers_first_funded_tick_from_zero_seed(tmp_path) -> None:
    from wayfinder_paths.jobs.execution.driver import _reconciliation_block

    recorder = ForwardRecorder(job_id="j", job_dir=tmp_path, mode="live")
    recon_path = tmp_path / "state" / "equity_recon.json"
    recon_path.parent.mkdir(parents=True)
    recon_path.write_text(
        json.dumps(
            {
                "venue_equity_start": 0.0,
                "ledger_realized_at_seed": 0.0,
                "seeded_at": "2026-08-25T14:35:00+00:00",
            }
        )
    )
    ticks_path = tmp_path / "results" / "forward" / "ticks.jsonl"
    ticks_path.parent.mkdir(parents=True, exist_ok=True)
    ticks_path.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "mode": "live",
                    "ts": "2026-08-25T14:35:00+00:00",
                    "snapshot": {"data": {"account_value": 0.0}},
                    "ledger": {"realized_pnl": 0.0},
                },
                {
                    "mode": "live",
                    "ts": "2026-08-25T16:39:00+00:00",
                    "snapshot": {"data": {"account_value": 52.8}},
                    "ledger": {"realized_pnl": 0.0},
                },
            )
        )
        + "\n"
    )

    tick = SimpleNamespace(
        snapshot=SimpleNamespace(data={"account_value": 50.8}),
        ledger_snapshot={"realized_pnl": -2.0, "positions": {}},
        guard_events=[],
    )
    block = _reconciliation_block(
        tick,
        root=tmp_path,
        recorder=recorder,
        mode="live",
        view=SimpleNamespace(latest=lambda _symbol: None),
    )

    assert block["venue_equity_start"] == 52.8
    assert block["expected_equity"] == 50.8
    assert block["drift"] == 0.0
    recovered = json.loads(recon_path.read_text())
    assert recovered["seed_source"] == "first_positive_live_tick"


def test_reconciliation_does_not_pin_provisional_zero_equity(tmp_path) -> None:
    from wayfinder_paths.jobs.execution.driver import _reconciliation_block

    recorder = ForwardRecorder(job_id="j", job_dir=tmp_path, mode="live")
    tick = SimpleNamespace(
        snapshot=SimpleNamespace(data={"account_value": 0.0}),
        ledger_snapshot={"realized_pnl": 0.0, "positions": {}},
        guard_events=[],
    )
    block = _reconciliation_block(
        tick,
        root=tmp_path,
        recorder=recorder,
        mode="live",
        view=SimpleNamespace(latest=lambda _symbol: None),
    )

    assert block is None
    assert not (tmp_path / "state" / "equity_recon.json").exists()


def test_risk_equity_includes_funding(tmp_path) -> None:
    import wayfinder_paths.jobs.execution.risk as risk_module

    summary_path = tmp_path / "results" / "forward" / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "trades": {"net_pnl": 1.0},
                "funding": {"total_usd": -0.4},
            }
        )
    )
    from wayfinder_paths.jobs.execution.engine import EngineState

    class EmptyView:
        symbols: list[str] = []

        def latest(self, symbol):
            return None

    snapshot = risk_module.build_risk_snapshot(
        state=EngineState(),
        view=EmptyView(),
        params={"initial_capital": 100.0},
        root=tmp_path,
        now=pd.Timestamp("2026-08-17T00:00:00Z"),
    )
    assert snapshot["equity"] == pytest.approx(100.0 + 1.0 - 0.4)
