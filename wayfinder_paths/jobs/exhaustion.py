"""Evidence-adjudicated exhaustion claims: closing a lane is a verdict.

The production stall this fixes: every research lane ended in an agent
SELF-rejection, the agent's dead map recorded those lanes as FULLY SETTLED,
and the wake mandate's "state why research is not warranted" hatch let every
subsequent stale wake cite the settled map — no update in the loop could
contradict the loop's own confident rejection. Exhaustion is an ESCALATION
verdict for an authorized reviewer, not a self-grant: agents FILE claims
(evidence summary, provenance, proposed next region); only the owner may
ACCEPT one — the same owner-provenance pattern as proposal rejection, script
mode stamps, and risk-latched halt clears.

Only an owner acceptance or a passing mechanical coverage audit settles its
audited lane. Filing is activity — it puts an escalation in flight — but is
not evidence that the lane is exhausted. A claim whose provenance is
`agent-self-rejected` can never settle a lane, whatever its status.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from wayfinder_paths.jobs.models import utc_now_iso

if TYPE_CHECKING:
    from wayfinder_paths.jobs.store import JobStore

CLAIMS_DIR = "research/exhaustion_claims"
AUDITS_DIR = "research/coverage"
RESEARCH_LANE_PATH = "state/research_lane.json"
RESEARCH_IMPASSE_PATH = "state/research_impasse.json"
OWNER_OVERRIDE_WINDOW = timedelta(hours=48)
CLAIM_STATUSES = {"pending", "accepted", "audit_passed", "rejected", "reopened"}
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
    file; it starts `pending` for a coverage audit or owner adjudication."""
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


def audit_and_adjudicate_exhaustion_claim(
    store: JobStore, job_id: str, claim_id: str
) -> dict[str, Any]:
    """Run the distinct evidence-gated audit actor and apply its verdict.

    Manual acceptance remains owner-only in ``adjudicate_exhaustion_claim``;
    this path can settle only through a reproducible coverage certificate.
    """
    from wayfinder_paths.jobs.coverage import audit_exhaustion_claim

    claim = store.read_json(job_id, _claim_path(store, job_id, claim_id))
    if not isinstance(claim, dict):
        raise FileNotFoundError(f"unknown exhaustion claim: {claim_id}")
    if claim.get("status") != "pending":
        raise ValueError(f"claim {claim_id} is already {claim.get('status')}")

    audit = audit_exhaustion_claim(store, job_id, claim)
    audited_at = datetime.now(UTC)
    certificate_path = f"{AUDITS_DIR}/{claim_id}.json"
    store.write_json(job_id, certificate_path, audit["certificate"])
    audit_summary = {
        "verdict": audit["verdict"],
        "reason_codes": audit["reason_codes"],
        "audited_scope": audit["audited_scope"],
        "required_next_experiments": audit["required_next_experiments"],
        "certificate": certificate_path,
        "audited_at": audited_at.isoformat(),
    }
    claim["audit"] = audit_summary

    if audit["verdict"] in {"pass", "narrow"}:
        claim["status"] = "audit_passed"
        claim["adjudication"] = {
            "by": "coverage-audit",
            "verdict": audit["verdict"],
            "ts": audited_at.isoformat(),
        }
        claim["audited_scope"] = audit["audited_scope"]
        claim["required_next_experiments"] = audit["required_next_experiments"]
        claim["owner_override_until"] = (audited_at + OWNER_OVERRIDE_WINDOW).isoformat()
        store.write_json(
            job_id,
            RESEARCH_LANE_PATH,
            {
                "active_lane": claim.get("next_region"),
                "opened_at": audited_at.isoformat(),
                "opened_from_claim": claim_id,
                "settled_lane": claim.get("lane"),
                "settled_scope": audit["audited_scope"],
                "carryover_mandate": (
                    audit["required_next_experiments"]
                    if audit["verdict"] == "narrow"
                    else []
                ),
            },
        )
        marker = store.read_json(job_id, RESEARCH_IMPASSE_PATH) or {}
        if audit["verdict"] == "narrow":
            mandate = {
                "claim_id": claim_id,
                "lane": claim.get("lane"),
                "reason_codes": audit["reason_codes"],
                "required_next_experiments": audit["required_next_experiments"],
                "certificate": certificate_path,
            }
            store.write_json(
                job_id,
                RESEARCH_IMPASSE_PATH,
                {
                    **marker,
                    "alerted_at": audited_at.isoformat(),
                    "status": "mandated_work",
                    "claim_ids": [claim_id],
                    "mandate": mandate,
                },
            )
        else:
            store.write_json(job_id, RESEARCH_IMPASSE_PATH, {})
        store.append_journal(
            job_id,
            {
                "type": "exhaustion_claim_audit_passed",
                "claim_id": claim_id,
                "lane": claim.get("lane"),
                "by": "coverage-audit",
                "audit_verdict": audit["verdict"],
                "certificate": certificate_path,
                "owner_override_until": claim["owner_override_until"],
            },
        )
        store.append_journal(
            job_id,
            {
                "type": "research_lane_settled",
                "claim_id": claim_id,
                "lane": claim.get("lane"),
                "scope": audit["audited_scope"],
            },
        )
        store.append_journal(
            job_id,
            {
                "type": "research_region_opened",
                "claim_id": claim_id,
                "region": claim.get("next_region"),
            },
        )
        if audit["verdict"] == "narrow":
            store.append_journal(
                job_id,
                {"type": "research_impasse_mandated", **mandate},
            )
        elif marker.get("alerted_at"):
            store.append_journal(
                job_id,
                {
                    "type": "research_impasse_resolved",
                    "adjudication_signals": ["exhaustion_claim_audit_passed"],
                },
            )
    else:
        claim["status"] = "rejected"
        claim["adjudication"] = {
            "by": "coverage-audit",
            "verdict": "reject",
            "ts": audited_at.isoformat(),
        }
        claim["required_next_experiments"] = audit["required_next_experiments"]
        current_marker = store.read_json(job_id, RESEARCH_IMPASSE_PATH) or {}
        mandate = {
            "claim_id": claim_id,
            "lane": claim.get("lane"),
            "reason_codes": audit["reason_codes"],
            "required_next_experiments": audit["required_next_experiments"],
            "certificate": certificate_path,
        }
        store.write_json(
            job_id,
            RESEARCH_IMPASSE_PATH,
            {
                **current_marker,
                "alerted_at": current_marker.get("alerted_at")
                or audited_at.isoformat(),
                "status": "mandated_work",
                "claim_ids": [claim_id],
                "mandate": mandate,
            },
        )
        store.append_journal(
            job_id,
            {
                "type": "exhaustion_claim_rejected",
                "claim_id": claim_id,
                "lane": claim.get("lane"),
                "by": "coverage-audit",
                "reason_codes": audit["reason_codes"],
                "required_next_experiments": audit["required_next_experiments"],
                "certificate": certificate_path,
            },
        )
        store.append_journal(
            job_id,
            {"type": "research_impasse_mandated", **mandate},
        )

    # Commit the terminal claim state after its lane/mandate side effects. If
    # the process dies during those writes, the still-pending claim remains
    # eligible for a watchdog retry instead of becoming permanently partial.
    store.write_json(job_id, _claim_path(store, job_id, claim_id), claim)
    store.refresh_scorecard(
        job_id,
        {
            "coverage_audit": {
                "claim_id": claim_id,
                "lane": claim.get("lane"),
                "verdict": audit["verdict"],
                "certificate": certificate_path,
                "audited_at": audited_at.isoformat(),
                "required_next_experiments": audit["required_next_experiments"],
                "owner_override_until": claim.get("owner_override_until"),
            }
        },
    )
    return claim


def reopen_exhaustion_claim(
    store: JobStore,
    job_id: str,
    claim_id: str,
    *,
    by: str,
    reason: str,
) -> dict[str, Any]:
    """Owner override for an audit-settled lane during its 48-hour window."""
    if by != "owner":
        store.append_journal(
            job_id,
            {"type": "exhaustion_claim_reopen_refused", "claim_id": claim_id, "by": by},
        )
        raise PermissionError("only the owner may reopen an audit-settled lane")
    if not str(reason or "").strip():
        raise ValueError("reopening an exhaustion claim requires a reason")
    claim = store.read_json(job_id, _claim_path(store, job_id, claim_id))
    if not isinstance(claim, dict):
        raise FileNotFoundError(f"unknown exhaustion claim: {claim_id}")
    if claim.get("status") != "audit_passed":
        raise ValueError(f"claim {claim_id} is not audit-passed")
    try:
        deadline = datetime.fromisoformat(str(claim["owner_override_until"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"claim {claim_id} has no valid override window") from exc
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    if now > deadline:
        raise ValueError(f"claim {claim_id} owner override window has expired")

    claim["status"] = "reopened"
    claim["reopened"] = {"by": by, "reason": reason, "ts": now.isoformat()}
    required = [
        {
            "id": f"owner-reopen:{claim_id}",
            "kind": "owner_reopen",
            "lane": claim.get("lane"),
            "reason": reason,
            "status": "not_run",
        }
    ]
    store.write_json(
        job_id,
        RESEARCH_LANE_PATH,
        {
            "active_lane": claim.get("lane"),
            "reopened_at": now.isoformat(),
            "reopened_claim": claim_id,
        },
    )
    store.write_json(
        job_id,
        RESEARCH_IMPASSE_PATH,
        {
            "alerted_at": now.isoformat(),
            "status": "mandated_work",
            "claim_ids": [claim_id],
            "mandate": {
                "claim_id": claim_id,
                "lane": claim.get("lane"),
                "reason_codes": ["owner_override"],
                "required_next_experiments": required,
            },
        },
    )
    store.append_journal(
        job_id,
        {
            "type": "exhaustion_claim_reopened",
            "claim_id": claim_id,
            "lane": claim.get("lane"),
            "by": by,
            "reason": reason,
        },
    )
    store.append_journal(
        job_id,
        {
            "type": "research_lane_reopened",
            "claim_id": claim_id,
            "lane": claim.get("lane"),
        },
    )
    # As above, commit the claim state last so a partial owner override can be
    # retried while the audit-passed claim remains the durable source state.
    store.write_json(job_id, _claim_path(store, job_id, claim_id), claim)
    store.refresh_scorecard(
        job_id,
        {
            "coverage_audit": {
                "claim_id": claim_id,
                "lane": claim.get("lane"),
                "verdict": "reopened",
                "reopened_at": now.isoformat(),
            }
        },
    )
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

    Filing is an escalation, not a verdict. Owner acceptance and a passing
    coverage audit settle; `agent-self-rejected` provenance NEVER settles."""
    if claim.get("provenance") == "agent-self-rejected":
        return False
    adjudication = claim.get("adjudication") or {}
    return bool(
        (claim.get("status") == "accepted" and adjudication.get("by") == "owner")
        or (
            claim.get("status") == "audit_passed"
            and adjudication.get("by") == "coverage-audit"
            and adjudication.get("verdict") in {"pass", "narrow"}
        )
    )
