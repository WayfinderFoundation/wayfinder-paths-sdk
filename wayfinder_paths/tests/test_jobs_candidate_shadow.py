from __future__ import annotations

import asyncio

import pandas as pd
import yaml

from wayfinder_paths.jobs.candidate_shadow import run_candidate_shadows
from wayfinder_paths.jobs.execution.primitives import CompletedBarsView
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.paper_experiment import ensure_paper_experiment
from wayfinder_paths.jobs.store import JobStore


def test_candidate_shadow_uses_separate_paper_state_and_stream(tmp_path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("majors-5m-lab", script="workspace/src/strategy.py")
    store.save(job)
    root = store.job_dir(job.id)
    script = root / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "class Strategy:\n"
        "    def decide(self, ctx):\n"
        "        return []\n\n"
        "def build_strategy(params):\n"
        "    return Strategy()\n",
        encoding="utf-8",
    )
    (root / "job.yaml").write_text(
        yaml.safe_dump(
            {
                "id": job.id,
                "execution_contract": "jobs_v1",
                "script_loop": {
                    "enabled": True,
                    "entrypoint": "workspace/src/strategy.py",
                },
                "execution_spec": {
                    "data_contract": {
                        "bar_interval": "5m",
                        "symbols": ["BTC"],
                        "stale_policy": "decide_anyway",
                    },
                    "venues": ["hyperliquid"],
                },
                "execution_params": {"symbols": ["BTC"], "lookback_bars": 20},
            }
        ),
        encoding="utf-8",
    )
    experiment = ensure_paper_experiment(store, job.id)
    assert experiment is not None
    revision = experiment["initial_revision"]
    view = CompletedBarsView.from_rows(
        [
            {
                "timestamp": "2026-08-25T11:55:00Z",
                "symbol": "BTC",
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 10,
            }
        ]
    )
    first = asyncio.run(
        run_candidate_shadows(
            store,
            job.id,
            view=view,
            now=pd.Timestamp("2026-08-25T12:00:00Z"),
        )
    )
    assert [row["arm"] for row in first] == ["control", "evolution"]
    assert all(row["intents"] == row["fills"] == 0 for row in first)
    for arm in ("control", "evolution"):
        assert (
            root / "state" / "evolution_shadows" / arm / revision / "engine_state.json"
        ).exists()
        assert (
            root / "results" / "forward" / "experiment" / arm / revision / "ticks.jsonl"
        ).exists()
    assert asyncio.run(run_candidate_shadows(store, job.id)) == []
