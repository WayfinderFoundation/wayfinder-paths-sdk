"""Compact evolution and probation views for the job snapshot.

Evolution runs on its own two-day cadence for eligible harnessed jobs; the
snapshot carries where a job stands (eligible, campaign status, when the
next campaign is due) and every probation trial in one row each, so the UI
and the chat surface read them without SDK round-trips. Raise-free: a feed
failure must never break a sync.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore

FREESTYLE_PATH_REASON = "freestyle_and_path_jobs_do_not_evolve"


def evolution_snapshot(
    store: JobStore, job_id: str, job: WayfinderJob
) -> dict[str, Any]:
    root = store.job_dir(job_id)
    contract = str(job.execution_contract or "legacy")
    if contract != "jobs_v1":
        return {
            "eligibility": {"eligible": False, "reasons": [FREESTYLE_PATH_REASON]},
            "campaign_status": None,
            "start_interval_hours": None,
            "next_due_at": None,
        }
    from wayfinder_paths.jobs.evolution_campaign import campaign_status
    from wayfinder_paths.jobs.improver.spec import ImproverSpec

    spec = ImproverSpec.load(root)
    eligibility = spec.evolution_eligibility(root, job_id)
    status = campaign_status(store, job_id) or {}
    interval_hours = float(
        spec.evolution.get("start_interval_hours")
        or spec.evolution.get("cooldown_hours")
        or 0
    )
    next_due_at: str | None = None
    started_at = status.get("started_at")
    if started_at and interval_hours > 0:
        try:
            anchor = datetime.fromisoformat(str(started_at))
            next_due_at = (anchor + timedelta(hours=interval_hours)).isoformat()
        except ValueError:
            next_due_at = None
    return {
        "eligibility": eligibility,
        "campaign_status": {
            key: status.get(key)
            for key in ("status", "campaign_id", "started_at", "finished_at", "phase")
            if key in status
        },
        "start_interval_hours": interval_hours or None,
        "next_due_at": next_due_at,
    }


def probation_summary(store: JobStore, job_id: str) -> list[dict[str, Any]]:
    from wayfinder_paths.jobs.probation import load_probation

    doc = load_probation(store, job_id) or {}
    rows: list[dict[str, Any]] = []
    for trial in doc.get("trials") or []:
        forward = dict(trial.get("forward") or {})
        metrics = dict(forward.get("metrics") or {})
        rows.append(
            {
                "trial_id": trial.get("trial_id"),
                "family": trial.get("family"),
                "candidate_id": trial.get("candidate_id"),
                "source": trial.get("source"),
                "status": trial.get("status"),
                "phase": trial.get("phase"),
                "paired_days": metrics.get("paired_days"),
                "candidate_pnl": metrics.get("candidate_net_pnl"),
                "reference_pnl": metrics.get("reference_net_pnl"),
                "delta": metrics.get("estimate"),
                "lcb": metrics.get("lcb"),
                "ucb": metrics.get("ucb"),
                "capital": (trial.get("burn_in") or {}).get("capital"),
                "deadline_at": forward.get("deadline_at"),
                "closed_reason": trial.get("closed_reason") or trial.get("reason"),
                "promotion": trial.get("promotion"),
            }
        )
    return rows


__all__ = ["FREESTYLE_PATH_REASON", "evolution_snapshot", "probation_summary"]
