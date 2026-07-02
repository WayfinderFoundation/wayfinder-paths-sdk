from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.experiments import (
    list_experiments,
    promote_params,
    run_experiment,
)
from wayfinder_paths.jobs.execution.simulator import PreparedExecutionDataset
from wayfinder_paths.jobs.execution.walk_forward import run_walk_forward
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore

DIRECTION_STRATEGY = """
def build_strategy(params):
    direction = str(params.get("direction") or "long")

    class Strategy:
        def decide(self, ctx):
            if "BTC" not in ctx.ledger.positions:
                return [
                    {
                        "action": "OPEN",
                        "venue": "hyperliquid",
                        "symbol": "BTC",
                        "side": "sell" if direction == "short" else "buy",
                        "size": 1,
                    }
                ]
            return []

    return Strategy()
"""


def _bars(
    count: int = 400,
    *,
    rise_until: int = 300,
    symbols: tuple[str, ...] = ("BTC",),
) -> list[dict[str, Any]]:
    rows = []
    price = 100.0
    for index in range(count):
        price *= 1.002 if index < rise_until else 0.997
        for symbol in symbols:
            rows.append(
                {
                    "timestamp": (
                        f"2026-{index // (24 * 28) + 1:02}-"
                        f"{(index // 24) % 28 + 1:02}T{index % 24:02}:00:00Z"
                    ),
                    "symbol": symbol,
                    "open": price,
                    "high": price * 1.001,
                    "low": price * 0.999,
                    "close": price,
                    "volume": 10,
                }
            )
    return rows


def _spec() -> ExecutionSpec:
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    return spec


def _script(tmp_path: Path) -> Path:
    script = tmp_path / "strategy.py"
    script.write_text(DIRECTION_STRATEGY.lstrip(), encoding="utf-8")
    return script


def test_planted_in_sample_param_shows_full_oos_decay(tmp_path: Path) -> None:
    """The SNX ST(7,2.5) failure mode in miniature: the grid picks the
    direction that worked on the rising train data; the held-out falling test
    windows lose on every fold — walk-forward must surface exactly that."""
    report = run_walk_forward(
        _script(tmp_path),
        PreparedExecutionDataset.from_rows(_bars()),
        _spec(),
        {"direction": ["long", "short"]},
        folds=2,
        test_bars=50,
        anchored=True,
        warmup_bars=60,
    )

    ok_folds = [row for row in report["folds"] if row["status"] == "ok"]
    assert len(ok_folds) == 2
    for row in ok_folds:
        assert row["params"]["direction"] == "long", "grid must pick the IS winner"
        assert row["train_stats"]["net_return"] > 0
        assert row["test_stats"]["net_return"] < 0
    summary = report["summary"]
    assert summary["oos_positive_folds"] == 0
    assert summary["is_return_mean"] > 0
    assert summary["oos_return_mean"] < 0
    assert summary["decay_ratio"] is not None and summary["decay_ratio"] < 0


def test_slicing_is_timestamp_based_multi_symbol_safe(tmp_path: Path) -> None:
    report = run_walk_forward(
        _script(tmp_path),
        PreparedExecutionDataset.from_rows(_bars(symbols=("BTC", "ETH"))),
        _spec(),
        {"direction": ["long"]},
        folds=2,
        test_bars=50,
        anchored=True,
    )

    for row in report["folds"]:
        assert row["test"]["bars"] == 50  # bar counts, not row counts (2 rows/ts)
    assert report["folds"][0]["train"]["bars"] == 300
    assert report["folds"][1]["train"]["bars"] == 350


def test_warmup_rebasing_counts_only_test_window_pnl(tmp_path: Path) -> None:
    """The buy-and-hold position opens during warmup; test-window net_return
    must equal the price move inside the window over the rebased equity base,
    unaffected by warmup gains."""
    bars = _bars()
    report = run_walk_forward(
        _script(tmp_path),
        PreparedExecutionDataset.from_rows(bars),
        _spec(),
        {"direction": ["long"]},
        folds=1,
        test_bars=50,
        anchored=True,
        warmup_bars=60,
    )

    fold = report["folds"][0]
    closes = [row["close"] for row in bars]
    test_start_idx = len(closes) - 50
    entry_idx = len(closes) - 50 - 60 + 1  # fill at next bar open after warmup bar 0
    entry_price = bars[entry_idx]["open"]
    initial = 10_000.0
    equity_at_test_start = initial + (closes[test_start_idx] - entry_price)
    equity_at_end = initial + (closes[-1] - entry_price)
    expected = equity_at_end / equity_at_test_start - 1
    assert fold["test_stats"]["net_return"] == pytest.approx(expected, abs=1e-9)


def test_insufficient_bars_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="needs at least"):
        run_walk_forward(
            _script(tmp_path),
            PreparedExecutionDataset.from_rows(_bars(count=120)),
            _spec(),
            {"direction": ["long"]},
            folds=3,
            test_bars=50,
            anchored=True,
        )
    with pytest.raises(ValueError, match="train_bars or anchored"):
        run_walk_forward(
            _script(tmp_path),
            PreparedExecutionDataset.from_rows(_bars()),
            _spec(),
            {"direction": ["long"]},
            folds=1,
            test_bars=50,
        )


def _make_bundle(tmp_path: Path) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "wf-demo",
        script=".wayfinder/jobs/wf-demo/workspace/src/strategy.py",
        interval_seconds=3600,
        execution_contract="jobs_v1",
    )
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    job.execution_spec = spec.to_dict()
    job.execution_params = {"symbols": ["BTC"], "initial_capital": 10_000.0}
    store.save(job)
    root = store.job_dir(job.id)
    script = root / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(DIRECTION_STRATEGY.lstrip(), encoding="utf-8")
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(_bars()), encoding="utf-8"
    )
    return store, job.id


def test_experiment_records_walk_forward_and_promotion_never_blocks(
    tmp_path: Path,
) -> None:
    store, job_id = _make_bundle(tmp_path)

    outcome = run_experiment(
        job_id,
        {"direction": ["long", "short"]},
        walk_forward={"folds": 2, "test_bars": 50, "anchored": True},
        store=store,
    )

    rows = list_experiments(job_id, store=store)
    assert len(rows) == 1
    row = rows[0]
    for key in ("ts", "grid_id", "rank_by", "run_count", "best"):
        assert key in row, key  # pre-existing schema unchanged
    assert row["walk_forward"]["summary"]["oos_positive_folds"] == 0
    assert len(row["walk_forward"]["folds"]) == 2

    promoted = promote_params(job_id, grid_id=row["grid_id"], store=store)

    assert promoted["mode"] == "direct", "terrible OOS decay must NOT block"
    assert promoted["walk_forward_summary"]["oos_positive_folds"] == 0
    job = store.load(job_id)
    assert job.execution_params["direction"] == "long"
    assert outcome["backtest"]["walk_forward"]["summary"]["fold_count"] == 2
