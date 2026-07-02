from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.execution import (
    CompletedBarsView,
    ExecutionSpec,
    FillEvent,
    OrderIntent,
    PositionLedger,
    TradeCapacity,
    VenueCapabilities,
    VenueState,
)
from wayfinder_paths.jobs.execution.driver import tick_job
from wayfinder_paths.jobs.execution.engine import EngineState
from wayfinder_paths.jobs.execution.paper import PaperBroker
from wayfinder_paths.jobs.execution.primitives import PositionRecord
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore

PERP_CAPS = VenueCapabilities(
    market_kind="perp", supports_brackets=True, supports_shorts=True
)

STRATEGY = """
from wayfinder_paths.jobs.execution import OrderIntent


class Strategy:
    def __init__(self, params):
        self.params = params

    def decide(self, ctx):
        latest = ctx.view.latest("SNX")
        threshold = float(self.params.get("threshold", 10.4))
        if "SNX" not in ctx.ledger.positions and float(latest["close"]) > threshold:
            return [
                OrderIntent(
                    action="OPEN",
                    venue="hyperliquid",
                    symbol="SNX",
                    side="long",
                    size=1,
                    bracket={"stop_loss": 5.0, "take_profit": 50.0},
                )
            ]
        return []


def build_strategy(params):
    return Strategy(params)
"""


def _bars(count: int) -> list[dict[str, Any]]:
    rows = []
    for index in range(count):
        minute = index * 5
        price = 10.0 + index * 0.5
        rows.append(
            {
                "timestamp": f"2026-01-01T{minute // 60:02}:{minute % 60:02}:00Z",
                "symbol": "SNX",
                "open": price,
                "high": price + 0.6,
                "low": price - 0.3,
                "close": price + 0.5,
                "volume": 100,
            }
        )
    return rows


class FakeLiveBroker:
    capabilities = PERP_CAPS

    def __init__(
        self,
        *,
        venue_positions: dict[str, PositionRecord] | None = None,
        fetch_error: Exception | None = None,
    ) -> None:
        self.placed: list[OrderIntent] = []
        self.venue_positions = venue_positions or {}
        self.fetch_error = fetch_error
        self.snapshot: Any = None

    async def place(
        self, intent: OrderIntent, *, timestamp: str, price: float | None = None
    ) -> FillEvent:
        self.placed.append(intent)
        return FillEvent(
            status="filled",
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            filled_size=float(intent.size or 1.0),
            avg_price=float(price or 10.0),
            reduce_only=intent.reduce_only,
            client_order_id=intent.client_order_id,
            raw={"intent_action": intent.action, "intent_metadata": intent.metadata},
            timestamp=timestamp,
        )

    async def fetch_state(self, symbols: Any = ()) -> VenueState:
        if self.fetch_error is not None:
            raise self.fetch_error
        return VenueState(positions=dict(self.venue_positions), source="fake")

    async def get_capacity(self, symbol: str, side: str) -> TradeCapacity:
        return TradeCapacity(safe=True, source="fake")

    async def cancel(self, client_order_id: str) -> FillEvent:
        return FillEvent(status="rejected", venue="fake", symbol="", side="")


class FakeFeed:
    def __init__(self, view: CompletedBarsView) -> None:
        self.view = view

    async def get_completed_bars(
        self, symbols: Any, interval: str, *, lookback_bars: int, as_of: Any = None
    ) -> CompletedBarsView:
        return self.view

    async def get_events(self, symbols: Any, *, since: Any = None) -> list[Any]:
        return []


class FakeAdapter:
    name = "hyperliquid"
    capabilities = PERP_CAPS

    def __init__(self, view: CompletedBarsView, broker: Any) -> None:
        self.feed = FakeFeed(view)
        self.broker = broker


def _make_job(
    tmp_path: Path, *, mode: str = "paper", params: dict[str, Any] | None = None
) -> tuple[JobStore, WayfinderJob, Path]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "driver-demo",
        script=".wayfinder/jobs/driver-demo/workspace/src/strategy.py",
        interval_seconds=300,
        execution_contract="jobs_v1",
    )
    job.script_loop.mode = mode
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "5m"
    job.execution_spec = spec.to_dict()
    job.execution_params = {"symbols": ["SNX"], "threshold": 10.4, **(params or {})}
    store.save(job)
    root = store.job_dir(job.id)
    script = root / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(STRATEGY.lstrip(), encoding="utf-8")
    return store, job, root


def _view(count: int) -> CompletedBarsView:
    return CompletedBarsView.from_rows(_bars(count))


def _now(view: CompletedBarsView) -> pd.Timestamp:
    return view.timestamps[-1]


async def test_happy_paper_ticks_fill_at_next_bar_open(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    broker = PaperBroker(capabilities=PERP_CAPS)

    first = await tick_job(
        job,
        root,
        "paper",
        store=store,
        adapters={"hyperliquid": FakeAdapter(_view(1), broker)},
        now=_now(_view(1)),
    )
    assert first["ok"] is True
    assert first["intents"] and first["fills"] == []

    second_view = _view(2)
    second = await tick_job(
        job,
        root,
        "paper",
        store=store,
        adapters={"hyperliquid": FakeAdapter(second_view, broker)},
        now=_now(second_view),
    )
    assert second["fills"], "queued intent should fill at the next bar's open"
    assert second["fills"][0]["avg_price"] == 10.5  # bar 1 open
    assert (root / "state" / "engine_state.json").exists()
    ticks = (root / "results" / "forward" / "ticks.jsonl").read_text().splitlines()
    assert len(ticks) == 2
    assert json.loads(ticks[0])["view_hash"]


async def test_duplicate_bar_tick_is_skipped_and_idempotent(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    broker = PaperBroker(capabilities=PERP_CAPS)
    view = _view(2)
    adapters = {"hyperliquid": FakeAdapter(view, broker)}

    first = await tick_job(
        job, root, "paper", store=store, adapters=adapters, now=_now(view)
    )
    second = await tick_job(
        job, root, "paper", store=store, adapters=adapters, now=_now(view)
    )

    assert first["skipped"] is False
    assert second["skipped"] is True
    assert second["skip_reason"] == "no_new_bar"


async def test_stale_feed_skips_and_routes_no_opens(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    broker = FakeLiveBroker()
    view = _view(2)
    late = _now(view) + pd.Timedelta(hours=2)

    result = await tick_job(
        job,
        root,
        "live",
        store=store,
        adapters={"hyperliquid": FakeAdapter(view, broker)},
        now=late,
    )

    assert result["skipped"] is True
    assert result["skip_reason"] == "stale_data"
    assert broker.placed == []


async def test_restart_adopts_venue_positions_when_state_missing(
    tmp_path: Path,
) -> None:
    store, job, root = _make_job(tmp_path, mode="live")
    venue_position = PositionRecord(symbol="SNX", side="long", size=2.0, avg_price=9.5)
    broker = FakeLiveBroker(venue_positions={"SNX": venue_position})
    view = _view(2)

    result = await tick_job(
        job,
        root,
        "live",
        store=store,
        adapters={"hyperliquid": FakeAdapter(view, broker)},
        now=_now(view),
    )

    assert result["ok"] is True
    assert result["snapshot"]["status"] == "valid"
    assert result["positions"]["SNX"]["size"] == 2.0
    assert result["positions"]["SNX"]["metadata"]["adopted_from_venue"] is True
    assert any(
        event["kind"] == "adopted_from_venue" for event in result["guard_events"]
    )


async def test_ledger_venue_divergence_goes_reduce_only(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path, mode="live")
    state = EngineState(ledger=PositionLedger())
    state.ledger.apply_fill(
        FillEvent(
            status="filled",
            venue="hyperliquid",
            symbol="IMX",
            side="long",
            filled_size=3.0,
            avg_price=9.0,
        )
    )
    state.save(root / "state" / "engine_state.json")
    broker = FakeLiveBroker(venue_positions={})  # venue says flat

    view = _view(2)
    result = await tick_job(
        job,
        root,
        "live",
        store=store,
        adapters={"hyperliquid": FakeAdapter(view, broker)},
        now=_now(view),
    )

    assert result["snapshot"]["status"] == "ambiguous"
    assert result["intents"] == []  # strategy's OPEN blocked in reduce-only mode
    assert any(
        event["kind"] == "intent_rejected" and "reduce-only" in event["reason"]
        for event in result["guard_events"]
    )
    # local state must never be cleared on a mismatch
    restored = EngineState.load(root / "state" / "engine_state.json")
    assert "IMX" in restored.ledger.positions
    journal = (
        (root / "journal.jsonl").read_text()
        if (root / "journal.jsonl").exists()
        else ""
    )
    assert "reconcile_mismatch" in journal


async def test_venue_fetch_failure_is_ambiguous_not_state_clearing(
    tmp_path: Path,
) -> None:
    store, job, root = _make_job(tmp_path, mode="live")
    state = EngineState(ledger=PositionLedger())
    state.ledger.apply_fill(
        FillEvent(
            status="filled",
            venue="hyperliquid",
            symbol="SNX",
            side="long",
            filled_size=1.0,
            avg_price=9.0,
        )
    )
    state.save(root / "state" / "engine_state.json")
    broker = FakeLiveBroker(fetch_error=RuntimeError("429 rate limited"))

    view = _view(2)
    result = await tick_job(
        job,
        root,
        "live",
        store=store,
        adapters={"hyperliquid": FakeAdapter(view, broker)},
        now=_now(view),
    )

    assert result["snapshot"]["status"] == "ambiguous"
    restored = EngineState.load(root / "state" / "engine_state.json")
    assert "SNX" in restored.ledger.positions


async def test_deterministic_client_order_ids(tmp_path: Path) -> None:
    async def run_once(base: Path) -> str:
        store, job, root = _make_job(base)
        broker = PaperBroker(capabilities=PERP_CAPS)
        view = _view(1)
        result = await tick_job(
            job,
            root,
            "paper",
            store=store,
            adapters={"hyperliquid": FakeAdapter(view, broker)},
            now=_now(view),
        )
        return result["intents"][0]["client_order_id"]

    first = await run_once(tmp_path / "a")
    second = await run_once(tmp_path / "b")

    assert first == second
    assert first.startswith("0x")


def test_paper_ticks_match_backtest_fills(tmp_path: Path) -> None:
    """The key parity test: driving the real driver tick-by-tick over the same
    bars with a PaperBroker must produce exactly the fills the backtest
    produced for the same strategy/params/spec."""
    import asyncio

    store, job, root = _make_job(tmp_path)
    bars = _bars(6)
    spec = ExecutionSpec.from_dict(job.execution_spec)
    backtest = simulate_execution(
        root / "workspace" / "src" / "strategy.py",
        PreparedExecutionDataset.from_rows(bars),
        spec,
        job.execution_params,
    )

    async def _drive() -> list[dict[str, Any]]:
        broker = PaperBroker(capabilities=PERP_CAPS)
        fills: list[dict[str, Any]] = []
        for count in range(1, len(bars) + 1):
            view = CompletedBarsView.from_rows(bars[:count])
            result = await tick_job(
                job,
                root,
                "paper",
                store=store,
                adapters={"hyperliquid": FakeAdapter(view, broker)},
                now=_now(view),
            )
            fills.extend(result["fills"])
        return fills

    driver_fills = asyncio.run(_drive())

    def key(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        return [
            (
                row["symbol"],
                row["side"],
                row["filled_size"],
                row["avg_price"],
                row["timestamp"],
            )
            for row in rows
            if row["status"] == "filled"
        ]

    assert key(driver_fills) == key(backtest.trace["fills"])


async def test_tick_records_strategy_state_in_engine_state_pre(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    stateful = root / "workspace" / "src" / "strategy.py"
    stateful.write_text(
        """
def decide(ctx):
    ctx.strategy_state["seen"] = int(ctx.strategy_state.get("seen") or 0) + 1
    return []
""".lstrip(),
        encoding="utf-8",
    )
    broker = PaperBroker(capabilities=PERP_CAPS)

    first_view = _view(1)
    await tick_job(
        job,
        root,
        "paper",
        store=store,
        adapters={"hyperliquid": FakeAdapter(first_view, broker)},
        now=_now(first_view),
    )
    second_view = _view(2)
    await tick_job(
        job,
        root,
        "paper",
        store=store,
        adapters={"hyperliquid": FakeAdapter(second_view, broker)},
        now=_now(second_view),
    )

    ticks = [
        json.loads(line)
        for line in (root / "results" / "forward" / "ticks.jsonl")
        .read_text()
        .splitlines()
    ]
    assert ticks[0]["engine_state_pre"].get("strategy_state", {}) == {}
    assert ticks[1]["engine_state_pre"]["strategy_state"] == {"seen": 1}
    restored = EngineState.load(root / "state" / "engine_state.json")
    assert restored.strategy_state == {"seen": 2}


def test_compiler_wrapper_branches_on_contract(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    compiler = JobCompiler(store=store)
    wrappers = compiler._write_wrappers(job, root)
    jobs_v1_wrapper = (tmp_path / wrappers["script"]).read_text(encoding="utf-8")
    assert "run_scheduled_tick" in jobs_v1_wrapper
    assert "runpy" not in jobs_v1_wrapper

    legacy = WayfinderJob.new(
        "legacy-demo",
        script=".wayfinder/jobs/legacy-demo/workspace/src/strategy.py",
        interval_seconds=300,
    )
    store.save(legacy)
    legacy_root = store.job_dir(legacy.id)
    legacy_script = legacy_root / "workspace" / "src" / "strategy.py"
    legacy_script.parent.mkdir(parents=True, exist_ok=True)
    legacy_script.write_text("print('legacy')\n", encoding="utf-8")
    legacy_wrappers = compiler._write_wrappers(legacy, legacy_root)
    legacy_wrapper = (tmp_path / legacy_wrappers["script"]).read_text(encoding="utf-8")
    assert 'runpy.run_path(str(ENTRYPOINT), run_name="__main__")' in legacy_wrapper
    assert "run_scheduled_tick" not in legacy_wrapper
