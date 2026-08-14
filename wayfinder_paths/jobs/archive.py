"""Candidate archive: branches, not just a champion.

Every evaluated candidate is retained with its lineage, objective vector, and
behavior descriptor — the Pareto frontier and refuted branches stay visible
so exploration can revive an ancestor when the regime flips instead of
re-deriving it, and duplicate-family proposals hit their own refutation.
Entries are never silently deleted; status flips are the only mutation.
"""

from __future__ import annotations

from typing import Any

from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

ARCHIVE_PATH = "state/archive.json"
ARCHIVE_STATUSES = {
    "incumbent",
    "frontier",
    "archived",
    "probation",
    "refuted",
    "retired",
}
# Pareto axes over the objective vector: maximize growth, minimize the rest.
_MAXIMIZE = ("net_log_growth",)
_MINIMIZE = ("downside_deviation", "tail_loss", "max_drawdown_pct")


def load_archive(store: JobStore, job_id: str) -> dict[str, Any]:
    doc = store.read_json(job_id, ARCHIVE_PATH) or {}
    if not isinstance(doc, dict) or not isinstance(doc.get("candidates"), list):
        return {"candidates": []}
    return doc


def record_candidate(
    store: JobStore,
    job_id: str,
    *,
    candidate_id: str,
    family: str,
    summary: str,
    status: str,
    objective: dict[str, Any] | None,
    revision: str | None = None,
    parent_id: str | None = None,
    behavior: dict[str, Any] | None = None,
    evidence: str | None = None,
) -> dict[str, Any]:
    if status not in ARCHIVE_STATUSES:
        raise ValueError(f"status must be one of {sorted(ARCHIVE_STATUSES)}")
    doc = load_archive(store, job_id)
    existing = _find(doc, candidate_id)
    if existing is not None:
        # Same candidate re-evaluated: update evidence/status, keep lineage.
        # Terminal verdicts are STICKY — re-recording a refuted/retired family
        # (the resubmit-a-dead-idea move) must not silently reset it to
        # archived and re-open it for promotion. Reopening a refutation
        # requires an explicit set_candidate_status with named new evidence.
        sticky = existing.get("status") in {"refuted", "retired"}
        existing.update(
            {
                "status": existing["status"] if sticky else status,
                "objective": objective or existing.get("objective"),
                "evidence": existing.get("evidence") if sticky else (
                    evidence or existing.get("evidence")
                ),
                "updated_at": utc_now_iso(),
            }
        )
        entry = existing
    else:
        entry = {
            "candidate_id": candidate_id,
            "family": family,
            "summary": summary[:160],
            "status": status,
            "objective": objective,
            "revision": revision,
            "parent_id": parent_id,
            "behavior": behavior or {},
            "evidence": evidence,
            "created_at": utc_now_iso(),
        }
        doc["candidates"].append(entry)
    _refresh_frontier(doc)
    store.write_json(job_id, ARCHIVE_PATH, doc)
    return entry


def set_candidate_status(
    store: JobStore,
    job_id: str,
    candidate_id: str,
    status: str,
    *,
    evidence: str | None = None,
) -> dict[str, Any]:
    if status not in ARCHIVE_STATUSES:
        raise ValueError(f"status must be one of {sorted(ARCHIVE_STATUSES)}")
    doc = load_archive(store, job_id)
    entry = _find(doc, candidate_id)
    if entry is None:
        raise ValueError(f"unknown archive candidate {candidate_id!r}")
    entry["status"] = status
    if evidence:
        entry["evidence"] = evidence
    entry["updated_at"] = utc_now_iso()
    _refresh_frontier(doc)
    store.write_json(job_id, ARCHIVE_PATH, doc)
    return entry


def set_incumbent(store: JobStore, job_id: str, candidate_id: str) -> None:
    """Promote one entry to incumbent; previous incumbents become archived
    branches (still ranked on the frontier — that is the point)."""
    doc = load_archive(store, job_id)
    promoted = _find(doc, candidate_id)
    if promoted is None:
        return
    for entry in doc.get("candidates") or []:
        if entry.get("status") == "incumbent":
            entry["status"] = "archived"
            entry["updated_at"] = utc_now_iso()
    promoted["status"] = "incumbent"
    promoted["updated_at"] = utc_now_iso()
    _refresh_frontier(doc)
    store.write_json(job_id, ARCHIVE_PATH, doc)


def archive_snapshot_block(store: JobStore, job_id: str) -> dict[str, Any]:
    """Compact per-wake view: frontier + counts + refuted families, so
    exploration cites archive state instead of memory."""
    doc = load_archive(store, job_id)
    candidates = doc.get("candidates") or []
    counts: dict[str, int] = {}
    for entry in candidates:
        counts[entry.get("status", "?")] = counts.get(entry.get("status", "?"), 0) + 1
    frontier = [
        {
            "candidate_id": entry["candidate_id"],
            "family": entry["family"],
            "summary": entry["summary"],
            "objective": entry.get("objective"),
            "status": entry["status"],
        }
        for entry in candidates
        if entry.get("on_frontier")
    ]
    refuted = [
        {
            "candidate_id": entry["candidate_id"],
            "family": entry["family"],
            "evidence": (entry.get("evidence") or "")[:120],
        }
        for entry in candidates
        if entry.get("status") == "refuted"
    ][-10:]
    return {
        "counts": counts,
        "frontier": frontier[:8],
        "recent_refuted": refuted,
        "_basis": (
            "Candidate archive. Re-proposing a refuted family requires NAMED "
            "new evidence; a frontier entry strong in the current regime is a "
            "revival candidate before any novel divergent search."
        ),
    }


def _find(doc: dict[str, Any], candidate_id: str) -> dict[str, Any] | None:
    for entry in doc.get("candidates") or []:
        if entry.get("candidate_id") == candidate_id:
            return entry
    return None


def _refresh_frontier(doc: dict[str, Any]) -> None:
    """Pareto frontier over candidates with objective vectors, excluding
    refuted/retired branches (dead branches stay recorded, not ranked)."""
    ranked = [
        entry
        for entry in doc.get("candidates") or []
        if isinstance(entry.get("objective"), dict)
        and entry.get("status") not in {"refuted", "retired"}
    ]
    for entry in doc.get("candidates") or []:
        entry["on_frontier"] = False
    for entry in ranked:
        entry["on_frontier"] = not any(
            _dominates(other, entry) for other in ranked if other is not entry
        )


def _dominates(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True when a is at least as good on every axis and better on one."""
    better_somewhere = False
    for axis in _MAXIMIZE + _MINIMIZE:
        a_value = a["objective"].get(axis)
        b_value = b["objective"].get(axis)
        if a_value is None or b_value is None:
            return False
        a_value, b_value = float(a_value), float(b_value)
        if axis in _MAXIMIZE:
            if a_value < b_value:
                return False
            better_somewhere = better_somewhere or a_value > b_value
        else:
            if a_value > b_value:
                return False
            better_somewhere = better_somewhere or a_value < b_value
    return better_somewhere
