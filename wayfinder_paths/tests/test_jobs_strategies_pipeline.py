"""End-to-end framework proof for the three ported live strategies: each one
must pass execution-contract validation, preflight (simulated forward over
the real driver), paper-mode driver parity vs the backtest, and the live
gate."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec, VenueCapabilities
from wayfinder_paths.jobs.execution.driver import tick_job
from wayfinder_paths.jobs.execution.job import backtest_execution_job
from wayfinder_paths.jobs.execution.paper import PaperBroker
from wayfinder_paths.jobs.execution.preflight import run_preflight
from wayfinder_paths.jobs.execution.primitives import CompletedBarsView
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)
from wayfinder_paths.jobs.execution.validation import validate_execution_job
from wayfinder_paths.jobs.gating import evaluate_live_gate
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.tests.test_jobs_live_driver import FakeAdapter

PERP_CAPS = VenueCapabilities(
    market_kind="perp", supports_brackets=True, supports_shorts=True
)

STRATEGIES = {
    "snx-momentum": {
        "module": "snx_momentum",
        "symbol": "SNX",
        "params": {"notional_usd": 2500.0},
        "closes": (
            [10.0 + (i % 3) * 0.01 for i in range(28)]
            + [9.5, 9.45, 9.6, 10.3, 9.2, 9.15, 9.4]
        ),
    },
    "imx-momentum": {
        "module": "imx_momentum",
        "symbol": "IMX",
        "params": {"notional_usd": 3000.0},
        "closes": (
            [0.1260 + (i % 3) * 0.0002 for i in range(55)]
            + [0.1205, 0.1206, 0.1212, 0.1180, 0.1290, 0.1150, 0.1160]
        ),
    },
    "imx-atr-target": {
        "module": "imx_atr_target",
        "symbol": "IMX",
        "params": {"notional_usd": 1000.0},
        "closes": (
            [0.1260 + (i % 3) * 0.0002 for i in range(28)]
            + [0.1205, 0.1200, 0.1120, 0.1130, 0.1260, 0.1100]
        ),
    },
}


def _bars(closes: list[float], symbol: str) -> list[dict[str, Any]]:
    rows = []
    for index, close in enumerate(closes):
        rows.append(
            {
                "timestamp": f"2026-06-{index // 24 + 1:02}T{index % 24:02}:00:00Z",
                "symbol": symbol,
                "open": close,
                "high": close * 1.002,
                "low": close * 0.998,
                "close": close,
                "volume": 100,
            }
        )
    return rows


def _make_bundle(
    tmp_path: Path, name: str, config: dict[str, Any]
) -> tuple[JobStore, WayfinderJob, Path]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        name,
        script=f".wayfinder/jobs/{name}/workspace/src/strategy.py",
        interval_seconds=3600,
        execution_contract="jobs_v1",
    )
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    job.execution_spec = spec.to_dict()
    job.execution_params = {
        "symbols": [config["symbol"]],
        "symbol": config["symbol"],
        **config["params"],
    }
    store.save(job)
    root = store.job_dir(job.id)
    shim = root / "workspace" / "src" / "strategy.py"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(
        "from wayfinder_paths.jobs.strategies."
        f"{config['module']} import build_strategy\n\n"
        '__all__ = ["build_strategy"]\n',
        encoding="utf-8",
    )
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(_bars(config["closes"], config["symbol"])), encoding="utf-8"
    )
    return store, job, root


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_full_pipeline_validates_and_gates(tmp_path: Path, name: str) -> None:
    config = STRATEGIES[name]
    store, job, root = _make_bundle(tmp_path, name, config)

    backtest = backtest_execution_job(job.id, store=store)
    assert backtest["result"]["validation"]["execution_valid"] is True
    assert backtest["result"]["stats"]["trade_count"] >= 1, (
        "fixture must produce at least one trade"
    )

    preflight = run_preflight(job.id, store=store)
    assert preflight["status"] == "passed", [
        check for check in preflight["checks"] if not check["passed"]
    ]

    report = validate_execution_job(job.id, store=store)
    assert report["status"] == "passed", [
        check for check in report["checks"] if not check["passed"]
    ]

    gate = evaluate_live_gate(job.id, store=store)
    assert gate["live_ready"] is True, gate["reasons"]


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_paper_driver_matches_backtest(tmp_path: Path, name: str) -> None:
    """Simulated forward: driving the real driver tick-by-tick in paper mode
    over the same bars must produce exactly the fills the backtest produced."""
    config = STRATEGIES[name]
    store, job, root = _make_bundle(tmp_path, name, config)
    bars = _bars(config["closes"], config["symbol"])

    backtest = simulate_execution(
        root / "workspace" / "src" / "strategy.py",
        PreparedExecutionDataset.from_rows(bars),
        ExecutionSpec.from_dict(job.execution_spec),
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
                now=view.timestamps[-1],
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
                row["reduce_only"],
            )
            for row in rows
            if row["status"] == "filled"
        ]

    assert key(driver_fills) == key(backtest.trace["fills"])
    assert key(driver_fills), "parity is vacuous without at least one fill"
