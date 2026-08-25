from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.jobs.factors import (
    blend_factor_scores,
    cross_sectional_rank,
    cross_sectional_robust_zscore,
    panel_from_frames,
    residual_return,
    rolling_beta,
)
from wayfinder_paths.jobs.research import factor_rank_holdout, factor_rank_scan


def test_panel_from_frames_aligns_requested_numeric_columns() -> None:
    frames = {
        "B": pd.DataFrame({"timestamp": ["2026-01-01T04:00:00Z"], "close": ["2.0"]}),
        "A": pd.DataFrame({"timestamp": ["2026-01-01T00:00:00Z"], "close": [1.0]}),
    }

    panel = panel_from_frames(frames, "close", symbols=("A", "B", "missing"))

    assert list(panel.columns) == ["A", "B"]
    assert str(panel.index.tz) == "UTC"
    assert panel.loc[pd.Timestamp("2026-01-01T00:00:00Z"), "A"] == 1.0
    assert panel.loc[pd.Timestamp("2026-01-01T04:00:00Z"), "B"] == 2.0


def test_cross_sectional_rank_is_symmetric_and_respects_eligibility() -> None:
    values = pd.DataFrame([[1.0, 2.0, 3.0], [5.0, 5.0, 9.0]], columns=["A", "B", "C"])
    eligible = pd.DataFrame(
        [[True, True, True], [True, True, False]], columns=values.columns
    )
    ranked = cross_sectional_rank(values, eligible)
    assert ranked.iloc[0].tolist() == pytest.approx([-1.0, 0.0, 1.0])
    assert ranked.iloc[1, :2].tolist() == pytest.approx([0.0, 0.0])
    assert pd.isna(ranked.iloc[1, 2])


def test_cross_sectional_robust_zscore_is_row_local_and_clipped() -> None:
    values = pd.DataFrame(
        [[1.0, 2.0, 100.0], [10.0, 20.0, 30.0]], columns=["A", "B", "C"]
    )
    score = cross_sectional_robust_zscore(values, clip=2.0)
    assert score.iloc[0, 2] == 2.0
    assert score.iloc[1, 1] == pytest.approx(0.0)
    changed = values.copy()
    changed.iloc[1] = [1_000.0, 2_000.0, 3_000.0]
    pd.testing.assert_series_equal(
        cross_sectional_robust_zscore(changed).iloc[0],
        cross_sectional_robust_zscore(values).iloc[0],
    )


def test_rolling_beta_and_residual_return_are_causal() -> None:
    benchmark_returns = pd.Series([0.01, -0.02, 0.03, 0.01, -0.01, 0.02])
    asset_returns = pd.DataFrame(
        {"A": 2.0 * benchmark_returns, "B": -benchmark_returns}
    )
    beta = rolling_beta(asset_returns, benchmark_returns, 4, clip=None)
    assert beta.iloc[-1].tolist() == pytest.approx([2.0, -1.0])

    benchmark_close = (1.0 + benchmark_returns).cumprod() * 100
    close = pd.DataFrame(
        {
            "A": (1.0 + 2.0 * benchmark_returns).cumprod() * 50,
            "B": (1.0 - benchmark_returns).cumprod() * 80,
        }
    )
    residual = residual_return(
        close,
        benchmark_close,
        1,
        beta_period=4,
        beta_min_periods=4,
        beta_clip=None,
    )
    assert residual.iloc[-1].tolist() == pytest.approx([0.0, 0.0], abs=1e-12)

    extended = pd.concat([close, pd.DataFrame({"A": [9_999.0], "B": [1.0]})])
    benchmark_extended = pd.concat([benchmark_close, pd.Series([500.0])])
    pd.testing.assert_frame_equal(
        residual_return(
            extended,
            benchmark_extended,
            1,
            beta_period=4,
            beta_min_periods=4,
            beta_clip=None,
        ).iloc[: len(close)],
        residual,
    )


def test_blend_factor_scores_validates_and_reranks() -> None:
    momentum = pd.DataFrame([[1.0, 0.0, -1.0]], columns=["A", "B", "C"])
    quality = pd.DataFrame([[0.0, 1.0, -1.0]], columns=momentum.columns)
    blended = blend_factor_scores(
        {"momentum": momentum, "quality": quality},
        {"momentum": 3.0, "quality": 1.0},
    )
    assert blended.iloc[0].tolist() == pytest.approx([1.0, 0.0, -1.0])
    with pytest.raises(ValueError, match="must match"):
        blend_factor_scores({"momentum": momentum}, {"quality": 1.0})


def _predictive_factor_frames() -> dict[str, pd.DataFrame]:
    symbols = [f"S{index}" for index in range(8)]
    timestamps = pd.date_range("2025-01-01", periods=260, freq="4h", tz="UTC")
    alpha = np.random.default_rng(17).normal(size=(len(timestamps), len(symbols)))
    noise = np.random.default_rng(4).normal(size=alpha.shape)
    outcome_rng = np.random.default_rng(31)
    opens = np.full(alpha.shape, 100.0)
    for row in range(len(timestamps) - 2):
        outcome = 0.004 * alpha[row] + outcome_rng.normal(0.0, 0.003, len(symbols))
        opens[row + 2] = opens[row + 1] * (1.0 + outcome)
    return {
        symbol: pd.DataFrame(
            {
                "timestamp": timestamps,
                "symbol": symbol,
                "open": opens[:, index],
                "high": opens[:, index] * 1.001,
                "low": opens[:, index] * 0.999,
                "close": opens[:, index],
                "volume": 1_000.0,
                "alpha": alpha[:, index],
                "noise": noise[:, index],
            }
        )
        for index, symbol in enumerate(symbols)
    }


def test_factor_rank_scan_pools_family_and_keeps_tail_closed() -> None:
    report = factor_rank_scan(
        _predictive_factor_frames(),
        ["alpha", "noise"],
        horizons=[1],
        holdout_fraction=0.2,
    )
    assert report["holdout"]["opened"] is False
    assert report["tests_run"] == 2
    alpha = next(row for row in report["results"] if row["column"] == "alpha")
    noise = next(row for row in report["results"] if row["column"] == "noise")
    assert alpha["orientation"] == "high_score_outperforms"
    assert alpha["folds_agreeing"] == 4
    assert alpha["passed"] is True
    assert noise["passed"] is False


def test_factor_rank_holdout_checks_frozen_orientation() -> None:
    report = factor_rank_holdout(
        _predictive_factor_frames(),
        "alpha",
        1,
        "high_score_outperforms",
        holdout_fraction=0.2,
    )
    assert report["n"] >= 10
    assert report["mean_ic"] > 0
    assert report["verdict"] == "confirm"


def test_factor_jobs_persist_scan_and_do_not_recompute_spent_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    import wayfinder_paths.jobs.research as research_module
    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("factor-demo", agent_mode="intervene")
    store.save(job)
    root = store.job_dir(job.id)
    frames = _predictive_factor_frames()
    monkeypatch.setattr(
        research_module,
        "_precomputed_job_frames",
        lambda _job_id, _store: (root, sorted(frames), frames),
    )
    scan = research_module.factor_scan_job(
        job.id,
        columns=["alpha", "noise"],
        horizons=[1],
        holdout_fraction=0.2,
        store=store,
    )
    assert scan["passed"]
    persisted = json.loads(
        (root / "results/research/factor_scan.json").read_text(encoding="utf-8")
    )
    assert persisted["holdout"]["opened"] is False
    with pytest.raises(ValueError, match="passed the persisted factor scan"):
        research_module.factor_holdout_check_job(
            job.id,
            column="unseen",
            horizon=1,
            orientation="high_score_outperforms",
            holdout_fraction=0.2,
            store=store,
        )

    first = research_module.factor_holdout_check_job(
        job.id,
        column="alpha",
        horizon=1,
        orientation="high_score_outperforms",
        holdout_fraction=0.2,
        store=store,
    )
    monkeypatch.setattr(
        research_module,
        "_precomputed_job_frames",
        lambda *_args: (_ for _ in ()).throw(AssertionError("tail recomputed")),
    )
    second = research_module.factor_holdout_check_job(
        job.id,
        column="alpha",
        horizon=1,
        orientation="high_score_outperforms",
        holdout_fraction=0.2,
        store=store,
    )
    assert first["already_spent"] is False
    assert first["cutoff_ts"] == persisted["holdout"]["cutoff_ts"]
    assert first["confirmation_threshold"] == {
        "min_n": 10,
        "one_sided_t": 1.645,
    }
    assert second["already_spent"] is True
    assert second["mean_ic"] == first["mean_ic"]


def test_factor_cli_routes_declared_family(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from wayfinder_paths.jobs import cli as cli_module

    captured: dict[str, object] = {}

    def fake_scan(job_id: str, **kwargs: object) -> dict[str, list[object]]:
        captured.update({"job_id": job_id, **kwargs})
        return {"passed": []}

    monkeypatch.setattr(cli_module, "factor_scan_job", fake_scan)
    result = CliRunner().invoke(
        cli_module.job_cli,
        [
            "factor-scan",
            "demo",
            "--columns",
            "momentum,carry",
            "--horizons",
            "4,24",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["job_id"] == "demo"
    assert captured["columns"] == ["momentum", "carry"]
    assert captured["horizons"] == [4, 24]

    def fake_holdout(job_id: str, **kwargs: object) -> dict[str, str]:
        captured.clear()
        captured.update({"job_id": job_id, **kwargs})
        return {"verdict": "confirm"}

    monkeypatch.setattr(cli_module, "factor_holdout_check_job", fake_holdout)
    result = CliRunner().invoke(
        cli_module.job_cli,
        [
            "factor-holdout",
            "demo",
            "--column",
            "momentum",
            "--horizon",
            "4",
            "--orientation",
            "high_score_outperforms",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["job_id"] == "demo"
    assert captured["column"] == "momentum"
    assert captured["horizon"] == 4
    assert captured["orientation"] == "high_score_outperforms"


@pytest.mark.asyncio
async def test_core_jobs_routes_factor_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    from wayfinder_paths.mcp.tools import jobs as jobs_module

    captured: dict[str, object] = {}

    async def fake_run(op: str, kwargs: dict[str, object]) -> dict[str, object]:
        captured.update({"op": op, "kwargs": kwargs})
        return {"ok": True, "result": {"passed": []}}

    monkeypatch.setattr(jobs_module, "_run_job_op", fake_run)
    result = await jobs_module.core_jobs(
        action="factor_scan",
        job_id="demo",
        columns=["momentum", "carry"],
        horizons=[4, 24],
    )
    assert result["ok"] is True
    assert captured["op"] == "factor_scan"
    assert captured["kwargs"] == {
        "job_id": "demo",
        "columns": ["momentum", "carry"],
        "horizons": [4, 24],
        "holdout_fraction": 0.15,
    }

    result = await jobs_module.core_jobs(
        action="factor_holdout",
        job_id="demo",
        column="momentum",
        horizon=4,
        orientation="high_score_outperforms",
    )
    assert result["ok"] is True
    assert captured["op"] == "factor_holdout"
    assert captured["kwargs"] == {
        "job_id": "demo",
        "column": "momentum",
        "horizon": 4,
        "orientation": "high_score_outperforms",
        "holdout_fraction": 0.15,
    }
