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


def test_entrypoint_inside_workspace_passes(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, interval_seconds=300, bar_interval="5m")
    report = validate_execution_job(job_id, store=store)
    check = _check(report, "entrypoint_inside_workspace")
    assert check["passed"] is True
    assert check["blocking"] is True


def test_entrypoint_outside_workspace_blocks_jobs_v1(tmp_path: Path) -> None:
    """A job-root strategy can never be versioned or proposed — validation
    must fail with the named check (this is how live jobs got stuck with
    active_revision null)."""
    store, job_id = _make_job(tmp_path, interval_seconds=300, bar_interval="5m")
    root = store.job_dir(job_id)
    rogue = root / "strategy.py"
    rogue.write_text(
        (root / "workspace" / "src" / "strategy.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    job_yaml = root / "job.yaml"
    data = yaml.safe_load(job_yaml.read_text(encoding="utf-8"))
    data["script_loop"]["entrypoint"] = str(rogue)
    job_yaml.write_text(yaml.safe_dump(data), encoding="utf-8")

    report = validate_execution_job(job_id, store=store)

    check = _check(report, "entrypoint_inside_workspace")
    assert check["passed"] is False
    assert check["expected_dir"].endswith("workspace/src")
    assert "workspace/src" in check["hint"]
    assert report["status"] == "failed"


def _close_stop_report(tmp_path: Path, body: str) -> dict[str, Any]:
    store, job_id = _make_job(tmp_path, interval_seconds=300, bar_interval="5m")
    script = store.job_dir(job_id) / "workspace" / "src" / "strategy.py"
    script.write_text(body, encoding="utf-8")
    return validate_execution_job(job_id, store=store)


def test_close_stop_check_ignores_comments(tmp_path: Path) -> None:
    report = _close_stop_report(
        tmp_path,
        "# time stop: close if held > N days; 0 to disable\n"
        "def decide(ctx):\n    return []\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    assert _check(report, "no_close_only_stop_tp")["passed"] is True


def test_close_stop_check_ignores_docstrings(tmp_path: Path) -> None:
    report = _close_stop_report(
        tmp_path,
        '"""Stop logic note: we close via brackets, not manually."""\n'
        "def decide(ctx):\n    return []\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    assert _check(report, "no_close_only_stop_tp")["passed"] is True


def test_close_stop_check_still_fires_in_code(tmp_path: Path) -> None:
    report = _close_stop_report(
        tmp_path,
        "def decide(ctx):\n"
        "    stop_hit = ctx.view.latest('SNX')['close'] < 9\n"
        "    if stop_hit:\n"
        "        return [{'action': 'CLOSE'}]\n"
        "    return []\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    assert _check(report, "no_close_only_stop_tp")["passed"] is False


def test_bracket_escape_hatch_must_be_code_not_comment(tmp_path: Path) -> None:
    report = _close_stop_report(
        tmp_path,
        "# BracketEngine handles this... eventually\n"
        "def decide(ctx):\n"
        "    stop_hit = ctx.view.latest('SNX')['close'] < 9\n"
        "    if stop_hit:\n"
        "        return [{'action': 'CLOSE'}]\n"
        "    return []\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    assert _check(report, "no_close_only_stop_tp")["passed"] is False


def test_boot_relative_warmup_counter_is_flagged(tmp_path: Path) -> None:
    report = _close_stop_report(
        tmp_path,
        "def decide(ctx):\n"
        "    bar_count = ctx.strategy_state.get('bar_count', 0) + 1\n"
        "    ctx.strategy_state['bar_count'] = bar_count\n"
        "    if bar_count < 28:\n"
        "        return []\n"
        "    return []\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    check = _check(report, "no_boot_relative_warmup")
    assert check["passed"] is False
    assert check["blocking"] is False
    assert "every_n_bars" in check["hint"]


def test_data_derived_warmup_passes_counter_check(tmp_path: Path) -> None:
    report = _close_stop_report(
        tmp_path,
        "def decide(ctx):\n"
        "    if ctx.bar_index < 28 or not ctx.every_n_bars(2):\n"
        "        return []\n"
        "    return []\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    assert _check(report, "no_boot_relative_warmup")["passed"] is True


def test_live_mode_blocks_without_wallet_label() -> None:
    from wayfinder_paths.jobs.execution.validation import _live_wallet_checks

    live_no_wallet = {
        "execution_contract": "jobs_v1",
        "script_loop": {"mode": "live"},
        "execution_params": {"venue": "hyperliquid"},
    }
    checks = _live_wallet_checks(live_no_wallet)
    assert checks[0]["name"] == "wallet_label_declared"
    assert checks[0]["passed"] is False
    assert checks[0]["blocking"] is True
    assert "execution_params.wallet_label" in checks[0]["hint"]

    live_with_wallet = {
        "execution_contract": "jobs_v1",
        "script_loop": {"mode": "live"},
        "execution_params": {"wallet_label": "funding-carry-basket"},
    }
    assert _live_wallet_checks(live_with_wallet)[0]["passed"] is True

    # Paper mode and legacy jobs are exempt — the check exists for live only.
    paper = {"execution_contract": "jobs_v1", "script_loop": {"mode": "paper"}}
    assert _live_wallet_checks(paper) == []
    legacy = {"execution_contract": "legacy", "script_loop": {"mode": "live"}}
    assert _live_wallet_checks(legacy) == []
