from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.paper_experiment import (
    admit_paper_candidate,
    ensure_paper_experiment,
    experiment_status,
    maybe_finalize_experiment,
)
from wayfinder_paths.jobs.probation import load_probation
from wayfinder_paths.jobs.store import JobStore


def _job(tmp_path: Path, *, now: datetime) -> tuple[JobStore, str, dict]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("majors-5m-lab", script="workspace/src/strategy.py")
    store.save(job)
    script = store.job_dir(job.id) / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("def decide(ctx):\n    return []\n", encoding="utf-8")
    state = ensure_paper_experiment(store, job.id, now=now)
    assert state is not None
    return store, job.id, state


def _write_days(
    store: JobStore,
    job_id: str,
    state: dict,
    *,
    evolution_pnl: float,
) -> None:
    for arm in ("control", "evolution"):
        champion = state["arms"][arm]["champion"]
        stream = store.job_dir(job_id) / champion["stream"]
        stream.mkdir(parents=True, exist_ok=True)
        ticks = []
        trades = []
        for offset in range(14):
            stamp = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(days=offset)
            ticks.append(json.dumps({"bar_ts": stamp.isoformat()}))
            if arm == "evolution" and evolution_pnl:
                trades.append(
                    json.dumps(
                        {"timestamp": stamp.isoformat(), "net_pnl": evolution_pnl}
                    )
                )
        (stream / "ticks.jsonl").write_text("\n".join(ticks) + "\n", encoding="utf-8")
        if trades:
            (stream / "trades.jsonl").write_text(
                "\n".join(trades) + "\n", encoding="utf-8"
            )


@pytest.mark.parametrize(
    ("evolution_pnl", "expected"),
    [(10.0, "accrete"), (-10.0, "kill"), (0.0, "inconclusive")],
)
def test_fixed_horizon_paper_verdict_is_pre_registered_and_paper_only(
    tmp_path: Path, evolution_pnl: float, expected: str
) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store, job_id, state = _job(tmp_path, now=started)
    original_job = (store.job_dir(job_id) / "job.yaml").read_bytes()
    assert state["protocol"]["primary_endpoint"] == (
        "paired_daily_forward_log_utility_delta_lcb"
    )
    assert state["protocol"]["paper_only"] is True
    assert (
        state["arms"]["control"]["champion"]["stream"]
        != (state["arms"]["evolution"]["champion"]["stream"])
    )
    _write_days(store, job_id, state, evolution_pnl=evolution_pnl)

    report = maybe_finalize_experiment(store, job_id, now=started + timedelta(days=14))

    assert report is not None
    assert report["verdict"] == expected
    assert report["paired_days"] == 14
    assert report["paper_only"] is True
    assert experiment_status(store, job_id)["status"] == "complete"
    assert (store.job_dir(job_id) / "job.yaml").read_bytes() == original_job


def test_candidate_admission_replaces_only_one_isolated_paper_champion(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store, job_id, state = _job(tmp_path, now=started)
    root = store.job_dir(job_id)
    source = root / "candidate-source"
    (source / "workspace" / "src").mkdir(parents=True)
    (source / "workspace" / "src" / "strategy.py").write_text(
        "def decide(ctx):\n    return []\n", encoding="utf-8"
    )
    (source / "job.yaml").write_bytes((root / "job.yaml").read_bytes())
    control_before = dict(state["arms"]["control"]["champion"])
    with pytest.raises(ValueError, match="path-safe"):
        admit_paper_candidate(
            store,
            job_id,
            arm="evolution",
            candidate_id="candidate-escape",
            candidate_root=source,
            revision="../../escape",
            source="evolution_campaign",
            now=started + timedelta(minutes=30),
        )

    admitted = admit_paper_candidate(
        store,
        job_id,
        arm="evolution",
        candidate_id="candidate-1",
        candidate_root=source,
        revision="candidate-revision",
        source="evolution_campaign",
        now=started + timedelta(hours=1),
    )

    updated = experiment_status(store, job_id)
    assert updated["arms"]["control"]["champion"] == control_before
    assert updated["arms"]["evolution"]["champion"] == admitted
    assert admitted["stream"].startswith("results/forward/experiment/evolution/")
    assert load_probation(store, job_id).get("legs") == []
    copied = root / admitted["bundle"] / "workspace" / "src" / "strategy.py"
    source.joinpath("workspace/src/strategy.py").write_text(
        "raise RuntimeError('changed source')\n", encoding="utf-8"
    )
    assert "changed source" not in copied.read_text(encoding="utf-8")


def test_experiment_retires_only_legacy_evolution_probation(tmp_path: Path) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("majors-5m-lab", script="workspace/src/strategy.py")
    store.save(job)
    script = store.job_dir(job.id) / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("def decide(ctx):\n    return []\n", encoding="utf-8")
    leg_template = {
        "symbol": "portfolio",
        "status": "active",
        "tier": "paper",
        "graduate": {"criterion": "paper evidence", "progress": None},
        "kill": {"criterion": "safety breach", "status": None},
    }
    store.write_json(
        job.id,
        "probation.json",
        {
            "legs": [
                {
                    **leg_template,
                    "name": "ordinary-funnel-probation",
                    "candidate_bundle_id": "funnel-candidate",
                },
                {
                    **leg_template,
                    "name": "legacy-evolution-probation",
                    "candidate_bundle_id": "evolution-candidate",
                    "campaign_id": "campaign-1",
                },
            ]
        },
    )

    assert ensure_paper_experiment(store, job.id, now=started) is not None

    legs = {
        leg["name"]: leg for leg in load_probation(store, job.id).get("legs") or []
    }
    assert legs["ordinary-funnel-probation"]["status"] == "active"
    assert legs["legacy-evolution-probation"]["status"] == "killed"
    assert (
        legs["legacy-evolution-probation"]["graduate"]["progress"]
        == "migrated to the isolated paper A/B rail"
    )
