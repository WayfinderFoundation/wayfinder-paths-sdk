"""The honest readout: verdict rules on harnessed jobs, the fixed no-claim
sentence on scripts, and the artifact it writes."""

from __future__ import annotations

import json
from pathlib import Path

from wayfinder_paths.jobs.execution.job import backtest_execution_job
from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.execution.validation import validate_execution_job
from wayfinder_paths.jobs.freestyle.validate import validate_freestyle_job
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.readout import NO_CLAIM_SENTENCE, READOUT_PATH, build_readout
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.tests.test_execution_contract import _bars, _write_strategy


def _jobs_v1(tmp_path: Path) -> tuple[JobStore, WayfinderJob]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "exec-demo",
        script=".wayfinder/jobs/exec-demo/workspace/src/strategy.py",
        interval_seconds=300,
        execution_contract="jobs_v1",
    )
    job.execution_spec = ExecutionSpec().to_dict()
    store.save(job)
    root = store.job_dir(job.id)
    _write_strategy(root / "workspace" / "src" / "strategy.py")
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps({"bars": _bars(), "metadata": {"label_convention": "close_time"}}),
        encoding="utf-8",
    )
    return store, job


def _experiment_row(
    grid_id: str, *, oos_positive: int, folds: int, oos_mean: float, decay: float
) -> dict:
    return {
        "grid_id": grid_id,
        "walk_forward": {
            "summary": {
                "fold_count": folds,
                "oos_positive_folds": oos_positive,
                "oos_return_mean": oos_mean,
                "decay_ratio": decay,
                "is_return_mean": 0.1,
            },
            "folds": [
                {
                    "test_window": {"start": "2026-01-01", "end": "2026-01-10"},
                    "test_stats": {
                        "net_return": 0.02,
                        "sharpe": 1.1,
                        "max_drawdown_pct": -0.01,
                    },
                }
                for _ in range(folds)
            ],
        },
    }


def test_readout_without_backtest_says_so(tmp_path: Path) -> None:
    store, job = _jobs_v1(tmp_path)
    readout = build_readout(job.id, store=store)
    assert readout["verdict"] == "no_backtest"
    assert "backtest" in readout["missing"]
    assert readout["launch_allowed"] is False  # nothing validated yet
    assert (store.job_dir(job.id) / READOUT_PATH).exists()


def test_readout_verdicts_follow_the_holdout_rules(tmp_path: Path) -> None:
    store, job = _jobs_v1(tmp_path)
    backtest_execution_job(job.id, store=store)
    validate_execution_job(job.id, store=store)
    root = store.job_dir(job.id)
    experiments = root / "results" / "backtest" / "experiments.jsonl"

    weak = build_readout(job.id, store=store)
    assert weak["verdict"] in {"weak", "not_supported_by_backtest"}
    assert weak["evidence"]["backtest"]["stats"]["trade_count"] >= 0
    if weak["verdict"] == "weak":
        assert any("walk-forward" in r for r in weak["reasons"])

    net_return = float(weak["evidence"]["backtest"]["stats"]["net_return"] or 0)
    if net_return > 0:
        experiments.write_text(
            json.dumps(
                _experiment_row("g1", oos_positive=3, folds=3, oos_mean=0.03, decay=0.9)
            )
            + "\n",
            encoding="utf-8",
        )
        supported = build_readout(job.id, store=store)
        assert supported["verdict"] == "supported", supported["reasons"]
        assert (
            supported["evidence"]["holdout"]["last_fold"]["test_stats"]["net_return"]
            == 0.02
        )

        experiments.write_text(
            json.dumps(
                _experiment_row("g2", oos_positive=1, folds=3, oos_mean=0.01, decay=0.2)
            )
            + "\n",
            encoding="utf-8",
        )
        weak_again = build_readout(job.id, store=store)
        assert weak_again["verdict"] == "weak"
        assert any("folds are positive" in r for r in weak_again["reasons"])

        experiments.write_text(
            json.dumps(
                _experiment_row(
                    "g3", oos_positive=0, folds=3, oos_mean=-0.02, decay=0.0
                )
            )
            + "\n",
            encoding="utf-8",
        )
        rejected = build_readout(job.id, store=store)
        assert rejected["verdict"] == "not_supported_by_backtest"


def test_freestyle_readout_makes_no_performance_claim(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "hormuz",
        script="workspace/src/hormuz.py",
        interval_seconds=300,
        timeout_seconds=60,
        execution_contract="freestyle_v1",
        source={"kind": "freestyle"},
    )
    root = store.init_layout(job)
    (root / "workspace" / "src" / "hormuz.py").write_text(
        "def tick(ctx):\n    ctx.act({'venue': 'hyperliquid', 'kind': 'market', 'symbol': 'BTC', 'side': 'long', 'notional': 10})\n",
        encoding="utf-8",
    )
    store.save(job)
    before = build_readout(job.id, store=store)
    assert before["verdict"] == "no_backtest" and before["launch_allowed"] is False
    validate_freestyle_job(job.id, store=store)
    after = build_readout(job.id, store=store)
    assert after["verdict"] == "no_backtest" and after["performance_claim"] is None
    assert after["reasons"][0] == NO_CLAIM_SENTENCE
    assert after["launch_allowed"] is True
    assert after["evidence"]["action_ledger_preview"][0]["symbol"] == "BTC"


def _bars_doc(count: int, symbols: tuple[str, ...] = ("BTC",)) -> dict[str, object]:
    from datetime import UTC, datetime, timedelta

    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {
            "timestamp": (start + timedelta(hours=i)).isoformat(),
            "symbol": symbol,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
        }
        for i in range(count)
        for symbol in symbols
    ]
    return {"metadata": {"days": count // 24}, "bars": rows}


def test_feasible_holdout_fits_the_dataset(tmp_path) -> None:
    """The refresh used to launch experiments with test_bars=500 x 3 folds
    plus a 2000-bar train window on a 2879-bar dataset and fail; the
    holdout now shrinks folds, then the test window, until it fits."""
    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.readout import dataset_bar_count, feasible_holdout
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("holdout-demo", script="strategy.py", interval_seconds=3600)
    store.init_layout(job)
    store.save(job)
    assert feasible_holdout(store, job.id) is None  # no dataset yet

    store.write_json(
        job.id, "results/backtest/input_bars.json", _bars_doc(2879, ("BTC", "HYPE"))
    )
    assert dataset_bar_count(store, job.id) == 2879
    # The requested 500 x 3 does not fit (needs 3500); one fold of 500 does.
    holdout = feasible_holdout(store, job.id, test_bars=500, folds=3)
    assert holdout == {
        "test_bars": 500,
        "train_bars": None,
        "folds": 1,
        "anchored": False,
    }
    # Default: the last ~15% as one window, three folds when they fit.
    holdout = feasible_holdout(store, job.id)
    assert holdout["test_bars"] == 431 and holdout["folds"] == 2  # 2 x 431 + 1724 fits

    store.write_json(job.id, "results/backtest/input_bars.json", _bars_doc(200))
    holdout = feasible_holdout(store, job.id, test_bars=100, folds=3)
    assert holdout is not None and holdout["test_bars"] < 100 and holdout["folds"] == 1
    store.write_json(job.id, "results/backtest/input_bars.json", _bars_doc(70))
    assert feasible_holdout(store, job.id, test_bars=50) is None
