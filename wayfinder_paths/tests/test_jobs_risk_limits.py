"""RiskLimits halt layer for the live/paper driver.

`workspace/risk_limits.json` (legacy schema, reused directly) turns account-
level breaches into a `risk_halt` snapshot: the engine's existing non-valid
routing makes the tick reduce-only — exits still flow, new risk is blocked.
No file == byte-identical driver behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
import yaml

from wayfinder_paths.jobs.execution import FillEvent, PositionLedger
from wayfinder_paths.jobs.execution.driver import tick_job
from wayfinder_paths.jobs.execution.engine import EngineState
from wayfinder_paths.jobs.execution.paper import PaperBroker
from wayfinder_paths.jobs.execution.risk import build_risk_snapshot, check_risk_halt
from wayfinder_paths.tests.test_jobs_live_driver import (
    PERP_CAPS,
    FakeAdapter,
    _make_job,
    _now,
    _view,
)

CLOSING_STRATEGY = """
def decide(ctx):
    if "SNX" in ctx.ledger.positions:
        return [{"action": "CLOSE", "venue": "hyperliquid", "symbol": "SNX",
                 "side": "sell", "size": 1, "reduce_only": True}]
    return [{"action": "OPEN", "venue": "hyperliquid", "symbol": "SNX",
             "side": "long", "size": 1}]
""".lstrip()


def _write_limits(root: Path, limits: dict) -> None:
    path = root / "workspace" / "risk_limits.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(limits), encoding="utf-8")


def _write_summary(root: Path, trades: dict) -> None:
    path = root / "results" / "forward" / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"trades": trades}), encoding="utf-8")


def _write_peak(root: Path, peak: float) -> None:
    path = root / "state" / "risk_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"peak_equity": peak}), encoding="utf-8")


def _write_governance(tmp_path: Path, job_id: str, hard: dict) -> None:
    gov = tmp_path / "governance" / job_id
    gov.mkdir(parents=True, exist_ok=True)
    (gov / "hard_constraints.yaml").write_text(yaml.safe_dump(hard), encoding="utf-8")


def _seed_position(root: Path, *, size: float = 1.0, price: float = 9.0) -> None:
    state = EngineState(ledger=PositionLedger())
    state.ledger.apply_fill(
        FillEvent(
            status="filled",
            venue="hyperliquid",
            symbol="SNX",
            side="long",
            filled_size=size,
            avg_price=price,
        )
    )
    state.save(root / "state" / "engine_state.json")


async def _tick(job, root, store, *, view_count: int = 2):
    view = _view(view_count)
    return await tick_job(
        job,
        root,
        "paper",
        store=store,
        adapters={
            "hyperliquid": FakeAdapter(view, PaperBroker(capabilities=PERP_CAPS))
        },
        now=_now(view),
    )


async def test_no_limits_file_changes_nothing(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    result = await _tick(job, root, store)
    assert result["ok"] is True
    assert result["snapshot"]["status"] == "valid"
    assert not any(e["kind"] == "risk_halt" for e in result["guard_events"])
    # No limits -> no risk snapshot, no peak-equity persistence.
    assert not (root / "state" / "risk_state.json").exists()


async def test_max_drawdown_halts_opens_and_journals(tmp_path: Path) -> None:
    store, job, root = _make_job(
        tmp_path, params={"initial_capital": 10_000.0, "threshold": 0.0}
    )
    _write_limits(root, {"max_drawdown": -0.1})
    _write_summary(root, {"net_pnl": -2000.0, "current_loss_streak": 1})
    _write_peak(root, 10_000.0)  # equity 8000 vs peak 10000 -> dd -0.2

    result = await _tick(job, root, store)

    assert result["snapshot"]["status"] == "risk_halt"
    assert "max_drawdown" in result["snapshot"]["reason"]
    assert result["intents"] == []  # strategy's OPEN blocked
    assert any(
        e["kind"] == "intent_rejected" and "risk_halt" in e["reason"]
        for e in result["guard_events"]
    )
    halt_events = [e for e in result["guard_events"] if e["kind"] == "risk_halt"]
    assert len(halt_events) == 1
    assert halt_events[0]["snapshot"]["drawdown"] == pytest.approx(-0.2)
    journal = (root / "journal.jsonl").read_text(encoding="utf-8")
    assert "risk_halt" in journal


async def test_halt_still_routes_reduce_only_exits(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path, params={"initial_capital": 10_000.0})
    script = root / "workspace" / "src" / "strategy.py"
    script.write_text(CLOSING_STRATEGY, encoding="utf-8")
    _seed_position(root)
    _write_limits(root, {"max_drawdown": -0.1})
    _write_summary(root, {"net_pnl": -3000.0})
    _write_peak(root, 10_000.0)

    result = await _tick(job, root, store)

    assert result["snapshot"]["status"] == "risk_halt"
    assert len(result["intents"]) == 1
    assert result["intents"][0]["action"] == "CLOSE"


async def test_first_tick_seeds_peak_so_drawdown_is_zero(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path, params={"initial_capital": 10_000.0})
    _write_limits(root, {"max_drawdown": -0.05})
    _write_summary(root, {"net_pnl": -2000.0})
    # No pre-existing risk_state.json: peak seeds at current equity (8000).

    result = await _tick(job, root, store)

    assert result["snapshot"]["status"] == "valid"
    saved = json.loads((root / "state" / "risk_state.json").read_text())
    assert saved["peak_equity"] == 8000.0


async def test_peak_equity_persists_across_ticks(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path, params={"initial_capital": 10_000.0})
    _write_limits(root, {"max_drawdown": -0.04})
    _write_summary(root, {"net_pnl": 0.0})

    first = await _tick(job, root, store, view_count=1)
    assert first["snapshot"]["status"] == "valid"
    assert (
        json.loads((root / "state" / "risk_state.json").read_text())["peak_equity"]
        == 10_000.0
    )

    _write_summary(root, {"net_pnl": -500.0})  # dd -0.05 vs persisted peak
    second = await _tick(job, root, store, view_count=2)
    assert second["snapshot"]["status"] == "risk_halt"
    assert "max_drawdown" in second["snapshot"]["reason"]


def test_venue_peak_persists_across_ticks_and_drawdown_fires(tmp_path: Path) -> None:
    """Live regression: the venue peak must survive between ticks (with its
    source stamp) so drawdown ACCUMULATES — before the fix, the unstamped
    peak was discarded every tick and the live drawdown halt was dead."""
    root = tmp_path / "job"
    _write_limits(root, {"max_drawdown": -0.1})

    first_reason, first = check_risk_halt(
        root,
        state=EngineState(),
        view=_view(1),
        params={},
        now=pd.Timestamp("2026-01-01T00:00:00Z"),
        account_equity=10_000.0,
    )
    assert first_reason is None
    assert first["drawdown"] == 0.0
    saved = json.loads((root / "state" / "risk_state.json").read_text())
    assert saved["peak_equity"] == 10_000.0
    assert saved["equity_source"] == "venue"

    second_reason, second = check_risk_halt(
        root,
        state=EngineState(),
        view=_view(2),
        params={},
        now=pd.Timestamp("2026-01-01T00:05:00Z"),
        account_equity=8_000.0,
    )
    assert second_reason is not None and "max_drawdown" in second_reason
    assert second["peak_equity"] == 10_000.0
    assert second["drawdown"] == pytest.approx(-0.2)


def test_modelled_blip_neither_halts_nor_reseeds_venue_peak(tmp_path: Path) -> None:
    """An ambiguous venue fetch falls back to modelled equity for one tick.
    Comparing modelled equity to the venue peak must not latch a halt, and
    the blip must not reseed (or drop) the venue peak."""
    root = tmp_path / "job"
    _write_limits(root, {"max_drawdown": -0.1})
    check_risk_halt(
        root,
        state=EngineState(),
        view=_view(1),
        params={},
        now=pd.Timestamp("2026-01-01T00:00:00Z"),
        account_equity=10_000.0,
    )

    blip_reason, blip = check_risk_halt(
        root,
        state=EngineState(),
        view=_view(2),
        params={"initial_capital": 100.0},  # modelled equity 100 vs venue peak 10k
        now=pd.Timestamp("2026-01-01T00:05:00Z"),
        account_equity=None,
    )
    assert blip_reason is None
    assert blip["equity_source"] == "modelled"
    assert blip["drawdown"] == 0.0
    saved = json.loads((root / "state" / "risk_state.json").read_text())
    assert saved["peak_equity"] == 10_000.0
    assert saved["equity_source"] == "venue"

    # Venue equity returns: drawdown resumes against the carried venue peak.
    third_reason, third = check_risk_halt(
        root,
        state=EngineState(),
        view=_view(3),
        params={},
        now=pd.Timestamp("2026-01-01T00:10:00Z"),
        account_equity=8_500.0,
    )
    assert third_reason is not None and "max_drawdown" in third_reason
    assert third["peak_equity"] == 10_000.0
    assert third["drawdown"] == pytest.approx(-0.15)


def test_venue_equity_does_not_reuse_modelled_peak(tmp_path: Path) -> None:
    root = tmp_path / "job"
    _write_peak(root, 10_000.0)

    snapshot = build_risk_snapshot(
        state=EngineState(),
        view=_view(1),
        params={"initial_capital": 10_000.0},
        root=root,
        now=_now(_view(1)),
        account_equity=750.0,
    )

    assert snapshot["equity_source"] == "venue"
    assert snapshot["peak_equity"] == 750.0
    assert snapshot["drawdown"] == 0.0


async def test_max_daily_loss_counts_only_today(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path, params={"initial_capital": 10_000.0})
    _write_limits(root, {"max_daily_loss_usd": 500.0})
    trades_path = root / "results" / "forward" / "trades.jsonl"
    trades_path.parent.mkdir(parents=True, exist_ok=True)

    # Fixture `now` is 2026-01-01T00:05Z. Yesterday's loss must not count.
    trades_path.write_text(
        json.dumps({"net_pnl": -600.0, "closed_at": "2025-12-31T23:00:00+00:00"})
        + "\n",
        encoding="utf-8",
    )
    ok = await _tick(job, root, store, view_count=1)
    assert ok["snapshot"]["status"] == "valid"

    trades_path.write_text(
        json.dumps({"net_pnl": -600.0, "closed_at": "2026-01-01T00:00:00+00:00"})
        + "\n",
        encoding="utf-8",
    )
    halted = await _tick(job, root, store, view_count=2)
    assert halted["snapshot"]["status"] == "risk_halt"
    assert "max_daily_loss_usd" in halted["snapshot"]["reason"]


async def test_consecutive_losses_halt(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    _write_limits(root, {"pause_after_consecutive_losses": 3})
    _write_summary(root, {"net_pnl": -30.0, "current_loss_streak": 3})

    result = await _tick(job, root, store)
    assert result["snapshot"]["status"] == "risk_halt"
    assert "pause_after_consecutive_losses" in result["snapshot"]["reason"]


async def test_exposure_limits_use_marked_ledger(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path, params={"initial_capital": 10_000.0})
    _seed_position(root, size=100.0, price=9.0)  # marked at close 11.0 -> 1100
    _write_limits(root, {"max_gross_exposure_usd": 1000.0})

    result = await _tick(job, root, store)
    assert result["snapshot"]["status"] == "risk_halt"
    assert "max_gross_exposure_usd" in result["snapshot"]["reason"]


async def test_per_symbol_position_limit(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path, params={"initial_capital": 10_000.0})
    _seed_position(root, size=100.0, price=9.0)
    _write_limits(root, {"max_position_per_symbol_usd": 1000.0})

    result = await _tick(job, root, store)
    assert result["snapshot"]["status"] == "risk_halt"
    assert "max_position_per_symbol_usd" in result["snapshot"]["reason"]
    assert "SNX" in result["snapshot"]["reason"]


def test_check_risk_halt_without_file_is_inert(tmp_path: Path) -> None:
    reason, snapshot = check_risk_halt(
        tmp_path,
        state=EngineState(),
        view=_view(2),
        params={},
        now=pd.Timestamp("2026-01-01T00:05:00Z"),
    )
    assert reason is None
    assert snapshot == {}


def test_build_risk_snapshot_marks_unrealized_at_latest_close(
    tmp_path: Path,
) -> None:
    state = EngineState(ledger=PositionLedger())
    state.ledger.apply_fill(
        FillEvent(
            status="filled",
            venue="hyperliquid",
            symbol="SNX",
            side="long",
            filled_size=2.0,
            avg_price=10.0,
        )
    )
    view = _view(2)  # latest close 11.0
    snapshot = build_risk_snapshot(
        state=state,
        view=view,
        params={"initial_capital": 1000.0},
        root=tmp_path,
        now=pd.Timestamp("2026-01-01T00:05:00Z"),
    )
    assert snapshot["equity"] == 1000.0 + 2.0 * (11.0 - 10.0)
    assert snapshot["gross_exposure_usd"] == 22.0
    assert snapshot["positions_usd"] == {"SNX": 22.0}
    assert snapshot["drawdown"] == 0.0  # first observation seeds peak


def test_live_account_equity_overrides_configured_capital(tmp_path: Path) -> None:
    snapshot = build_risk_snapshot(
        state=EngineState(),
        view=_view(2),
        params={"initial_capital": 10_000.0},
        root=tmp_path,
        now=pd.Timestamp("2026-01-01T00:05:00Z"),
        account_equity=750.0,
    )
    assert snapshot["equity"] == 750.0
    assert snapshot["peak_equity"] == 750.0


async def test_governance_clamps_looser_agent_risk_limits(tmp_path: Path) -> None:
    """Agent risk_limits.json may be stricter than the owner's governance
    ceilings, never looser: effective limit is the stricter of the two."""
    store, job, root = _make_job(tmp_path, params={"initial_capital": 10_000.0})
    _write_limits(root, {"max_drawdown": -0.5})  # agent loosened to -50%
    _write_governance(tmp_path, job.id, {"max_drawdown": 0.1})
    _write_summary(root, {"net_pnl": -2000.0})
    _write_peak(root, 10_000.0)  # dd -0.2: inside agent's -0.5, past owner's -0.1

    result = await _tick(job, root, store)

    assert result["snapshot"]["status"] == "risk_halt"
    assert "max_drawdown" in result["snapshot"]["reason"]


async def test_agent_risk_limits_stricter_than_governance_kept(
    tmp_path: Path,
) -> None:
    store, job, root = _make_job(tmp_path, params={"initial_capital": 10_000.0})
    _write_limits(root, {"max_drawdown": -0.05})
    _write_governance(tmp_path, job.id, {"max_drawdown": 0.5})
    _write_summary(root, {"net_pnl": -1000.0})
    _write_peak(root, 10_000.0)  # dd -0.1: past the agent's stricter -0.05

    result = await _tick(job, root, store)

    assert result["snapshot"]["status"] == "risk_halt"
    assert "max_drawdown" in result["snapshot"]["reason"]


async def test_governance_caps_enforce_without_agent_limits_file(
    tmp_path: Path,
) -> None:
    """Deleting workspace/risk_limits.json must not lift the owner ceiling."""
    store, job, root = _make_job(tmp_path, params={"initial_capital": 10_000.0})
    _write_governance(tmp_path, job.id, {"max_drawdown": 0.1})
    _write_summary(root, {"net_pnl": -2000.0})
    _write_peak(root, 10_000.0)

    result = await _tick(job, root, store)

    assert result["snapshot"]["status"] == "risk_halt"
    assert "max_drawdown" in result["snapshot"]["reason"]


async def test_governance_max_leverage_clamps_engine_knob_and_journals(
    tmp_path: Path,
) -> None:
    store, job, root = _make_job(tmp_path, params={"leverage": 4.0})
    _write_governance(tmp_path, job.id, {"max_leverage": 2})

    result = await _tick(job, root, store, view_count=1)

    clamps = [e for e in result["guard_events"] if e["kind"] == "leverage_clamped"]
    assert clamps == [
        {
            "kind": "leverage_clamped",
            "requested": 4.0,
            "max_leverage": 2.0,
            "effective": 2.0,
        }
    ]
    # The engine seam applied the GOVERNED value: OPEN sized x2, not x4.
    assert result["intents"][0]["size"] == 2.0
    journal = (root / "journal.jsonl").read_text(encoding="utf-8")
    assert "leverage_clamped" in journal


async def test_leverage_within_governance_ceiling_untouched(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path, params={"leverage": 2.0})
    _write_governance(tmp_path, job.id, {"max_leverage": 3})

    result = await _tick(job, root, store, view_count=1)

    assert not any(e["kind"] == "leverage_clamped" for e in result["guard_events"])
    assert result["intents"][0]["size"] == 2.0
