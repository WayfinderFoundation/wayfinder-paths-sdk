"""Evolution on a two-day cadence: eligibility per kind, the next-due stamp
and the probation rows the snapshot carries."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wayfinder_paths.jobs.evolution_view import (
    FREESTYLE_PATH_REASON,
    evolution_snapshot,
    probation_summary,
)
from wayfinder_paths.jobs.improver.spec import ImproverSpec
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def _job(
    tmp_path: Path, job_id: str, contract: str, *, agent_mode: str = "intervene"
) -> tuple[JobStore, WayfinderJob]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        job_id,
        script=f"workspace/src/{job_id.replace('-', '_')}.py",
        interval_seconds=300,
        execution_contract=contract,
        agent_mode=agent_mode,
        source={"kind": "freestyle"} if contract == "freestyle_v1" else None,
    )
    root = store.init_layout(job)
    (root / "workspace" / "src" / f"{job_id.replace('-', '_')}.py").write_text(
        "def tick(ctx):\n    pass\n", encoding="utf-8"
    )
    store.save(job)
    return store, job


def test_default_policy_is_fleet_wide_every_two_days(tmp_path: Path) -> None:
    store, job = _job(tmp_path, "any-strategy", "jobs_v1")
    spec = ImproverSpec.load(store.job_dir(job.id))
    assert spec.evolution_enabled_for("any-strategy") is True
    assert spec.evolution["start_interval_hours"] == 48
    eligibility = spec.evolution_eligibility(store.job_dir(job.id), job.id)
    # eligible by policy; only the canonical dataset is missing on a bare job
    assert eligibility["reasons"] == ["canonical_dataset_missing"]


def test_freestyle_and_path_jobs_never_evolve(tmp_path: Path) -> None:
    store, job = _job(tmp_path, "hormuz", "freestyle_v1")
    spec = ImproverSpec.load(store.job_dir(job.id))
    reasons = spec.evolution_eligibility(store.job_dir(job.id), job.id)["reasons"]
    assert FREESTYLE_PATH_REASON in reasons
    view = evolution_snapshot(store, job.id, job)
    assert view["eligibility"] == {
        "eligible": False,
        "reasons": [FREESTYLE_PATH_REASON],
    }
    assert view["next_due_at"] is None


def test_evolution_snapshot_reports_next_due_from_the_last_campaign(
    tmp_path: Path, monkeypatch
) -> None:
    store, job = _job(tmp_path, "majors", "jobs_v1")
    started = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.campaign_status",
        lambda store_, job_id: {
            "status": "finished",
            "campaign_id": "c1",
            "started_at": started.isoformat(),
        },
    )
    view = evolution_snapshot(store, job.id, job)
    assert view["start_interval_hours"] == 48
    assert view["next_due_at"] == (started + timedelta(hours=48)).isoformat()
    assert view["campaign_status"]["campaign_id"] == "c1"


def test_probation_summary_compacts_each_trial(tmp_path: Path) -> None:
    store, job = _job(tmp_path, "majors", "jobs_v1")
    (store.job_dir(job.id) / "probation.json").write_text(
        json.dumps(
            {
                "legs": [],
                "trials": [
                    {
                        "trial_id": "t1",
                        "family": "momentum",
                        "candidate_id": "c-1",
                        "source": "evolution_campaign",
                        "status": "active",
                        "phase": "forward",
                        "burn_in": {"capital": 100.0},
                        "forward": {
                            "deadline_at": "2026-09-30T00:00:00+00:00",
                            "metrics": {
                                "paired_days": 5,
                                "candidate_net_pnl": 1.2,
                                "reference_net_pnl": 0.4,
                                "estimate": 0.008,
                                "lcb": -0.001,
                                "ucb": 0.02,
                            },
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    rows = probation_summary(store, job.id)
    assert rows == [
        {
            "trial_id": "t1",
            "family": "momentum",
            "candidate_id": "c-1",
            "source": "evolution_campaign",
            "status": "active",
            "phase": "forward",
            "paired_days": 5,
            "candidate_pnl": 1.2,
            "reference_pnl": 0.4,
            "delta": 0.008,
            "lcb": -0.001,
            "ucb": 0.02,
            "capital": 100.0,
            "deadline_at": "2026-09-30T00:00:00+00:00",
            "closed_reason": None,
            "promotion": None,
        }
    ]
