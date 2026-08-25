"""Candidate archive: branches, not just a champion.

Every evaluated candidate is retained with its lineage, objective vector, and
behavior descriptor — the Pareto frontier and refuted branches stay visible
so exploration can revive an ancestor when the regime flips instead of
re-deriving it, and duplicate-family proposals hit their own refutation.
Entries are never silently deleted; status flips are the only mutation.
"""

from __future__ import annotations

from typing import Any

from wayfinder_paths.jobs.compute_lock import job_state_lock
from wayfinder_paths.jobs.improver.spec import revision_stamp
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

ARCHIVE_PATH = "state/archive.json"
ARCHIVE_STATUSES = {
    "generated",
    "invalid",
    "low_fidelity_rejected",
    "dev_frontier",
    "audit_rejected",
    "paper_probation",
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
_NON_RANKED_STATUSES = {
    "generated",
    "invalid",
    "low_fidelity_rejected",
    "audit_rejected",
    "refuted",
    "retired",
}


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
    parent_candidate_ids: list[str] | None = None,
    proposal_id: str | None = None,
    behavior: dict[str, Any] | None = None,
    evidence: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with job_state_lock(store.repo_root, job_id, name="archive"):
        return _record_candidate(
            store,
            job_id,
            candidate_id=candidate_id,
            family=family,
            summary=summary,
            status=status,
            objective=objective,
            revision=revision,
            parent_id=parent_id,
            parent_candidate_ids=parent_candidate_ids,
            proposal_id=proposal_id,
            behavior=behavior,
            evidence=evidence,
            metadata=metadata,
        )


def _record_candidate(
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
    parent_candidate_ids: list[str] | None = None,
    proposal_id: str | None = None,
    behavior: dict[str, Any] | None = None,
    evidence: str | None = None,
    metadata: dict[str, Any] | None = None,
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
                "revision": revision or existing.get("revision"),
                "evidence": existing.get("evidence")
                if sticky
                else (evidence or existing.get("evidence")),
                "updated_at": utc_now_iso(),
            }
        )
        if metadata:
            existing.setdefault("metadata", {}).update(metadata)
        # Content-derived IDs dedup re-proposals of the same workspace: the
        # entry accumulates every proposal UUID and parent edge instead of
        # duplicating. Refresh behavior when a later evaluation supplies it.
        if proposal_id:
            ids = existing.setdefault("proposal_ids", [])
            if proposal_id not in ids:
                ids.append(proposal_id)
        for parent in parent_candidate_ids or []:
            parents = existing.setdefault("parent_candidate_ids", [])
            if parent and parent not in parents:
                parents.append(parent)
        if behavior:
            existing["behavior"] = behavior
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
            "parent_candidate_ids": [p for p in (parent_candidate_ids or []) if p],
            "proposal_ids": [proposal_id] if proposal_id else [],
            "behavior": behavior or {},
            "evidence": evidence,
            "metadata": dict(metadata or {}),
            "created_at": utc_now_iso(),
            **revision_stamp(store.job_dir(job_id)),
        }
        doc["candidates"].append(entry)
    _refresh_frontier(doc)
    store.write_json(job_id, ARCHIVE_PATH, doc)
    return entry


def behavior_cell(behavior: dict[str, Any] | None) -> str | None:
    """Map a candidate into the 3×3×3 quality-diversity archive."""
    behavior = behavior or {}
    try:
        direction = float(behavior["direction_bias"])
        hold_value = behavior.get("median_hold_bars")
        hold_bars = float(
            hold_value if hold_value is not None else behavior["average_hold_bars"]
        )
        density = float(behavior["trades_per_asset_30d"])
    except (KeyError, TypeError, ValueError):
        return None
    direction_bin = (
        "short" if direction < -0.25 else "long" if direction > 0.25 else "mixed"
    )
    hold_bin = "fast" if hold_bars <= 12 else "slow" if hold_bars > 72 else "medium"
    density_bin = "sparse" if density < 5 else "dense" if density > 20 else "regular"
    return f"{direction_bin}/{hold_bin}/{density_bin}"


def quality_diversity_snapshot(
    store: JobStore, job_id: str, *, per_cell: int = 2
) -> dict[str, list[dict[str, Any]]]:
    """Return up to two non-dominated candidates per occupied behavior cell."""
    cells: dict[str, list[dict[str, Any]]] = {}
    for entry in load_archive(store, job_id).get("candidates") or []:
        if entry.get("status") in _NON_RANKED_STATUSES:
            continue
        cell = behavior_cell(entry.get("behavior"))
        if cell is not None:
            cells.setdefault(cell, []).append(entry)
    snapshot: dict[str, list[dict[str, Any]]] = {}
    for cell, entries in cells.items():
        nondominated = [
            entry
            for entry in entries
            if not any(
                _dominates(other, entry) for other in entries if other is not entry
            )
        ]
        nondominated.sort(
            key=lambda entry: float(
                (entry.get("objective") or {}).get("net_log_growth") or 0.0
            ),
            reverse=True,
        )
        snapshot[cell] = nondominated[: max(1, int(per_cell))]
    return snapshot


def set_candidate_status(
    store: JobStore,
    job_id: str,
    candidate_id: str,
    status: str,
    *,
    evidence: str | None = None,
) -> dict[str, Any]:
    with job_state_lock(store.repo_root, job_id, name="archive"):
        return _set_candidate_status(
            store, job_id, candidate_id, status, evidence=evidence
        )


def _set_candidate_status(
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
    branches (still ranked on the frontier — that is the point). The id
    resolves as content id first, then proposal UUID, then raw revision —
    promotion callers hold different handles across archive generations."""
    with job_state_lock(store.repo_root, job_id, name="archive"):
        doc = load_archive(store, job_id)
        promoted = _resolve(doc, candidate_id)
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


def lineage_of(store: JobStore, job_id: str, candidate_id: str) -> list[dict[str, Any]]:
    """Ancestry walk over parent_candidate_ids (legacy parent_id as
    fallback edge), nearest parent first. Cycle-safe; unknown parents end
    the walk — the DAG only knows what was archived."""
    doc = load_archive(store, job_id)
    entry = _resolve(doc, candidate_id)
    lineage: list[dict[str, Any]] = []
    seen: set[str] = set()
    frontier = list((entry or {}).get("parent_candidate_ids") or []) or [
        str((entry or {}).get("parent_id") or "")
    ]
    while frontier:
        parent_id = frontier.pop(0)
        if not parent_id or parent_id in seen:
            continue
        seen.add(parent_id)
        parent = _resolve(doc, parent_id)
        if parent is None:
            continue
        lineage.append(parent)
        frontier.extend(parent.get("parent_candidate_ids") or [parent.get("parent_id")])
    return lineage


def _resolve(doc: dict[str, Any], candidate_id: str) -> dict[str, Any] | None:
    exact = _find(doc, candidate_id)
    if exact is not None:
        return exact
    for entry in doc.get("candidates") or []:
        if candidate_id in (entry.get("proposal_ids") or []):
            return entry
    for entry in doc.get("candidates") or []:
        if entry.get("revision") and entry["revision"] == candidate_id:
            return entry
    return None


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
        and entry.get("status") not in _NON_RANKED_STATUSES
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
