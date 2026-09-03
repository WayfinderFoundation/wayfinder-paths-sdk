from __future__ import annotations

import asyncio
import json

import pandas as pd
import yaml

from wayfinder_paths.jobs.candidate_shadow import (
    candidate_shadow_lookback_bars,
    run_candidate_shadows,
)
from wayfinder_paths.jobs.execution.primitives import CompletedBarsView
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.probation import (
    load_probation,
    stage_evolution_probation,
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
    revision = compute_workspace_revision(root)
    staged = stage_evolution_probation(
        store,
        job.id,
        candidate_id="candidate-1",
        candidate_root=root,
        revision=revision,
        source="evolution_campaign",
        family="test-candidate",
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
    assert {row["role"] for row in first} == {"candidate", "reference"}
    updated = load_probation(store, job.id)["trials"][0]
    assert updated["trial_id"] == staged["trial_id"]
    assert updated["status"] == "active"
    assert updated["burn_in"]["status"] == "passed"
    assert (
        len(list((root / "state" / "probation_shadows").rglob("engine_state.json")))
        >= 2
    )
    ticks_paths = list(
        (root / "results" / "forward" / "probation").rglob("ticks.jsonl")
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


def test_regime_shadow_requests_classifier_history_from_shared_feed(tmp_path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("shadow-regime-depth", script="workspace/strategy.py")
    store.save(job)
    root = store.job_dir(job.id)
    script = root / "workspace/strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("def decide(ctx):\n    return []\n", encoding="utf-8")
    (root / "job.yaml").write_text(
        yaml.safe_dump(
            {
                "id": job.id,
                "execution_contract": "jobs_v1",
                "script_loop": {
                    "enabled": True,
                    "entrypoint": "workspace/strategy.py",
                },
                "execution_params": {
                    "symbols": ["BTC"],
                    "warmup_bars": 20,
                    "target_regimes": ["up_lowvol"],
                    "defense_overlay": {},
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "execution_spec.json").write_text(
        json.dumps(
            {
                "data_contract": {
                    "bar_interval": "5m",
                    "symbols": ["BTC"],
                }
            }
        ),
        encoding="utf-8",
    )
    revision = compute_workspace_revision(root)
    stage_evolution_probation(
        store,
        job.id,
        candidate_id="specialist",
        candidate_root=root,
        revision=revision,
        source="evolution_campaign",
        family="regime-specialist",
        now=pd.Timestamp("2026-08-24T12:00:00Z").to_pydatetime(),
    )

    assert candidate_shadow_lookback_bars(store, job.id) == 690


def test_candidate_shadow_merges_the_features_the_candidate_declares(tmp_path) -> None:
    """A feature-aware candidate reads its declared derived column in the
    shadow lane from the job store, defaulting until the first row exists."""
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
        "        macro = ctx.view.feature('macro_regime', default=0.0)\n"
        "        if str(ctx.timestamp) >= '2026-08-25T11' and macro != 1.0:\n"
        "            raise ValueError('declared feature never reached the shadow')\n"
        "        if macro != 1.0 or ctx.ledger.positions:\n"
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
                        "features": [{"name": "macro_regime", "source": "file"}],
                    },
                    "venues": ["hyperliquid"],
                },
                "execution_params": {"symbols": ["BTC"], "lookback_bars": 20},
            }
        ),
        encoding="utf-8",
    )
    # The store turns bull half-way through the replay; before that the
    # default carries the strategy.
    turned = started + pd.Timedelta(hours=18)
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "state" / "features.jsonl").write_text(
        json.dumps(
            {
                "timestamp": turned.isoformat(),
                "name": "macro_regime",
                "value": 1.0,
                "symbol": "BTC",
                "written_at": turned.isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    revision = compute_workspace_revision(root)
    stage_evolution_probation(
        store,
        job.id,
        candidate_id="candidate-1",
        candidate_root=root,
        revision=revision,
        source="evolution_campaign",
        family="feature-aware",
        evidence={"objective": {"candidate": {"trade_count": 12}}},
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
    rows = asyncio.run(
        run_candidate_shadows(
            store, job.id, view=view, now=pd.Timestamp("2026-08-25T12:00:00Z")
        )
    )
    assert {row["role"] for row in rows} == {"candidate", "reference"}
    ticks_paths = list(
        (root / "results" / "forward" / "probation").rglob("ticks.jsonl")
    )
    assert ticks_paths
    last_bars = [
        max(
            pd.Timestamp(json.loads(line)["bar_ts"])
            for line in path.read_text(encoding="utf-8").splitlines()
        )
        for path in ticks_paths
    ]
    # Every stream replayed through the last bar, past the point where an
    # unmerged feature would have raised.
    assert all(last >= started + pd.Timedelta(hours=24) for last in last_bars)


def _feature_candidate_job(
    tmp_path, *, feature: dict, strategy_body: str
) -> tuple[JobStore, str, pd.Timestamp, pd.Timestamp, list[dict]]:
    """A job whose incumbent declares ``feature`` and reads it with a default,
    with one store row turning it to 1.0 eighteen hours into a 25-hour
    5-minute replay, and a staged probation candidate of the same bundle."""
    started = pd.Timestamp("2026-08-24T11:00:00Z")
    queued_at = pd.Timestamp("2026-08-24T12:00:00Z")
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("majors-5m-lab", script="workspace/src/strategy.py")
    store.save(job)
    root = store.job_dir(job.id)
    script = root / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(strategy_body, encoding="utf-8")
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
                        "features": [feature],
                    },
                    "venues": ["hyperliquid"],
                },
                "execution_params": {"symbols": ["BTC"], "lookback_bars": 20},
            }
        ),
        encoding="utf-8",
    )
    turned = started + pd.Timedelta(hours=18)
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "state" / "features.jsonl").write_text(
        json.dumps(
            {
                "timestamp": turned.isoformat(),
                "name": "macro_regime",
                "value": 1.0,
                "symbol": "BTC",
                "written_at": turned.isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    stage_evolution_probation(
        store,
        job.id,
        candidate_id="candidate-1",
        candidate_root=root,
        revision=compute_workspace_revision(root),
        source="evolution_campaign",
        family="feature-aware",
        evidence={"objective": {"candidate": {"trade_count": 12}}},
        now=queued_at.to_pydatetime(),
    )
    bars = [
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
    return store, job.id, started, turned, bars


_READS_MACRO = (
    "def decide(ctx):\n"
    "    macro = ctx.view.feature('macro_regime', default=0.0)\n"
    "    if str(ctx.timestamp) >= '2026-08-25T11' and macro != 1.0:\n"
    "        raise ValueError('the candidate did not read its own merge')\n"
    "    return []\n"
)


def test_candidate_shadow_skips_stale_declared_features_like_the_driver(
    tmp_path,
) -> None:
    store, job_id, started, turned, bars = _feature_candidate_job(
        tmp_path,
        feature={
            "name": "macro_regime",
            "source": "file",
            "max_age_seconds": 3600,
            "stale_policy": "skip",
        },
        strategy_body="def decide(ctx):\n    return []\n",
    )
    rows = asyncio.run(
        run_candidate_shadows(
            store,
            job_id,
            view=CompletedBarsView.from_rows(bars),
            now=pd.Timestamp("2026-08-25T12:00:00Z"),
        )
    )
    candidate = [row for row in rows if row["role"] == "candidate"]
    assert candidate
    fresh = [
        row
        for row in candidate
        if turned
        <= pd.Timestamp(row["bar_timestamp"])
        <= turned + pd.Timedelta(hours=1)
    ]
    stale = [
        row
        for row in candidate
        if pd.Timestamp(row["bar_timestamp"]) > turned + pd.Timedelta(hours=1)
    ]
    assert fresh and stale
    assert not any(row["skipped"] for row in fresh)
    assert all(row["skipped"] for row in stale)


def test_candidate_shadow_drops_a_queued_column_the_candidate_declares_itself(
    tmp_path,
) -> None:
    """A queued view already carrying the incumbent's column under the same
    name (views queued before the driver stopped merging it) must not
    collide: the candidate's own merge wins."""
    store, job_id, started, turned, bars = _feature_candidate_job(
        tmp_path,
        feature={"name": "macro_regime", "source": "file"},
        strategy_body=_READS_MACRO,
    )
    stale_rows = [{**row, "macro_regime": -1.0} for row in bars]
    rows = asyncio.run(
        run_candidate_shadows(
            store,
            job_id,
            view=CompletedBarsView.from_rows(stale_rows),
            now=pd.Timestamp("2026-08-25T12:00:00Z"),
        )
    )
    candidate = [row for row in rows if row["role"] == "candidate"]
    assert candidate and not any(row["skipped"] for row in candidate)
    assert max(
        pd.Timestamp(row["bar_timestamp"]) for row in candidate
    ) >= started + pd.Timedelta(hours=24)
