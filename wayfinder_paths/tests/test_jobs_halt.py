"""Manual kill switch: instant reduce-only, pending-intent cancel, flatten.

The halt flag outranks every snapshot status, survives runner resume cycles,
and is deliberately independent of the promotion gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wayfinder_paths.jobs.execution import FillEvent, PositionLedger
from wayfinder_paths.jobs.execution.driver import tick_job
from wayfinder_paths.jobs.execution.engine import EngineState
from wayfinder_paths.jobs.execution.paper import PaperBroker
from wayfinder_paths.jobs.halt import clear_halt, read_halt, request_halt
from wayfinder_paths.tests.test_jobs_live_driver import (
    PERP_CAPS,
    FakeAdapter,
    _make_job,
    _now,
    _view,
)


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


def _seed_position(root: Path, *, size: float = 2.0, price: float = 9.0) -> None:
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


async def test_halt_forces_reduce_only(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    request_halt(store, job.id, reason="testing stop")

    result = await _tick(job, root, store)

    assert result["snapshot"]["status"] == "risk_halt"
    assert "manual halt: testing stop" in result["snapshot"]["reason"]
    assert result["intents"] == []  # strategy's OPEN blocked
    assert any(
        e["kind"] == "intent_rejected" and "risk_halt" in e["reason"]
        for e in result["guard_events"]
    )
    assert any(e["kind"] == "manual_halt" for e in result["guard_events"])


async def test_halt_outranks_ambiguous_and_cancels_pending_opens(
    tmp_path: Path,
) -> None:
    store, job, root = _make_job(tmp_path)
    # Tick 1 (no halt): strategy queues an OPEN that would fill at bar 2 open.
    first = await _tick(job, root, store, view_count=1)
    assert first["intents"], "OPEN queued"

    request_halt(store, job.id, reason="stop before the fill")
    second = await _tick(job, root, store, view_count=2)

    assert second["snapshot"]["status"] == "risk_halt"
    assert second["fills"] == [], "queued OPEN must not settle under halt"
    assert any(
        e["kind"] == "pending_intent_canceled_by_halt" for e in second["guard_events"]
    )


async def test_flatten_closes_positions_and_journals(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    _seed_position(root, size=2.0, price=9.0)
    request_halt(store, job.id, reason="flatten test", flatten=True)

    result = await _tick(job, root, store)

    closes = [
        fill
        for fill in result["fills"]
        if fill["reduce_only"] and fill["status"] == "filled"
    ]
    assert len(closes) == 1
    assert closes[0]["symbol"] == "SNX"
    assert closes[0]["filled_size"] == 2.0
    restored = EngineState.load(root / "state" / "engine_state.json")
    assert restored.ledger.positions == {}
    journal = (root / "journal.jsonl").read_text(encoding="utf-8")
    assert "halt_flattened" in journal

    # Next tick: nothing left to flatten, still reduce-only, no new fills.
    again = await _tick(job, root, store, view_count=3)
    assert again["fills"] == []
    assert again["snapshot"]["status"] == "risk_halt"


async def test_resume_from_halt_restores_trading(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    request_halt(store, job.id, reason="pause for review")
    halted = await _tick(job, root, store, view_count=1)
    assert halted["snapshot"]["status"] == "risk_halt"

    clear_halt(store, job.id, by="owner")
    resumed = await _tick(job, root, store, view_count=2)

    assert resumed["snapshot"]["status"] == "valid"
    assert resumed["intents"], "strategy trades again after resume-from-halt"


def test_halt_lifecycle_scorecard_and_idempotence(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    store.refresh_scorecard(job.id, {"live_execution_status": "ok"})

    first = request_halt(store, job.id, reason="one")
    assert read_halt(root)["reason"] == "one"
    scorecard = store.read_json(job.id, "scorecard.json", default={})
    assert scorecard["live_execution_status"] == "halted"

    # Idempotent: second halt keeps the original timestamp/reason; upgrading
    # to flatten is allowed and journaled once.
    second = request_halt(store, job.id, flatten=True)
    assert second["ts"] == first["ts"]
    assert second["reason"] == "one"
    assert second["flatten"] is True
    journal = (root / "journal.jsonl").read_text(encoding="utf-8")
    assert journal.count("halt_requested") == 1
    assert journal.count("halt_flatten_requested") == 1

    cleared = clear_halt(store, job.id, by="owner")
    assert cleared["cleared"] is True
    assert read_halt(root) is None
    scorecard = store.read_json(job.id, "scorecard.json", default={})
    assert scorecard["live_execution_status"] == "ok"  # prior status restored

    # Clearing again is a no-op.
    assert clear_halt(store, job.id, by="owner")["cleared"] is False


def test_risk_latched_halt_requires_owner_clear(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    request_halt(store, job.id, reason="drawdown breach", source="risk_limits")

    with pytest.raises(PermissionError):
        clear_halt(store, job.id, by="agent")
    assert read_halt(root) is not None, "refused clear must leave the halt latched"
    journal = (root / "journal.jsonl").read_text(encoding="utf-8")
    assert "halt_clear_refused" in journal

    cleared = clear_halt(store, job.id, by="owner")
    assert cleared["cleared"] is True
    assert read_halt(root) is None


def test_protection_latched_halt_requires_owner_clear(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    request_halt(store, job.id, reason="stop missing", source="native_protection")

    with pytest.raises(PermissionError):
        clear_halt(store, job.id, by="agent")
    assert clear_halt(store, job.id, by="owner")["cleared"] is True


def test_manual_halt_stays_agent_clearable(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    request_halt(store, job.id, reason="routine pause")  # source="manual"

    assert clear_halt(store, job.id, by="agent")["cleared"] is True
    assert read_halt(root) is None


def test_unreadable_halt_file_fails_safe(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    path = root / "state" / "halt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    halt = read_halt(root)
    assert halt is not None
    assert halt["flatten"] is False


def test_halt_snapshot_key_in_sync(tmp_path: Path) -> None:
    from wayfinder_paths.jobs.sync import snapshot_job

    store, job, root = _make_job(tmp_path)
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    request_halt(store, job.id, reason="sync me")

    snapshot = snapshot_job(job.id, store=store)
    assert snapshot["halt"]["reason"] == "sync me"
    assert snapshot["scorecard"]["live_execution_status"] == "halted"

    clear_halt(store, job.id, by="owner")
    assert snapshot_job(job.id, store=store)["halt"] is None


def test_clearing_loss_streak_halt_resets_the_streak(tmp_path: Path) -> None:
    """Without the reset, clearing is a treadmill: the next tick re-reads the
    stale streak, re-latches before any trade runs, and the only organic
    reset (a winning close) can never happen under reduce-only."""
    from wayfinder_paths.jobs.models import DEFAULT_FORWARD_SUMMARY

    store, job, root = _make_job(tmp_path)
    store.write_json(
        job.id,
        DEFAULT_FORWARD_SUMMARY,
        {"trades": {"current_loss_streak": 7, "losses": 7, "wins": 0}},
    )
    request_halt(
        store,
        job.id,
        reason="pause_after_consecutive_losses breached: 7 >= 5",
        source="risk_limits",
    )

    cleared = clear_halt(store, job.id, by="owner")
    assert cleared["cleared"] is True
    summary = store.read_json(job.id, DEFAULT_FORWARD_SUMMARY, default={})
    assert summary["trades"]["current_loss_streak"] == 0
    # Other tallies are history, not state — they stay.
    assert summary["trades"]["losses"] == 7
    journal = (root / "journal.jsonl").read_text(encoding="utf-8")
    assert "loss_streak_reset" in journal


def test_clearing_other_halts_leaves_streak_alone(tmp_path: Path) -> None:
    from wayfinder_paths.jobs.models import DEFAULT_FORWARD_SUMMARY

    store, job, root = _make_job(tmp_path)
    store.write_json(
        job.id,
        DEFAULT_FORWARD_SUMMARY,
        {"trades": {"current_loss_streak": 3}},
    )
    request_halt(store, job.id, reason="manual", source="manual")
    clear_halt(store, job.id, by="owner")
    summary = store.read_json(job.id, DEFAULT_FORWARD_SUMMARY, default={})
    assert summary["trades"]["current_loss_streak"] == 3
