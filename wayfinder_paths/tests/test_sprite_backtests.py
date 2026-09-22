from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from wayfinder_paths.jobs.execution.job import backtest_execution_job
from wayfinder_paths.jobs.sprite_bundle import extract_archive, pack_job
from wayfinder_paths.tests.test_jobs_preflight import _make_job


def _run_runtime(bundle: Path, directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "wayfinder_paths.jobs.sprite_runtime",
            "--bundle",
            str(bundle),
            "--root",
            str(directory / "remote"),
            "--output",
            str(directory / "summary.json"),
            "--artifacts",
            str(directory / "artifacts.tar.gz"),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )


@pytest.mark.parametrize(
    "mode",
    [
        "single",
        "quick",
        "serial",
        "thread",
        "process",
        "optuna",
        "walk_forward",
        "experiments",
        "robustness",
        "preflight",
        "validation",
    ],
)
def test_sdk_backtests_run_from_portable_workspace(tmp_path: Path, mode: str) -> None:
    store, job_id, job_root = _make_job(tmp_path / "source")
    # The source workspace uses an absolute entrypoint, a common Shell layout.
    job = yaml.safe_load((job_root / "job.yaml").read_text())
    job["script_loop"]["entrypoint"] = str(job_root / "workspace/src/strategy.py")
    job["execution_params"]["initial_capital"] = 1000
    (job_root / "job.yaml").write_text(yaml.safe_dump(job))
    baseline = backtest_execution_job(job_id, store=store)
    op = "backtest_job"
    options: dict[str, Any] = {}
    extra: list[str] = []
    if mode == "quick":
        options = {"quick_bars": 4}
    elif mode in {"serial", "thread", "process", "optuna", "walk_forward"}:
        grid = tmp_path / "source" / "grid.json"
        grid.write_text(json.dumps({"initial_capital": [1000, 2000]}))
        options = {
            "grid_path": str(grid),
            "parallel": mode if mode in {"thread", "process"} else "serial",
            "workers": 2,
        }
        extra = ["grid.json"]
        if mode == "optuna":
            grid.write_text(
                json.dumps(
                    {"initial_capital": {"type": "float", "low": 1000, "high": 2000}}
                )
            )
            options.update(
                optimizer="optuna", optuna_options={"n_trials": 2, "seed": 42}
            )
        if mode == "walk_forward":
            options["walk_forward"] = {
                "train_bars": 4,
                "test_bars": 2,
                "folds": 2,
                "warmup_bars": 1,
            }
    elif mode == "experiments":
        op = "experiments"
        options = {
            "grid": {"initial_capital": [1000, 2000]},
            "parallel": "process",
            "workers": 2,
        }
    elif mode == "robustness":
        op = "robustness_check"
        options = {
            "robustness_plan": {
                "neighbors": {"initial_capital": [1000, 2000]},
            }
        }
    elif mode == "preflight":
        op = "preflight"
    elif mode == "validation":
        op = "validate_job"
    source_bytes = (job_root / "job.yaml").read_bytes()
    bundle = tmp_path / "workspace.tar.gz"
    packed = pack_job(store, job_id, bundle, op=op, options=options, extra_paths=extra)
    output, artifacts = tmp_path / "summary.json", tmp_path / "artifacts.tar.gz"
    proc = _run_runtime(bundle, tmp_path)
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(output.read_text())
    assert summary["source_revision"] == packed["request"]["source_revision"]
    assert (job_root / "job.yaml").read_bytes() == source_bytes
    restored = tmp_path / "restored"
    extract_archive(artifacts, restored)
    result = json.loads((restored / "operation-result.json").read_text())
    assert (
        restored / ".wayfinder/jobs" / job_id / "workspace/src/strategy.py"
    ).is_file()
    if mode == "single":
        assert result["result"]["stats"] == baseline["result"]["stats"]
        assert result["result"]["trace"]
    if mode in {"serial", "thread", "process", "optuna", "walk_forward"}:
        assert len(result["result"]["runs"]) == 2
    if mode == "walk_forward":
        assert result["walk_forward"]["folds"]


def test_experiments_and_large_ml_artifacts_are_not_truncated(tmp_path: Path) -> None:
    store, job_id, root = _make_job(tmp_path / "source")
    code = root / "workspace/src/research.py"
    code.write_text("""
import json
import joblib
from sklearn.linear_model import LinearRegression
from wayfinder_paths.jobs.execution.experiments import run_experiment
from wayfinder_paths.jobs.store import JobStore
model = LinearRegression().fit([[1],[2],[3]], [2,4,6])
joblib.dump(model, 'trained.joblib')
result = run_experiment('preflight-demo', {'initial_capital':[1000,2000]}, parallel='serial', store=JobStore())
open('large-trace.bin','wb').write(b'0123456789' * 200000)
json.dump({'prediction': model.predict([[4]]).tolist(), 'experiment':result}, open('result.json','w'), default=str)
""")
    bundle = tmp_path / "workspace.tar.gz"
    pack_job(
        store,
        job_id,
        bundle,
        op="script",
        options={"path": str(code.relative_to(store.repo_root))},
    )
    proc = _run_runtime(bundle, tmp_path)
    assert proc.returncode == 0, proc.stderr
    extract_archive(tmp_path / "artifacts.tar.gz", tmp_path / "collected")
    assert (tmp_path / "collected/large-trace.bin").stat().st_size == 2000000
    assert (tmp_path / "collected/trained.joblib").is_file()
    assert json.loads((tmp_path / "collected/operation-result.json").read_text())[
        "prediction"
    ] == pytest.approx([8])


@pytest.mark.parametrize("failure", [False, True])
def test_script_preserves_legacy_backtests_nonfinite_metrics_and_partial_files(
    tmp_path: Path, failure: bool
) -> None:
    store, job_id, root = _make_job(tmp_path / "source")
    script = root / "workspace/src/legacy.py"
    script.write_text(
        """
import json
import pandas as pd
from wayfinder_paths.core.backtesting.backtester import run_backtest
from wayfinder_paths.core.backtesting.multi import run_multi_leverage_backtest
from wayfinder_paths.core.backtesting.types import BacktestConfig
prices = pd.DataFrame({'A': [100,102,101,105], 'B': [50,51,50,52]}, index=pd.date_range('2024-01-01', periods=4, freq='1h'))
positions = pd.DataFrame({'A': [0.5]*4, 'B': [-0.5]*4}, index=prices.index)
funding = prices * 0 + 0.0001
backtest = run_backtest(prices, positions, BacktestConfig(funding_rates=funding))
tiers = run_multi_leverage_backtest(prices, positions, leverage_tiers=(1.0, 2.0))
backtest.equity_curve.to_csv('equity.csv')
json.dump({'stats': backtest.stats, 'tiers': list(tiers), 'undefined_metric': float('nan')}, open('result.json','w'), default=str)
"""
        + (
            "raise RuntimeError('test interrupted research')\n"
            if failure
            else "raise SystemExit(0)\n"
        )
    )
    archive, output, artifacts = (
        tmp_path / "workspace.tar.gz",
        tmp_path / "summary.json",
        tmp_path / "artifacts.tar.gz",
    )
    pack_job(
        store,
        job_id,
        archive,
        op="script",
        options={"path": str(script.relative_to(store.repo_root))},
    )
    proc = _run_runtime(archive, tmp_path)
    assert proc.returncode == (1 if failure else 0), proc.stderr
    extract_archive(artifacts, tmp_path / "collected")
    assert (tmp_path / "collected/equity.csv").is_file()
    summary = json.loads(output.read_text())
    json.dumps(summary, allow_nan=False)
    if failure:
        assert summary["error"] == "test interrupted research"
        assert (tmp_path / "collected/operation-error.json").is_file()
    else:
        assert summary["summary"]["undefined_metric"] is None
        assert summary["summary"]["tiers"] == ["1x", "2x"]
        assert "NaN" in (tmp_path / "collected/operation-result.json").read_text()
