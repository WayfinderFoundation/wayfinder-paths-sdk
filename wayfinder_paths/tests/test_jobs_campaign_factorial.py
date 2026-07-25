"""Campaign-scoped scan families and factorial attribution."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.execution.simulator import grid_factor_attribution
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.research import signal_scan_job
from wayfinder_paths.jobs.signal_library import SIGNAL_LIBRARY, build_signal_frame
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.workspace_signals import SignalDef


def _frame(count: int = 200) -> pd.DataFrame:
    base = pd.Timestamp("2026-07-01T00:00:00Z")
    rows = []
    for i in range(count):
        price = 100 + 5 * math.sin(i / 9)
        rows.append(
            {
                "timestamp": (base + pd.Timedelta(minutes=5 * i)).isoformat(),
                "symbol": "LIT",
                "open": price,
                "high": price + 0.4,
                "low": price - 0.4,
                "close": price + 0.1,
                "volume": 10.0,
            }
        )
    return pd.DataFrame(rows)


def test_campaign_frame_excludes_canonical_library() -> None:
    frame = _frame()
    extra = [
        SignalDef(
            name="camp_up",
            family="campaign",
            description="close above 20-bar mean",
            min_bars=20,
            build=lambda f: f["close"].astype(float)
            > f["close"].astype(float).rolling(20).mean(),
        )
    ]
    full = build_signal_frame(frame, extra)
    assert "camp_up" in full.columns and len(full.columns) == len(SIGNAL_LIBRARY) + 1
    campaign_only = build_signal_frame(frame, extra, include_canonical=False)
    assert list(campaign_only.columns) == ["camp_up"]


def test_campaign_scan_requires_workspace_defs(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "camp-demo",
        script=".wayfinder/jobs/camp-demo/workspace/src/strategy.py",
        interval_seconds=300,
    )
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "5m"
    job.execution_spec = spec.to_dict()
    store.save(job)
    root = store.job_dir(job.id)
    bars_path = root / "results" / "backtest" / "input_bars.json"
    bars_path.parent.mkdir(parents=True, exist_ok=True)
    bars_path.write_text(json.dumps(_frame(400).to_dict("records")), encoding="utf-8")

    with pytest.raises(ValueError, match="campaign scan needs workspace signals"):
        signal_scan_job(job.id, campaign="volume_v1", store=store)


def _cell(params: dict, metric: float) -> dict:
    return {
        "run_id": "r",
        "params": params,
        "stats": {"net_return": metric},
        "validation": {"execution_valid": True},
        "net_return": metric,
    }


def test_factor_attribution_marginals_and_interaction() -> None:
    # 2x2: exit change carries the improvement; the gate only helps when the
    # filter is on (sign flip).
    rows = [
        _cell({"gate": False, "mfe_target": 0}, 0.10),
        _cell({"gate": True, "mfe_target": 0}, 0.06),
        _cell({"gate": False, "mfe_target": 60}, 0.20),
        _cell({"gate": True, "mfe_target": 60}, 0.26),
    ]
    grid = {"gate": [False, True], "mfe_target": [0, 60]}
    result = grid_factor_attribution(rows, grid, rank_by="net_return")
    assert result is not None
    assert result["factors"]["mfe_target"]["marginal_effect"] == pytest.approx(0.15)
    assert result["factors"]["gate"]["marginal_effect"] == pytest.approx(0.01)
    interaction = next(i for i in result["interactions"] if i["factor"] == "gate")
    assert interaction["sign_flip"] is True
    assert result["top_params"] == {"gate": True, "mfe_target": 60}

    # No swept axis -> None
    assert grid_factor_attribution(rows, {"gate": [True]}, rank_by="net_return") is None
