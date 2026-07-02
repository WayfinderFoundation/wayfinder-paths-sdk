from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.execution import CompletedBarsView, VenueCapabilities
from wayfinder_paths.jobs.execution.driver import tick_job
from wayfinder_paths.jobs.execution.paper import PaperBroker
from wayfinder_paths.jobs.execution.reconcile import reconcile_job
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.tests.test_jobs_live_driver import (
    FakeAdapter,
    _make_job,
)
from wayfinder_paths.tests.test_jobs_preflight import _bars

PERP_CAPS = VenueCapabilities(
    market_kind="perp", supports_brackets=True, supports_shorts=True
)


def _drive_ticks(store: JobStore, job: Any, root: Path, bars: list[dict]) -> None:
    async def _run() -> None:
        broker = PaperBroker(capabilities=PERP_CAPS)
        for count in range(1, len(bars) + 1):
            view = CompletedBarsView.from_rows(bars[:count])
            await tick_job(
                job,
                root,
                "paper",
                store=store,
                adapters={"hyperliquid": FakeAdapter(view, broker)},
                now=view.timestamps[-1],
            )

    asyncio.run(_run())


def test_reconcile_perfect_replay_matches(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    bars = _bars(8)
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(bars), encoding="utf-8"
    )
    _drive_ticks(store, job, root, bars)

    report = reconcile_job(job.id, store=store)

    assert report["ticks_compared"] > 0
    assert report["intent_match_rate"] == 1.0
    assert report["data_drift_ticks"] == 0
    assert report["missing_intents"] == []
    assert (root / "reports" / "reconcile" / "latest.json").exists()
    scorecard = store.read_json(job.id, "scorecard.json", default={})
    assert scorecard["reconcile"]["intent_match_rate"] == 1.0
    assert scorecard.get("health") != "red"


def test_reconcile_detects_mutated_recorded_intent(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    bars = _bars(8)
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(bars), encoding="utf-8"
    )
    _drive_ticks(store, job, root, bars)

    ticks_path = root / "results" / "forward" / "ticks.jsonl"
    rows = [json.loads(line) for line in ticks_path.read_text().splitlines()]
    mutated = False
    for row in rows:
        for intent in row.get("intents") or []:
            if intent.get("action") == "OPEN":
                intent["size"] = 999.0  # pretend live routed a different size
                mutated = True
                break
        if mutated:
            break
    assert mutated, "fixture should have produced at least one OPEN intent"
    ticks_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True, default=str) for row in rows) + "\n",
        encoding="utf-8",
    )

    report = reconcile_job(job.id, store=store)

    assert report["intent_match_rate"] < 1.0
    assert report["missing_intents"], "the mutated recorded intent must be flagged"
    assert report["extra_intents"], "the replayed intent must appear as extra"


def test_replay_matches_for_strategy_keyed_on_scratch_state(tmp_path: Path) -> None:
    """A strategy whose decisions depend on strategy_state must reconcile at
    100% — engine_state_pre carries the scratch dict, so the replay sees the
    exact pre-tick state."""
    store, job, root = _make_job(tmp_path)
    stateful = root / "workspace" / "src" / "strategy.py"
    stateful.write_text(
        """
from wayfinder_paths.jobs.execution import OrderIntent


def decide(ctx):
    st = ctx.strategy_state
    tick_number = int(st.get("tick_number") or 0) + 1
    st["tick_number"] = tick_number
    # Open only on the 3rd decision ever made -- purely scratch-state driven.
    if tick_number == 3 and "SNX" not in ctx.ledger.positions:
        return [
            OrderIntent(
                action="OPEN", venue="hyperliquid", symbol="SNX", side="long", size=1
            )
        ]
    return []
""".lstrip(),
        encoding="utf-8",
    )
    bars = _bars(6)
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(bars), encoding="utf-8"
    )
    _drive_ticks(store, job, root, bars)

    report = reconcile_job(job.id, store=store)

    assert report["ticks_compared"] > 0
    assert report["intent_match_rate"] == 1.0
    assert report["missing_intents"] == []
    assert report["extra_intents"] == []


def test_reconcile_reports_data_drift_on_view_hash_mismatch(tmp_path: Path) -> None:
    store, job, root = _make_job(tmp_path)
    bars = _bars(8)
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(bars), encoding="utf-8"
    )
    _drive_ticks(store, job, root, bars)

    drifted = [dict(row, close=row["close"] + 0.01) for row in bars]
    report = reconcile_job(
        job.id, store=store, history=CompletedBarsView.from_rows(drifted)
    )

    assert report["data_drift_ticks"] > 0
    assert report["ticks_compared"] == 0 or report["intent_match_rate"] is not None
