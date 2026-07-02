from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.job import backtest_execution_job
from wayfinder_paths.jobs.execution.preflight import run_preflight
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore

STRATEGY = """
from wayfinder_paths.jobs.execution import OrderIntent


class Strategy:
    def __init__(self, params):
        self.params = params

    def decide(self, ctx):
        latest = ctx.view.latest("SNX")
        if "SNX" not in ctx.ledger.positions and float(latest["close"]) > 10.4:
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

IMPURE_STRATEGY = """
import time


def decide(ctx):
    time.time()
    return []
"""


def _bars(count: int = 8) -> list[dict[str, Any]]:
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


def _make_job(
    tmp_path: Path,
    *,
    strategy: str = STRATEGY,
    contract: str = "jobs_v1",
) -> tuple[JobStore, str, Path]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "preflight-demo",
        script=".wayfinder/jobs/preflight-demo/workspace/src/strategy.py",
        interval_seconds=300,
        execution_contract=contract,  # type: ignore[arg-type]
    )
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "5m"
    job.execution_spec = spec.to_dict()
    job.execution_params = {"symbols": ["SNX"]}
    store.save(job)
    root = store.job_dir(job.id)
    script = root / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(strategy.lstrip(), encoding="utf-8")
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(_bars()), encoding="utf-8"
    )
    return store, job.id, root


def _check(report: dict[str, Any], name: str) -> dict[str, Any]:
    return next(check for check in report["checks"] if check["name"] == name)


def test_preflight_passes_for_well_behaved_strategy(tmp_path: Path) -> None:
    store, job_id, root = _make_job(tmp_path)

    report = run_preflight(job_id, store=store)

    assert report["status"] == "passed", report["checks"]
    assert report["revision"]
    for name in (
        "driver_ticks_complete",
        "purity_ok",
        "no_lookahead",
        "stale_tick_no_open",
        "rejected_fill_no_state_clear",
        "ambiguous_fill_no_success_report",
        "restart_recovers_position",
        "duplicate_tick_idempotent",
    ):
        assert _check(report, name)["passed"] is True, name
    assert (root / "reports" / "preflight" / "latest.json").exists()
    assert not (root / "reports" / "preflight" / "sandbox").exists()


def test_preflight_fails_for_impure_strategy(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path, strategy=IMPURE_STRATEGY)

    report = run_preflight(job_id, store=store)

    assert report["status"] == "failed"
    assert _check(report, "purity_ok")["passed"] is False


def test_preflight_refuses_legacy_jobs(tmp_path: Path) -> None:
    store, job_id, _ = _make_job(tmp_path, contract="legacy")

    report = run_preflight(job_id, store=store)

    assert report["status"] == "failed"
    assert _check(report, "execution_contract_jobs_v1")["passed"] is False


def test_preflight_requires_dataset(tmp_path: Path) -> None:
    store, job_id, root = _make_job(tmp_path)
    (root / "results" / "backtest" / "input_bars.json").unlink()

    report = run_preflight(job_id, store=store)

    assert report["status"] == "failed"
    assert _check(report, "dataset_available")["passed"] is False


def test_backtest_artifacts_are_revision_stamped(tmp_path: Path) -> None:
    store, job_id, root = _make_job(tmp_path)

    payload = backtest_execution_job(job_id, store=store)

    assert payload["revision"]
    assert payload["generated_at"]
    latest = json.loads(
        (root / "results" / "backtest" / "latest.json").read_text(encoding="utf-8")
    )
    assert latest["revision"] == payload["revision"]
    assert latest["dataset"]["source"].endswith("input_bars.json")
