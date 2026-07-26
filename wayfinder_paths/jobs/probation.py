"""Probation registry: durable, synced state for reduced-size trial legs.

Probation is only honest if its bookkeeping is visible: each leg's size cap,
pre-registered graduate/kill criteria, and progress live in one structured
file (`probation.json`) that rides the job snapshot to the backend — so the
owner watches the same numbers the worker updates, and graduation/kill are
journaled events, not prose."""

from __future__ import annotations

from typing import Any

from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

PROBATION_PATH = "probation.json"
PROBATION_STATUSES = {"active", "graduated", "killed"}
MAX_ACTIVE_LEGS = 2


def load_probation(store: JobStore, job_id: str) -> dict[str, Any]:
    return store.read_json(job_id, PROBATION_PATH, default={"legs": []}) or {"legs": []}


def record_probation_leg(
    store: JobStore,
    job_id: str,
    *,
    name: str,
    symbol: str,
    size_fraction: float,
    graduate_criterion: str,
    kill_criterion: str,
    proposal_id: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    if not 0 < size_fraction <= 0.5:
        raise ValueError("probation size_fraction must be in (0, 0.5]")
    doc = load_probation(store, job_id)
    active = [leg for leg in doc["legs"] if leg.get("status") == "active"]
    if len(active) >= MAX_ACTIVE_LEGS:
        raise ValueError(
            f"max {MAX_ACTIVE_LEGS} concurrent probation legs — graduate or "
            "kill one first"
        )
    if any(leg.get("name") == name for leg in doc["legs"]):
        raise ValueError(f"probation leg {name!r} already exists")
    leg = {
        "name": name,
        "symbol": symbol,
        "status": "active",
        "deployed_at": utc_now_iso(),
        "size_fraction": float(size_fraction),
        "proposal_id": proposal_id,
        "graduate": {"criterion": graduate_criterion, "progress": None},
        "kill": {"criterion": kill_criterion, "status": None},
        "notes": notes,
    }
    doc["legs"].append(leg)
    store.write_json(job_id, PROBATION_PATH, doc)
    store.append_journal(
        job_id,
        {"type": "probation_leg_opened", "leg": name, "proposal_id": proposal_id},
    )
    return leg


def update_probation_leg(
    store: JobStore,
    job_id: str,
    name: str,
    *,
    progress: str | None = None,
    kill_status: str | None = None,
    status: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    doc = load_probation(store, job_id)
    leg = next((leg for leg in doc["legs"] if leg.get("name") == name), None)
    if leg is None:
        raise ValueError(f"unknown probation leg {name!r}")
    if progress is not None:
        leg["graduate"]["progress"] = progress
    if kill_status is not None:
        leg["kill"]["status"] = kill_status
    if notes is not None:
        leg["notes"] = notes
    if status is not None:
        if status not in PROBATION_STATUSES:
            raise ValueError(f"status must be one of {sorted(PROBATION_STATUSES)}")
        previous = leg.get("status")
        leg["status"] = status
        if status != previous and status in {"graduated", "killed"}:
            leg["closed_at"] = utc_now_iso()
            store.append_journal(
                job_id, {"type": f"probation_leg_{status}", "leg": name}
            )
    leg["updated_at"] = utc_now_iso()
    store.write_json(job_id, PROBATION_PATH, doc)
    return leg
