from __future__ import annotations

import asyncio

import pandas as pd
import yaml

from wayfinder_paths.jobs.candidate_shadow import run_candidate_shadows
from wayfinder_paths.jobs.execution.primitives import CompletedBarsView
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def test_candidate_shadow_uses_separate_paper_state_and_stream(tmp_path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("shadow-job", script="workspace/src/strategy.py")
    store.save(job)
    root = store.job_dir(job.id)
    relative = "research/evolution/campaigns/camp/candidates/candidate-1"
    candidate = root / relative
    (candidate / "workspace" / "src").mkdir(parents=True)
    (candidate / "workspace" / "src" / "strategy.py").write_text(
        "class Strategy:\n"
        "    def decide(self, ctx):\n"
        "        return []\n\n"
        "def build_strategy(params):\n"
        "    return Strategy()\n",
        encoding="utf-8",
    )
    (candidate / "job.yaml").write_text(
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
    store.write_json(
        job.id,
        "probation.json",
        {
            "legs": [
                {
                    "name": "candidate-1",
                    "tier": "paper",
                    "status": "active",
                    "candidate_bundle_id": "candidate-1",
                    "candidate_bundle": relative,
                    "candidate_revision": "rev-candidate",
                    "shadow_stream": "results/forward/shadows/candidate-1",
                }
            ]
        },
    )
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
    result = asyncio.run(
        run_candidate_shadows(
            store,
            job.id,
            view=view,
            now=pd.Timestamp("2026-08-25T12:00:00Z"),
        )
    )
    assert result == [
        {"candidate_id": "candidate-1", "skipped": False, "intents": 0, "fills": 0}
    ]
    assert (
        root / "state" / "evolution_shadows" / "candidate-1" / "engine_state.json"
    ).exists()
    assert (
        root / "results" / "forward" / "shadows" / "candidate-1" / "ticks.jsonl"
    ).exists()
    assert not (root / "results" / "forward" / "orders.jsonl").read_text()
