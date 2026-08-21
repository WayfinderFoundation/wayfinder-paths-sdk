"""Owner-adjudicated exhaustion claims: closing a research lane is a verdict.

The production stall this fixes: every research lane ended in an agent
SELF-rejection, the agent's dead map recorded those lanes as FULLY SETTLED,
and the wake mandate's "state why research is not warranted" hatch let every
subsequent stale wake cite the settled map — no update in the loop could
contradict the loop's own confident rejection. Exhaustion is an ESCALATION
verdict for an authorized reviewer, not a self-grant: agents FILE claims
(evidence summary, provenance, proposed next region); only the owner may
ACCEPT one — the same owner-provenance pattern as proposal rejection, script
mode stamps, and risk-latched halt clears.

Only an owner-accepted claim settles its audited lane. Filing is activity — it
puts an escalation in flight — but is not evidence that the lane is exhausted.
A claim whose provenance is `agent-self-rejected` can never settle a lane,
whatever its status: self-rejections are development evidence, not verdicts.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Any

from wayfinder_paths.jobs.models import utc_now_iso

if TYPE_CHECKING:
    from wayfinder_paths.jobs.store import JobStore

CLAIMS_DIR = "research/exhaustion_claims"
CLAIM_STATUSES = {"pending", "accepted", "rejected"}
CLAIM_PROVENANCES = {
    "holdout-refuted",
    "owner-rejected",
    "agent-self-rejected",
    "data-wall",
}


def _claim_path(store: JobStore, job_id: str, claim_id: str) -> str:
    return f"{CLAIMS_DIR}/{claim_id}.json"


def file_exhaustion_claim(
    store: JobStore,
    job_id: str,
    *,
    lane: str,
    evidence: str,
    provenance: str,
    next_region: str,
    refs: list[str] | None = None,
    filed_by: str = "agent",
) -> dict[str, Any]:
    """File a claim that a research lane/region is exhausted. Anyone may
    file; the claim starts `pending` and only the owner can accept it."""
    lane = str(lane or "").strip()
    if not lane:
        raise ValueError("exhaustion claim requires a lane/region name")
    if provenance not in CLAIM_PROVENANCES:
        raise ValueError(f"provenance must be one of {sorted(CLAIM_PROVENANCES)}")
    if not str(evidence or "").strip():
        raise ValueError("exhaustion claim requires an evidence summary")
    if not str(next_region or "").strip():
        raise ValueError(
            "exhaustion claim requires a proposed next region to open — "
            "closing a lane without naming a successor is a stall, not a claim"
        )
    slug = re.sub(r"[^a-z0-9]+", "-", lane.lower()).strip("-")[:40] or "lane"
    claim_id = f"claim-{slug}-{uuid.uuid4().hex[:8]}"
    claim = {
        "claim_id": claim_id,
        "job_id": job_id,
        "lane": lane,
        "evidence": str(evidence),
        "refs": [str(ref) for ref in refs or []],
        "provenance": provenance,
        "next_region": str(next_region),
        "status": "pending",
        "filed_by": filed_by,
        "filed_at": utc_now_iso(),
        "adjudication": None,
    }
    store.write_json(job_id, _claim_path(store, job_id, claim_id), claim)
    store.append_journal(
        job_id,
        {
            "type": "exhaustion_claim_filed",
            "claim_id": claim_id,
            "lane": lane,
            "provenance": provenance,
            "next_region": claim["next_region"],
        },
    )
    store.refresh_scorecard(job_id)
    return claim


def adjudicate_exhaustion_claim(
    store: JobStore,
    job_id: str,
    claim_id: str,
    *,
    status: str,
    by: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Owner adjudication. `accepted` is owner-only (PermissionError for any
    other actor — agents can FILE, never accept); `rejected` records who."""
    if status not in {"accepted", "rejected"}:
        raise ValueError("adjudication status must be accepted or rejected")
    if status == "accepted" and by != "owner":
        store.append_journal(
            job_id,
            {"type": "exhaustion_claim_accept_refused", "claim_id": claim_id, "by": by},
        )
        raise PermissionError(
            "only the owner may accept an exhaustion claim — agents file "
            "claims for adjudication, they never adjudicate their own"
        )
    claim = store.read_json(job_id, _claim_path(store, job_id, claim_id))
    if not isinstance(claim, dict):
        raise FileNotFoundError(f"unknown exhaustion claim: {claim_id}")
    if claim.get("status") != "pending":
        raise ValueError(f"claim {claim_id} is already {claim.get('status')}")
    claim["status"] = status
    claim["adjudication"] = {"by": by, "note": note, "ts": utc_now_iso()}
    store.write_json(job_id, _claim_path(store, job_id, claim_id), claim)
    store.append_journal(
        job_id,
        {
            "type": f"exhaustion_claim_{status}",
            "claim_id": claim_id,
            "lane": claim.get("lane"),
            "by": by,
        },
    )
    store.refresh_scorecard(job_id)
    return claim


def list_exhaustion_claims(
    store: JobStore, job_id: str, *, status: str | None = None
) -> list[dict[str, Any]]:
    claims_dir = store.job_dir(job_id) / CLAIMS_DIR
    if not claims_dir.is_dir():
        return []
    claims: list[dict[str, Any]] = []
    for path in sorted(claims_dir.glob("*.json")):
        loaded = store.read_json(job_id, f"{CLAIMS_DIR}/{path.name}")
        if not isinstance(loaded, dict):
            continue
        if status is not None and loaded.get("status") != status:
            continue
        claims.append(loaded)
    claims.sort(key=lambda claim: str(claim.get("filed_at") or ""))
    return claims


def claim_settles_lane(claim: dict[str, Any]) -> bool:
    """Whether this claim satisfies the progress constitution for its lane.

    Filing is an escalation, not a verdict. Only owner acceptance settles;
    `agent-self-rejected` provenance NEVER settles — nothing in the loop may
    control the only evidence used for its own acceptance."""
    if claim.get("provenance") == "agent-self-rejected":
        return False
    adjudication = claim.get("adjudication") or {}
    return claim.get("status") == "accepted" and adjudication.get("by") == "owner"
