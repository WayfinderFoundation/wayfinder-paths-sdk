from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    run_execution_grid,
    simulate_execution,
)
from wayfinder_paths.jobs.execution.validation import validate_execution_job
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def _write_strategy(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
from wayfinder_paths.jobs.execution import OrderIntent


class Strategy:
    def __init__(self, params):
        self.params = params

    def decide(self, ctx):
        latest = ctx.view.latest("SNX")
        threshold = float(self.params.get("threshold", 10.4))
        if not ctx.ledger.positions and float(latest["close"]) > threshold:
            return [
                OrderIntent(
                    action="OPEN",
                    venue="hyperliquid",
                    symbol="SNX",
                    side="long",
                    size=1,
                    bracket={"stop_loss": 9.0, "take_profit": 12.0},
                )
            ]
        return []


def build_strategy(params):
    return Strategy(params)
""".lstrip(),
        encoding="utf-8",
    )


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
    interval_seconds: int | None = None,
    cron_expr: str | None = None,
    timezone: str = "UTC",
    timeout_seconds: int = 120,
    bar_interval: str | None = None,
    jobs_v1: bool = False,
) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "timing-demo",
        script=".wayfinder/jobs/timing-demo/workspace/src/strategy.py",
        interval_seconds=interval_seconds,
        cron_expr=cron_expr,
        timezone=timezone,
        timeout_seconds=timeout_seconds,
    )
    spec = ExecutionSpec()
    if bar_interval:
        spec.data_contract["bar_interval"] = bar_interval
    job.execution_spec = spec.to_dict()
    store.save(job)
    root = store.job_dir(job.id)
    _write_strategy(root / "workspace" / "src" / "strategy.py")
    if jobs_v1:
        job_yaml = root / "job.yaml"
        data = yaml.safe_load(job_yaml.read_text(encoding="utf-8"))
        data["execution_contract"] = "jobs_v1"
        job_yaml.write_text(yaml.safe_dump(data), encoding="utf-8")
    return store, job.id


def _check(report: dict[str, Any], name: str) -> dict[str, Any]:
    return next(check for check in report["checks"] if check["name"] == name)


def test_valid_interval_schedule_passes(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, interval_seconds=300, bar_interval="5m")

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "passed"
    assert _check(report, "bar_interval_declared")["passed"] is True
    assert _check(report, "schedule_declared_valid")["passed"] is True
    assert _check(report, "schedule_matches_bar_interval")["passed"] is True
    assert _check(report, "timeout_vs_interval")["passed"] is True
    assert _check(report, "staleness_policy_valid")["passed"] is True


def test_interval_slower_than_bars_blocks(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, interval_seconds=3600, bar_interval="5m")

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "failed"
    assert _check(report, "schedule_matches_bar_interval")["passed"] is False


def test_cron_period_slower_than_bars_blocks(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, cron_expr="0 * * * *", bar_interval="5m")

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "failed"
    check = _check(report, "schedule_matches_bar_interval")
    assert check["passed"] is False
    assert check["schedule_period_seconds"] == 3600


def test_cron_faster_than_bars_passes(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, cron_expr="*/5 * * * *", bar_interval="1h")

    report = validate_execution_job(job_id, store=store)

    assert _check(report, "schedule_matches_bar_interval")["passed"] is True


def test_invalid_cron_expression_blocks(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, cron_expr="not a cron", bar_interval="5m")

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "failed"
    assert _check(report, "schedule_declared_valid")["passed"] is False


def test_invalid_timezone_blocks(tmp_path: Path) -> None:
    store, job_id = _make_job(
        tmp_path, cron_expr="*/5 * * * *", timezone="Mars/Olympus", bar_interval="5m"
    )

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "failed"
    assert _check(report, "schedule_declared_valid")["passed"] is False


def test_missing_schedule_blocks(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, bar_interval="5m")

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "failed"
    assert _check(report, "schedule_declared_valid")["passed"] is False


def test_timeout_exceeding_interval_blocks(tmp_path: Path) -> None:
    store, job_id = _make_job(
        tmp_path, interval_seconds=300, timeout_seconds=300, bar_interval="5m"
    )

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "failed"
    assert _check(report, "timeout_vs_interval")["passed"] is False


def test_bar_interval_optional_for_legacy_jobs(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, interval_seconds=300)

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "passed"
    assert _check(report, "bar_interval_declared")["passed"] is True


def test_bar_interval_required_for_jobs_v1(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, interval_seconds=300, jobs_v1=True)

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "failed"
    check = _check(report, "bar_interval_declared")
    assert check["passed"] is False
    assert check["blocking"] is True


def test_stats_include_risk_metrics(tmp_path: Path) -> None:
    script = tmp_path / "strategy.py"
    _write_strategy(script)
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "5m"

    result = simulate_execution(
        script,
        PreparedExecutionDataset.from_rows(_bars()),
        spec,
        {"threshold": 10.4, "initial_capital": 1000},
    )

    stats = result.stats
    assert {
        "net_return",
        "ending_equity",
        "trade_count",
        "sharpe",
        "max_drawdown_pct",
        "win_rate",
        "profit_factor",
        "avg_trade_pnl",
        "exposure_pct",
    } <= set(stats)
    assert stats["trade_count"] >= 2
    assert stats["max_drawdown_pct"] <= 0
    assert 0 <= stats["win_rate"] <= 1
    assert stats["profit_factor"] is None or stats["profit_factor"] >= 0
    assert stats["avg_trade_pnl"] is not None
    assert stats["exposure_pct"] > 0
    assert stats["sharpe"] is not None


def test_grid_ranks_by_sharpe_and_rejects_unknown_keys(tmp_path: Path) -> None:
    script = tmp_path / "strategy.py"
    _write_strategy(script)
    dataset = PreparedExecutionDataset.from_rows(_bars())

    result = run_execution_grid(
        script,
        dataset,
        ExecutionSpec(),
        {"threshold": [10.4, 100.0]},
        rank_by="sharpe",
    )

    assert result.rank_by == "sharpe"
    assert len(result.runs) == 2
    assert all("sharpe" in row["stats"] for row in result.runs)

    with pytest.raises(ValueError, match="rank_by"):
        run_execution_grid(
            script,
            dataset,
            ExecutionSpec(),
            {"threshold": [10.4]},
            rank_by="bogus_metric",
        )
