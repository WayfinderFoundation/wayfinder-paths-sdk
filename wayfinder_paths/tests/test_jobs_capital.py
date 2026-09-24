"""Owner capital flows on a running job: the ledger, capital over time,
deposits that add to real capital, withdrawals that run or queue by free
margin, the risk-peak rebase, pending-withdrawal sizing and settlement, and
the views/payloads that read the ledger."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from click.testing import CliRunner

import wayfinder_paths.mcp.tools.hyperliquid as hl
from wayfinder_paths.jobs import cli as cli_module
from wayfinder_paths.jobs import sync as sync_module
from wayfinder_paths.jobs.capital import (
    CAPITAL_FLOWS_PATH,
    PENDING_WITHDRAWAL_PATH,
    STALE_TRANSFER_S,
    apply_flows_to_risk_peak,
    capital_at,
    capital_flows,
    capital_lock,
    capital_risk_step,
    capital_summary,
    capital_timeline,
    capital_transfer,
    capital_transfer_status,
    pending_withdrawal,
    record_capital_flow,
    set_pending_withdrawal,
)
from wayfinder_paths.jobs.execution.driver import tick_job
from wayfinder_paths.jobs.execution.engine import EngineState
from wayfinder_paths.jobs.execution.primitives import (
    ExecutionContext,
    ExecutionSpec,
    StateSnapshot,
    mark_to_market_equity,
)
from wayfinder_paths.jobs.execution.risk import check_risk_halt
from wayfinder_paths.jobs.forward_artifacts import _pnl_series
from wayfinder_paths.jobs.halt import read_halt
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.regime_health import _performance_windows
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.tests.test_jobs_live_driver import (
    FakeAdapter,
    FakeLiveBroker,
    _equity_job,
    _make_job,
    _now,
    _view,
)

WALLET = "cap-wallet"

NOOP_STRATEGY = """
class Strategy:
    def __init__(self, params):
        self.params = params

    def decide(self, ctx):
        return []


def build_strategy(params):
    return Strategy(params)
""".lstrip()


class FakeVenue:
    """The one venue seam: hyperliquid_get_state + deposit + submit."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, equity: float, free: float
    ) -> None:
        self.equity = equity
        self.free = free
        self.withdraw_status = "submitted"
        self.withdrawals: list[dict[str, Any]] = []
        self.deposits: list[dict[str, Any]] = []
        monkeypatch.setattr(hl, "hyperliquid_get_state", self.get_state)
        monkeypatch.setattr(hl, "hyperliquid_deposit_usdc", self.deposit)
        monkeypatch.setattr(hl, "submit_usdc_withdrawal", self.withdraw)

    async def get_state(self, *, label: str) -> dict[str, Any]:
        return {
            "ok": True,
            "result": {
                "summary": {
                    "unified_usdc_equity": self.equity,
                    "unified_usdc_margin_available": self.free,
                }
            },
        }

    async def deposit(self, *, wallet_label: str, amount_usdc: float) -> dict:
        self.deposits.append({"wallet_label": wallet_label, "amount": amount_usdc})
        return {
            "ok": True,
            "result": {
                "status": "confirmed",
                "effects": [{"result": {"txn_hash": "0xdep"}}],
            },
        }

    async def withdraw(
        self, *, wallet_label: str, amount_usdc: float, destination: str | None
    ) -> dict:
        self.withdrawals.append(
            {
                "wallet_label": wallet_label,
                "amount": amount_usdc,
                "destination": destination,
            }
        )
        return {"ok": True, "result": {"status": self.withdraw_status}}


def _funded_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    capital: float = 100.0,
    mode: str = "live",
) -> tuple[JobStore, str]:
    """majors-5m-lab's shape: live, capital 100, a seeded equity reconciler,
    no state/funding.json (funded before the venue funding flow existed)."""
    monkeypatch.setattr(sync_module, "sync_all_jobs", lambda **kwargs: None)
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("cap-demo", agent_mode="intervene")
    job.script_loop.mode = mode
    job.execution_params["initial_capital"] = capital
    job.execution_params["wallet_label"] = WALLET
    store.save(job)
    store.write_json(
        job.id,
        "state/equity_recon.json",
        {"venue_equity_start": capital, "ledger_realized_at_seed": 0.0},
    )
    return store, job.id


def _journal_types(store: JobStore, job_id: str) -> list[str]:
    return [row["type"] for row in store.read_jsonl(job_id, "journal.jsonl")]


def _ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


# ---------------------------------------------------------------- ledger


def test_capital_at_keeps_pre_ledger_history_and_steps_at_each_flow(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch, capital=100.0)
    # No flows: all of history keeps today's capital (pre-ledger behaviour).
    assert capital_at(store, job_id, "2026-01-01T00:00:00+00:00") == 100.0

    deposit = record_capital_flow(
        store, job_id, "deposit", 50.0, capital_delta=50.0, by="owner"
    )
    job = store.load(job_id)
    job.execution_params["initial_capital"] = 150.0
    store.save(job)
    withdrawal = record_capital_flow(
        store, job_id, "withdrawal", 30.0, capital_delta=-30.0, by="owner"
    )
    job.execution_params["initial_capital"] = 120.0
    store.save(job)

    timeline = capital_timeline(store, job_id)
    before = _ts(deposit["ts"]) - dt.timedelta(seconds=1)
    between = _ts(withdrawal["ts"]) - dt.timedelta(microseconds=1)
    assert timeline.at(before) == 100.0
    assert timeline.at(deposit["ts"]) == 150.0
    assert timeline.at(between) == 150.0
    assert timeline.at(withdrawal["ts"]) == 120.0
    rows = capital_flows(store, job_id)
    assert [row["kind"] for row in rows] == ["deposit", "withdrawal"]
    assert all(row["equity_applied"] is False for row in rows)


def test_deposit_adds_to_capital_on_a_job_with_live_history(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch, capital=100.0)
    venue = FakeVenue(monkeypatch, equity=90.0, free=90.0)

    result = asyncio.run(sync_module.venue_deposit(job_id, 10.0, store=store))

    assert venue.deposits == [{"wallet_label": WALLET, "amount": 10.0}]
    assert result["initial_capital"] == 110.0
    assert store.load(job_id).execution_params["initial_capital"] == 110.0
    (flow,) = capital_flows(store, job_id)
    assert flow["kind"] == "deposit"
    assert flow["amount"] == 10.0
    assert flow["capital_delta"] == 10.0
    assert flow["tx"] == "0xdep"
    assert flow["by"] == "owner"
    assert store.read_json(job_id, "state/equity_recon.json")[
        "venue_equity_start"
    ] == pytest.approx(110.0)
    assert "deposit_executed" in _journal_types(store, job_id)


def test_first_deposit_replaces_the_placeholder_on_a_never_traded_job(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(sync_module, "sync_all_jobs", lambda **kwargs: None)
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("fresh", agent_mode="intervene")
    job.execution_params["initial_capital"] = 10_000.0
    job.execution_params["wallet_label"] = WALLET
    store.save(job)
    FakeVenue(monkeypatch, equity=0.0, free=0.0)

    first = asyncio.run(sync_module.venue_deposit(job.id, 25.0, store=store))
    second = asyncio.run(sync_module.venue_deposit(job.id, 10.0, store=store))

    assert first["initial_capital"] == 25.0
    assert second["initial_capital"] == 35.0
    # The replaced placeholder stays the capital of the paper history before.
    first_flow = capital_flows(store, job.id)[0]
    assert first_flow["capital_delta"] == 25.0 - 10_000.0
    early = _ts(first_flow["ts"]) - dt.timedelta(seconds=1)
    assert capital_at(store, job.id, early) == 10_000.0


def test_withdraw_executes_when_free_margin_covers_it(tmp_path, monkeypatch) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch, capital=100.0)
    venue = FakeVenue(monkeypatch, equity=100.0, free=100.0)

    result = asyncio.run(
        sync_module.venue_withdraw(job_id, 40.0, destination="0xowner", store=store)
    )

    assert venue.withdrawals == [
        {"wallet_label": WALLET, "amount": 40.0, "destination": "0xowner"}
    ]
    assert result["pending"] is False
    assert result["withdraw_status"] == "submitted"
    assert result["initial_capital"] == 60.0
    (flow,) = capital_flows(store, job_id)
    assert flow["kind"] == "withdrawal"
    assert flow["destination"] == "0xowner"
    assert flow["capital_delta"] == -40.0
    assert pending_withdrawal(store, job_id) is None
    assert "withdrawal_executed" in _journal_types(store, job_id)


def test_withdraw_above_free_margin_queues_and_cancel_clears(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch, capital=100.0)
    venue = FakeVenue(monkeypatch, equity=100.0, free=20.0)

    result = asyncio.run(sync_module.venue_withdraw(job_id, 40.0, store=store))

    assert venue.withdrawals == []
    assert result["pending"] is True
    assert result["amount"] == 40.0
    # Free margin less the $1 Bridge2 fee buffer.
    assert result["withdrawable_now"] == 19.0
    assert pending_withdrawal(store, job_id)["amount"] == 40.0
    assert store.load(job_id).execution_params["initial_capital"] == 100.0
    assert capital_flows(store, job_id) == []
    with pytest.raises(ValueError, match="already pending"):
        asyncio.run(sync_module.venue_withdraw(job_id, 5.0, store=store))

    cancelled = sync_module.cancel_venue_withdrawal(job_id, store=store)
    assert cancelled["cancelled"] is True
    assert cancelled["pending_withdrawal"]["amount"] == 40.0
    assert pending_withdrawal(store, job_id) is None
    assert sync_module.cancel_venue_withdrawal(job_id, store=store)["cancelled"] is (
        False
    )
    types = _journal_types(store, job_id)
    assert "withdrawal_pending" in types and "withdrawal_cancelled" in types


def test_withdraw_rejects_more_than_venue_equity(tmp_path, monkeypatch) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch, capital=100.0)
    FakeVenue(monkeypatch, equity=50.0, free=10.0)
    with pytest.raises(ValueError, match="exceeds venue equity"):
        asyncio.run(sync_module.venue_withdraw(job_id, 60.0, store=store))
    assert pending_withdrawal(store, job_id) is None


def test_cli_venue_withdraw_executed_pending_and_cancel_json(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch, capital=100.0)
    venue = FakeVenue(monkeypatch, equity=100.0, free=100.0)
    monkeypatch.setattr(sync_module, "JobStore", lambda: store)
    runner = CliRunner()

    executed = runner.invoke(cli_module.job_cli, ["venue-withdraw", job_id, "10"])
    assert executed.exit_code == 0, executed.output
    payload = json.loads(executed.output)
    assert payload["ok"] is True
    assert payload["result"]["pending"] is False
    assert payload["result"]["flow"]["amount"] == 10.0

    venue.free = 5.0
    queued = runner.invoke(cli_module.job_cli, ["venue-withdraw", job_id, "30"])
    assert queued.exit_code == 0, queued.output
    payload = json.loads(queued.output)
    assert payload["result"]["pending"] is True
    assert payload["result"]["withdrawable_now"] == 4.0

    cancelled = runner.invoke(
        cli_module.job_cli, ["venue-withdraw", job_id, "--cancel"]
    )
    assert cancelled.exit_code == 0, cancelled.output
    payload = json.loads(cancelled.output)
    assert payload["result"]["cancelled"] is True

    missing = runner.invoke(cli_module.job_cli, ["venue-withdraw", job_id])
    assert missing.exit_code != 0


# ---------------------------------------------------------------- risk peak


def _seed_venue_peak(store: JobStore, job_id: str, peak: float, equity: float) -> None:
    store.write_json(
        job_id,
        "state/risk_state.json",
        {
            "peak_equity": peak,
            "equity": equity,
            "drawdown": equity / peak - 1.0,
            "equity_source": "venue",
        },
    )


@pytest.mark.parametrize(
    ("kind", "amount", "equity_now"),
    [("withdrawal", 40.0, 46.78), ("deposit", 50.0, 136.78)],
)
def test_risk_peak_rebase_keeps_drawdown_pct_and_applies_once(
    tmp_path, monkeypatch, kind, amount, equity_now
) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch)
    # majors-5m-lab on the primary: peak 95.84, equity 86.78 (-9.45%).
    _seed_venue_peak(store, job_id, 95.84, 86.78)
    drawdown_before = 86.78 / 95.84 - 1.0
    flow = record_capital_flow(
        store,
        job_id,
        kind,
        amount,
        capital_delta=amount if kind == "deposit" else -amount,
        by="owner",
    )
    observed = dt.datetime.now(dt.UTC)

    events = apply_flows_to_risk_peak(
        store, job_id, equity_now=equity_now, observed_at=observed
    )

    (event,) = events
    assert event["type"] == "risk_peak_rebased"
    assert event["flow"] == flow["id"]
    assert event["peak_before"] == 95.84
    risk_state = store.read_json(job_id, "state/risk_state.json")
    assert equity_now / risk_state["peak_equity"] - 1.0 == pytest.approx(
        drawdown_before
    )
    assert capital_flows(store, job_id)[0]["equity_applied"] is True
    assert "risk_peak_rebased" in _journal_types(store, job_id)
    # Applied once: a second tick leaves the peak alone.
    assert (
        apply_flows_to_risk_peak(
            store, job_id, equity_now=equity_now, observed_at=observed
        )
        == []
    )
    assert (
        store.read_json(job_id, "state/risk_state.json")["peak_equity"]
        == (risk_state["peak_equity"])
    )


def test_withdrawal_never_trips_the_drawdown_halt(tmp_path, monkeypatch) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch)
    root = store.job_dir(job_id)
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    (root / "workspace" / "risk_limits.json").write_text(
        json.dumps({"max_drawdown": -0.15})
    )
    _seed_venue_peak(store, job_id, 95.84, 86.78)
    record_capital_flow(
        store, job_id, "withdrawal", 40.0, capital_delta=-40.0, by="owner"
    )
    apply_flows_to_risk_peak(
        store, job_id, equity_now=46.78, observed_at=dt.datetime.now(dt.UTC)
    )

    reason, snapshot = check_risk_halt(
        root,
        state=EngineState(),
        view=_view(1),
        params={},
        now=pd.Timestamp("2026-09-23T00:00:00Z"),
        account_equity=46.78,
    )
    assert reason is None
    assert snapshot["drawdown"] == pytest.approx(86.78 / 95.84 - 1.0)


def test_flow_recorded_after_the_equity_fetch_waits_for_the_next_tick(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch)
    _seed_venue_peak(store, job_id, 100.0, 100.0)
    fetched = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5)
    record_capital_flow(
        store, job_id, "withdrawal", 50.0, capital_delta=-50.0, by="owner"
    )
    assert (
        apply_flows_to_risk_peak(store, job_id, equity_now=100.0, observed_at=fetched)
        == []
    )
    assert capital_flows(store, job_id)[0]["equity_applied"] is False


def test_flows_without_a_venue_peak_are_marked_applied_silently(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch)
    record_capital_flow(store, job_id, "deposit", 20.0, capital_delta=20.0, by="o")
    events = apply_flows_to_risk_peak(
        store, job_id, equity_now=120.0, observed_at=dt.datetime.now(dt.UTC)
    )
    assert events == []
    assert capital_flows(store, job_id)[0]["equity_applied"] is True
    assert store.read_json(job_id, "state/risk_state.json") is None


# ---------------------------------------------------------------- sizing


def _ctx_with(data: dict[str, Any]) -> ExecutionContext:
    return ExecutionContext(
        view=_view(2),
        ledger=EngineState().ledger,
        state_snapshot=StateSnapshot(data=data),
        capacity=None,
        params={"initial_capital": 100.0},
        timestamp="2026-01-01T00:05:00+00:00",
        execution_spec=ExecutionSpec(),
    )


def test_mark_to_market_equity_sizes_to_the_post_withdrawal_bankroll() -> None:
    assert mark_to_market_equity(_ctx_with({"account_value": 100.0})) == 100.0
    assert (
        mark_to_market_equity(
            _ctx_with({"account_value": 100.0, "pending_withdrawal_usd": 40.0})
        )
        == 60.0
    )
    # Withdrawing the whole account sizes to zero, never to config capital.
    assert (
        mark_to_market_equity(
            _ctx_with({"account_value": 30.0, "pending_withdrawal_usd": 40.0})
        )
        == 0.0
    )


# ---------------------------------------------------------------- live tick


async def test_live_tick_sizes_down_for_a_pending_withdrawal(
    tmp_path, monkeypatch
) -> None:
    store, job, root = _equity_job(
        tmp_path, params={"initial_capital": 100.0, "wallet_label": WALLET}
    )
    FakeVenue(monkeypatch, equity=100.0, free=10.0)
    set_pending_withdrawal(
        store, job.id, 40.0, destination=None, by="owner", withdrawable_now=9.0
    )
    view = _view(2)

    result = await tick_job(
        job,
        root,
        "live",
        store=store,
        adapters={
            "hyperliquid": FakeAdapter(view, FakeLiveBroker(account_value=100.0))
        },
        now=_now(view),
    )

    assert result["snapshot"]["data"]["pending_withdrawal_usd"] == 40.0
    assert float(result["intents"][0]["notional"]) == pytest.approx(30.0)
    assert result["withdrawal_settlement"]["kind"] == "withdrawal_waiting"
    assert pending_withdrawal(store, job.id)["amount"] == 40.0


def _noop_live_job(tmp_path: Path) -> tuple[JobStore, WayfinderJob, Path]:
    store, job, root = _make_job(
        tmp_path,
        mode="live",
        params={"initial_capital": 100.0, "wallet_label": WALLET},
    )
    (root / "workspace" / "src" / "strategy.py").write_text(NOOP_STRATEGY)
    (root / "workspace" / "risk_limits.json").write_text(
        json.dumps({"max_drawdown": -0.15})
    )
    return store, job, root


async def _live_tick(
    store: JobStore, job: WayfinderJob, root: Path, *, account_value: float, bars: int
) -> dict[str, Any]:
    view = _view(bars)
    return await tick_job(
        job,
        root,
        "live",
        store=store,
        adapters={
            "hyperliquid": FakeAdapter(
                view, FakeLiveBroker(account_value=account_value)
            )
        },
        now=_now(view),
    )


async def test_driver_settles_the_pending_withdrawal_once_margin_frees(
    tmp_path, monkeypatch
) -> None:
    store, job, root = _noop_live_job(tmp_path)
    venue = FakeVenue(monkeypatch, equity=100.0, free=10.0)
    set_pending_withdrawal(
        store, job.id, 40.0, destination="0xowner", by="owner", withdrawable_now=9.0
    )

    first = await _live_tick(store, job, root, account_value=100.0, bars=2)
    assert first["withdrawal_settlement"]["kind"] == "withdrawal_waiting"
    assert venue.withdrawals == []

    venue.free = 100.0
    second = await _live_tick(store, job, root, account_value=100.0, bars=3)
    assert second["withdrawal_settlement"]["kind"] == "withdrawal_executed"
    assert venue.withdrawals == [
        {"wallet_label": WALLET, "amount": 40.0, "destination": "0xowner"}
    ]
    assert pending_withdrawal(store, job.id) is None
    (flow,) = capital_flows(store, job.id)
    assert flow["deferred"] is True and flow["destination"] == "0xowner"
    assert store.load(job.id).execution_params["initial_capital"] == 60.0

    # The venue now reports the lower balance: the peak follows the flow, so
    # a 40% withdrawal is not a 40% drawdown against a 15% halt.
    third = await _live_tick(store, job, root, account_value=60.0, bars=4)
    assert third["snapshot"]["status"] == "valid", third["snapshot"]
    assert read_halt(root) is None
    risk_state = store.read_json(job.id, "state/risk_state.json")
    assert risk_state["peak_equity"] == pytest.approx(60.0)
    types = _journal_types(store, job.id)
    assert "withdrawal_executed" in types and "risk_peak_rebased" in types
    assert "risk_halt" not in types


async def test_failed_settlement_stays_pending_and_never_halts(
    tmp_path, monkeypatch
) -> None:
    store, job, root = _noop_live_job(tmp_path)
    venue = FakeVenue(monkeypatch, equity=100.0, free=100.0)
    venue.withdraw_status = "failed"
    set_pending_withdrawal(
        store, job.id, 40.0, destination=None, by="owner", withdrawable_now=9.0
    )

    result = await _live_tick(store, job, root, account_value=100.0, bars=2)

    assert result["ok"] is True
    assert result["withdrawal_settlement"]["kind"] == "withdrawal_failed"
    pending = pending_withdrawal(store, job.id)
    assert pending is not None and not pending.get("attempt_started_at")
    assert capital_flows(store, job.id) == []
    assert read_halt(root) is None
    assert "withdrawal_failed" in _journal_types(store, job.id)

    # A retry on the next tick succeeds.
    venue.withdraw_status = "submitted"
    retried = await _live_tick(store, job, root, account_value=100.0, bars=3)
    assert retried["withdrawal_settlement"]["kind"] == "withdrawal_executed"


async def test_interrupted_settlement_is_never_retried_blind(
    tmp_path, monkeypatch
) -> None:
    store, job, root = _noop_live_job(tmp_path)
    venue = FakeVenue(monkeypatch, equity=100.0, free=100.0)
    set_pending_withdrawal(
        store, job.id, 40.0, destination=None, by="owner", withdrawable_now=9.0
    )
    pending = pending_withdrawal(store, job.id)
    store.write_json(
        job.id,
        PENDING_WITHDRAWAL_PATH,
        {**pending, "attempt_started_at": "2026-09-23T00:00:00+00:00"},
    )

    result = await _live_tick(store, job, root, account_value=100.0, bars=2)

    assert result["withdrawal_settlement"]["kind"] == "withdrawal_outcome_unknown"
    assert venue.withdrawals == []
    assert pending_withdrawal(store, job.id)["attempt_status"] == "unknown"


# ---------------------------------------------------------------- views


def test_forward_series_steps_at_a_flow_instead_of_rebasing(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch, capital=100.0)
    flow = record_capital_flow(
        store, job_id, "deposit", 50.0, capital_delta=50.0, by="owner"
    )
    job = store.load(job_id)
    job.execution_params["initial_capital"] = 150.0
    store.save(job)
    before = (_ts(flow["ts"]) - dt.timedelta(hours=1)).isoformat()
    after = (_ts(flow["ts"]) + dt.timedelta(hours=1)).isoformat()
    ticks = [
        {"bar_ts": before, "ledger": {"realized_pnl": 5.0}, "mode": "live"},
        {"bar_ts": after, "ledger": {"realized_pnl": 5.0}, "mode": "live"},
    ]

    points = _pnl_series(job_id, ticks, store=store)["points"]

    assert [point["value"] for point in points] == [105.0, 155.0]


def test_regime_drawdown_uses_capital_at_each_close() -> None:
    now = dt.datetime(2026, 9, 23, tzinfo=dt.UTC)
    flow_at = now - dt.timedelta(days=2)
    trades = [
        {"closed_at": (now - dt.timedelta(days=3)).isoformat(), "net_pnl": -10.0},
        {"closed_at": (now - dt.timedelta(days=1)).isoformat(), "net_pnl": -10.0},
    ]

    def capital_at_moment(moment: dt.datetime) -> float:
        # Half the account was withdrawn between the two losses.
        return 100.0 if moment < flow_at else 50.0

    windows = _performance_windows(
        trades, [], [], now=now, capital_at=capital_at_moment
    )

    week = windows["windows"]["7"]
    assert week["max_drawdown_usd"] == 20.0
    assert week["max_drawdown_pct"] == pytest.approx(0.1 + 0.2)
    assert windows["initial_capital_basis"] == 50.0


# ---------------------------------------------------------------- payloads


def test_capital_summary_and_sync_payload(tmp_path, monkeypatch) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch, capital=100.0)
    record_capital_flow(store, job_id, "deposit", 10.0, capital_delta=10.0, by="o")
    set_pending_withdrawal(
        store, job_id, 25.0, destination=None, by="owner", withdrawable_now=3.0
    )
    ticks = store.job_dir(job_id) / "results" / "forward" / "ticks.jsonl"
    ticks.parent.mkdir(parents=True, exist_ok=True)
    ticks.write_text(
        json.dumps({"mode": "live", "snapshot": {"data": {"account_value": 88.5}}})
        + "\n"
    )

    summary = capital_summary(store, job_id)
    assert summary["bankroll_usd"] == 88.5
    assert summary["funded_capital_usd"] == 100.0
    assert [flow["amount"] for flow in summary["flows"]] == [10.0]
    assert summary["pending_withdrawal"]["amount"] == 25.0

    snapshot = sync_module.snapshot_job(job_id, store=store)
    assert snapshot["wallet_label"] == WALLET
    assert snapshot["capital"]["pending_withdrawal"]["amount"] == 25.0
    assert snapshot["capital"]["flows"][0]["kind"] == "deposit"


def test_wake_payload_carries_capital_context_and_forbids_moving_funds(
    tmp_path, monkeypatch
) -> None:
    from wayfinder_paths.jobs.worker import prepare_job_worker_prompt

    store, job_id = _funded_job(tmp_path, monkeypatch, capital=100.0)
    record_capital_flow(store, job_id, "deposit", 10.0, capital_delta=10.0, by="o")

    prompt = prepare_job_worker_prompt(store=store, job_id=job_id, mode="intervene")[
        "prompt"
    ]

    assert '"capital"' in prompt
    assert '"funded_capital_usd"' in prompt
    assert "Deposits and withdrawals are owner actions" in prompt
    assert "venue_withdraw" not in prompt
    assert (store.job_dir(job_id) / CAPITAL_FLOWS_PATH).exists()


# ---------------------------------------------------------------- transfer lock


class HeldTransfer:
    """Holds the capital lock from another thread, like an owner CLI/relay
    process mid-transfer (flock is per open file, so a second thread in this
    process contends exactly like a second process)."""

    def __init__(self, store: JobStore, job_id: str, kind: str = "deposit") -> None:
        self.store = store
        self.job_id = job_id
        self.kind = kind
        self.entered = threading.Event()
        self.release = threading.Event()
        self.on_release: Any = None
        self.thread = threading.Thread(target=self._run)

    def _run(self) -> None:
        with capital_transfer(self.store, self.job_id, self.kind) as status:
            self.status = status
            self.entered.set()
            self.release.wait(10)
            if self.on_release is not None:
                self.on_release()

    def __enter__(self) -> HeldTransfer:
        self.thread.start()
        assert self.entered.wait(10)
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        self.thread.join(10)


def _journal_count(store: JobStore, job_id: str, kind: str) -> int:
    return _journal_types(store, job_id).count(kind)


async def test_tick_during_a_deposit_skips_the_peak_then_applies_it_once(
    tmp_path, monkeypatch
) -> None:
    """The majors case: peak 95.8, equity 86.8, $50 deposit. A tick that sees
    the credit before the flow exists must not lift the peak to 136.8 (the
    next rescale would make it ~215.6: a 36% "drawdown" and a halt)."""
    store, job, root = _noop_live_job(tmp_path)
    _seed_venue_peak(store, job.id, 95.8, 86.8)
    held = HeldTransfer(store, job.id)
    held.on_release = lambda: record_capital_flow(
        store,
        job.id,
        "deposit",
        50.0,
        capital_delta=50.0,
        by="owner",
        equity_before=86.8,
    )

    with held:
        first = await _live_tick(store, job, root, account_value=136.8, bars=2)
        second = await _live_tick(store, job, root, account_value=136.8, bars=3)
        kinds = [event["kind"] for event in first["guard_events"]]
        assert "risk_check_deferred_capital_transfer" in kinds
        assert store.read_json(job.id, "state/risk_state.json")["peak_equity"] == (95.8)
        assert second["snapshot"]["status"] == "valid"
    assert _journal_count(store, job.id, "risk_check_deferred_capital_transfer") == 1

    third = await _live_tick(store, job, root, account_value=136.8, bars=4)

    assert third["snapshot"]["status"] == "valid", third["snapshot"]
    assert read_halt(root) is None
    risk_state = store.read_json(job.id, "state/risk_state.json")
    assert risk_state["peak_equity"] == pytest.approx(95.8 * 136.8 / 86.8)
    assert risk_state["drawdown"] == pytest.approx(86.8 / 95.8 - 1.0)
    assert _journal_count(store, job.id, "risk_peak_rebased") == 1
    fourth = await _live_tick(store, job, root, account_value=136.8, bars=5)
    assert fourth["snapshot"]["status"] == "valid"
    assert _journal_count(store, job.id, "risk_peak_rebased") == 1


def test_a_transfer_that_finished_after_the_equity_fetch_still_defers(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch)
    observed = capital_transfer_status(store, job_id)
    with capital_transfer(store, job_id, "withdrawal") as status:
        pass
    with capital_risk_step(store, job_id, observed=observed) as deferred_by:
        assert deferred_by == status["id"]
    later = capital_transfer_status(store, job_id)
    with capital_risk_step(store, job_id, observed=later) as deferred_by:
        assert deferred_by is None


def test_transfer_lock_is_released_when_the_transfer_raises(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch)
    with (
        pytest.raises(ValueError, match="bridge down"),
        capital_transfer(store, job_id, "deposit"),
    ):
        raise ValueError("bridge down")
    status = capital_transfer_status(store, job_id)
    assert status["finished_at"]
    acquired: list[bool] = []

    def contend() -> None:
        with capital_lock(store, job_id, timeout_s=0):
            acquired.append(True)

    thread = threading.Thread(target=contend)
    thread.start()
    thread.join(10)
    assert acquired == [True]


def test_hung_transfer_goes_stale_and_risk_checks_resume(tmp_path, monkeypatch) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch)
    with HeldTransfer(store, job_id) as held:
        observed = capital_transfer_status(store, job_id)
        with capital_risk_step(store, job_id, observed=observed) as deferred_by:
            assert deferred_by == held.status["id"]
        old = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=STALE_TRANSFER_S + 60)
        store.write_json(
            job_id,
            "state/capital_transfer.json",
            {**held.status, "started_at": old.isoformat()},
        )
        for _ in range(2):
            with capital_risk_step(store, job_id, observed=observed) as deferred_by:
                assert deferred_by is None
    assert _journal_count(store, job_id, "capital_transfer_stale") == 1


def test_crashed_transfer_marker_is_retired_as_stale(tmp_path, monkeypatch) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch)
    crashed = {
        "seq": 3,
        "id": "dead",
        "kind": "deposit",
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    store.write_json(job_id, "state/capital_transfer.json", crashed)

    with capital_risk_step(store, job_id, observed=crashed) as deferred_by:
        assert deferred_by is None

    status = capital_transfer_status(store, job_id)
    assert status["stale"] is True and status["finished_at"]
    assert _journal_count(store, job_id, "capital_transfer_stale") == 1


# ---------------------------------------------------------------- flow evidence


def test_unconfirmed_deposit_moves_the_peak_only_once_the_credit_shows(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch)
    _seed_venue_peak(store, job_id, 95.8, 86.8)
    record_capital_flow(
        store,
        job_id,
        "deposit",
        50.0,
        capital_delta=50.0,
        by="owner",
        equity_before=86.8,
        confirmed=False,
    )
    now = dt.datetime.now(dt.UTC)

    # 86.8 + 0.9 * 50 = 131.8 not reached: the credit is still in flight.
    assert (
        apply_flows_to_risk_peak(store, job_id, equity_now=120.0, observed_at=now) == []
    )
    assert store.read_json(job_id, "state/risk_state.json")["peak_equity"] == 95.8
    assert capital_flows(store, job_id)[0]["equity_applied"] is False

    events = apply_flows_to_risk_peak(store, job_id, equity_now=135.0, observed_at=now)

    assert len(events) == 1
    assert events[0]["peak_after"] == pytest.approx(95.8 * 136.8 / 86.8)
    flow = capital_flows(store, job_id)[0]
    assert flow["equity_applied"] is True and flow["confirmed"] is True


def test_rescale_uses_recorded_equity_before_not_later_market_moves(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch)
    _seed_venue_peak(store, job_id, 120.0, 100.0)
    record_capital_flow(
        store,
        job_id,
        "withdrawal",
        40.0,
        capital_delta=-40.0,
        by="owner",
        equity_before=100.0,
    )

    # The market fell $5 after the withdrawal; that loss must stay a loss.
    (event,) = apply_flows_to_risk_peak(
        store, job_id, equity_now=55.0, observed_at=dt.datetime.now(dt.UTC)
    )

    assert event["peak_after"] == pytest.approx(120.0 * 60.0 / 100.0)
    drawdown = store.read_json(job_id, "state/risk_state.json")["drawdown"]
    assert drawdown == pytest.approx(55.0 / 72.0 - 1.0)


def test_owner_transfers_record_equity_before_and_confirmation(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch, capital=100.0)
    venue = FakeVenue(monkeypatch, equity=86.8, free=86.8)

    asyncio.run(sync_module.venue_deposit(job_id, 50.0, store=store))
    venue.equity = venue.free = 136.8
    asyncio.run(sync_module.venue_withdraw(job_id, 20.0, store=store))

    deposit, withdrawal = capital_flows(store, job_id)
    assert deposit["equity_before"] == 86.8 and deposit["confirmed"] is True
    assert withdrawal["equity_before"] == 136.8
    status = capital_transfer_status(store, job_id)
    assert status["seq"] == 2 and status["finished_at"]


def test_unconfirmed_credit_is_recorded_unconfirmed(tmp_path, monkeypatch) -> None:
    store, job_id = _funded_job(tmp_path, monkeypatch, capital=100.0)
    venue = FakeVenue(monkeypatch, equity=86.8, free=86.8)

    async def unconfirmed(*, wallet_label: str, amount_usdc: float) -> dict:
        return {"ok": True, "result": {"status": "unconfirmed", "effects": []}}

    monkeypatch.setattr(hl, "hyperliquid_deposit_usdc", unconfirmed)
    asyncio.run(sync_module.venue_deposit(job_id, 50.0, store=store))

    (flow,) = capital_flows(store, job_id)
    assert flow["confirmed"] is False and flow["equity_before"] == venue.equity


# ---------------------------------------------------------------- personas


@pytest.mark.parametrize(
    "manifest",
    [
        ".opencode/agents/wayfinder-job-worker.md",
        ".opencode/agents/wayfinder-job-auto-worker.md",
    ],
)
def test_job_agents_cannot_run_venue_transfers(manifest: str) -> None:
    frontmatter = Path(manifest).read_text().split("---")[1]
    bash = frontmatter.split("\n  bash:\n", 1)[1].split("\n\n", 1)[0]
    rules = [line.strip() for line in bash.splitlines() if line.strip().startswith('"')]
    # opencode permission rules are last-match-wins: the denies must follow
    # every broader rule.
    catch_all = max(
        index for index, rule in enumerate(rules) if rule.startswith('"*":')
    )
    for pattern in ('"*venue-deposit*": deny', '"*venue-withdraw*": deny'):
        assert pattern in rules
        assert rules.index(pattern) > catch_all
