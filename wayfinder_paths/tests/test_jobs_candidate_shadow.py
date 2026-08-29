from __future__ import annotations

import asyncio
import json

import pandas as pd
import yaml

from wayfinder_paths.jobs.candidate_shadow import run_candidate_shadows
from wayfinder_paths.jobs.execution.primitives import CompletedBarsView
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.paper_experiment import (
    ensure_paper_experiment,
    stage_paper_proposal,
)
from wayfinder_paths.jobs.store import JobStore


def test_candidate_shadow_uses_separate_paper_state_and_stream(tmp_path) -> None:
    started = pd.Timestamp("2026-08-24T11:00:00Z")
    queued_at = pd.Timestamp("2026-08-24T12:00:00Z")
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("majors-5m-lab", script="workspace/src/strategy.py")
    store.save(job)
    root = store.job_dir(job.id)
    script = root / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "from wayfinder_paths.jobs.execution import OrderIntent\n\n"
        "class Strategy:\n"
        "    def decide(self, ctx):\n"
        "        if ctx.ledger.positions:\n"
        "            return []\n"
        "        latest = ctx.view.latest('BTC')\n"
        "        return [OrderIntent(action='OPEN', venue='hyperliquid', "
        "symbol='BTC', side='long', size=1, "
        "bracket={'take_profit': latest['close'] + 0.5})]\n\n"
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
    experiment = ensure_paper_experiment(store, job.id, now=started.to_pydatetime())
    assert experiment is not None
    revision = experiment["initial_revision"]
    stage_paper_proposal(
        store,
        job.id,
        arm="evolution",
        candidate_id="candidate-1",
        candidate_root=root,
        revision=revision,
        source="evolution_campaign",
        evidence={
            "objective": {"candidate": {"trade_count": 12}},
        },
        now=queued_at.to_pydatetime(),
    )
    view = CompletedBarsView.from_rows(
        [
            {
                "timestamp": (started + pd.Timedelta(minutes=5 * index)).isoformat(),
                "symbol": "BTC",
                "open": 100 + index * 0.01,
                "high": 101 + index * 0.01,
                "low": 99 + index * 0.01,
                "close": 100 + index * 0.01,
                "volume": 10,
            }
            for index in range(302)
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
    assert {row["role"] for row in first} == {
        "proposal_candidate",
        "proposal_reference",
    }
    updated = store.read_json(job.id, "state/evolution_experiment.json")
    assert updated["status"] == "active"
    assert updated["admissions"]["evolution"] == 1
    assert updated["proposals"]["evolution"]["active"] is None
    assert updated["proposals"]["evolution"]["history"][0]["status"] == "qualified"
    for role in ("proposal_candidate", "proposal_reference"):
        assert (
            root
            / "state"
            / "evolution_shadows"
            / "evolution"
            / role
            / revision
            / "engine_state.json"
        ).exists()
    ticks_paths = list(
        (root / "results" / "forward" / "experiment" / "proposals").rglob("ticks.jsonl")
    )
    assert ticks_paths
    # Shadow replays hand candidates the SAME bounded window the simulator and
    # live driver resolve (lookback_bars=20 here) — never the full growing
    # replay history.
    for ticks_path in ticks_paths:
        rows = [
            json.loads(line)
            for line in ticks_path.read_text(encoding="utf-8").splitlines()
        ]
        assert rows and max(row["view_window"]["rows"] for row in rows) <= 20
        assert min(pd.Timestamp(row["bar_ts"]) for row in rows) > queued_at
    assert asyncio.run(run_candidate_shadows(store, job.id)) == []
