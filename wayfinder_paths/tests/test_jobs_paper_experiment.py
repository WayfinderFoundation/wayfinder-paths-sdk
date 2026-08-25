from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.paper_experiment import (
    _resource_cost,
    ensure_paper_experiment,
    experiment_status,
    maybe_finalize_experiment,
    record_evidence,
    stage_paper_proposal,
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
    state["status"] = "active"
    state["started_at"] = started.isoformat()
    state["ends_at"] = (started + timedelta(days=14)).isoformat()
    store.write_json(job_id, "state/evolution_experiment.json", state)
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


def test_candidate_is_frozen_but_does_not_replace_champion_before_forward_day(
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
    revision = compute_workspace_revision(source)
    with pytest.raises(ValueError, match="path-safe"):
        stage_paper_proposal(
            store,
            job_id,
            arm="evolution",
            candidate_id="candidate-escape",
            candidate_root=source,
            revision="../../escape",
            source="evolution_campaign",
            now=started + timedelta(minutes=30),
        )

    staged = stage_paper_proposal(
        store,
        job_id,
        arm="evolution",
        candidate_id="candidate-1",
        candidate_root=source,
        revision=revision,
        source="evolution_campaign",
        now=started + timedelta(hours=1),
    )

    updated = experiment_status(store, job_id)
    assert updated["arms"]["control"]["champion"] == control_before
    assert (
        updated["arms"]["evolution"]["champion"]
        == state["arms"]["evolution"]["champion"]
    )
    assert staged["candidate"]["stream"].startswith(
        "results/forward/experiment/proposals/evolution/"
    )
    assert load_probation(store, job_id).get("legs") == []
    copied = root / staged["bundle"] / "workspace" / "src" / "strategy.py"
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

    legs = {leg["name"]: leg for leg in load_probation(store, job.id).get("legs") or []}
    assert legs["ordinary-funnel-probation"]["status"] == "active"
    assert legs["legacy-evolution-probation"]["status"] == "killed"
    assert (
        legs["legacy-evolution-probation"]["graduate"]["progress"]
        == "migrated to the isolated paper A/B rail"
    )


def test_qualification_deadline_closes_without_an_evolution_survivor(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store, job_id, _ = _job(tmp_path, now=started)

    report = maybe_finalize_experiment(store, job_id, now=started + timedelta(days=7))

    assert report is not None
    assert report["verdict"] == "kill"
    assert report["reason"] == "no_qualifier"
    assert experiment_status(store, job_id)["status"] == "complete"


def test_resource_meter_uses_arm_local_finalist_costs(tmp_path: Path) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store, job_id, state = _job(tmp_path, now=started)
    for arm in ("control", "evolution"):
        record_evidence(
            store,
            job_id,
            arm=arm,
            candidate_id=f"{arm}-candidate",
            revision=f"{arm}-revision",
            admitted=False,
            evidence={
                "token_usage": {"tokens_in": 80, "tokens_out": 20},
                "sim_wall_seconds": 60,
            },
        )

    costs = _resource_cost(store, job_id, state)
    assert costs["control"]["tokens"] == 100
    assert costs["evolution"]["tokens"] == 100
    assert costs["control"]["sim_wall_seconds"] == 60
    assert costs["evolution"]["sim_wall_seconds"] == 60
