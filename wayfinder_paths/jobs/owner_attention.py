"""Owner-attention feed: what actually needs the owner vs what ran itself.

Two-zone doctrine (owner-approved): ``needs_you`` is reserved for
live-capital decisions (approvals on live-capable jobs, risk-latched halt
clears, live decision-gate trips), governance escalations surfaced as
``owner_review_required`` journal markers (successor abandonment, reject-
refusal escalations, live-mode ratifications), and exhaustion claims the
mechanical coverage audit could not adjudicate. Everything else is
mechanical-with-visibility: it lands in ``decided_autonomously`` with
evidence and a bounded undo instead of an approval queue.

Live-capital risk here is deliberately NOT ``store._job_is_live_capable``
(that flags any job that has ENTERED paper operation — the fail-closed
governance trigger, which would route every operating paper job's pending
proposal to the owner and re-create the approval queue this feed replaces).
A paper job with no wallet bound cannot lose live capital; the attention
split keys on the live boundary instead: script mode live, or a funded
wallet bound (one operator flip from live).

Both arrays ride the sync snapshot top-level (like ``scorecard``), so the
backend/FE consume them without SDK round-trips. Building the feed is
read-only and raise-free — a broken artifact must never break a sync.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from wayfinder_paths.jobs.halt import RISK_LATCH_SOURCES, read_halt
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore, proposal_approvable

DECIDED_WINDOW_DAYS = 7
DECIDED_CAP = 50
# owner_review_required markers have no mechanical clear event; bound how
# long an unactioned one stays visible.
MARKER_WINDOW_DAYS = 30
# A freshly filed claim legitimately has no audit verdict until the next
# watchdog pass (5-minute cadence); only escalate after the audit has had
# ample time to produce one.
CLAIM_AUDIT_GRACE_SECONDS = 60 * 60
_JOURNAL_SCAN_LIMIT = 4000
_MECHANICAL_AUDIT_VERDICTS = frozenset({"pass", "narrow", "reject"})

# owner_review_required journal markers surface as needs_you items with
# kind = event type — but ONLY for these event types. The FE renders each
# kind with dedicated action buttons (vault-frontend WAYFINDER_NEEDS_YOU_KINDS);
# an unknown kind renders as a dead generic label the owner cannot act on, so
# marker types outside this set must never pass through. Notably the batch
# ``exhaustion_claims_pending`` marker (written by older deployed watchdogs,
# still present in live journals) is dropped here — pending claims surface
# per-claim via ``_unauditable_claims`` with ref_id = claim_id instead.
_OWNER_REVIEW_MARKER_KINDS = frozenset(
    {
        "successor_abandoned",
        "proposal_reject_refused",
        "live_mode_audit",
    }
)

# The complete needs_you kind union the deployed FE understands. Every item
# emitted by _needs_you MUST use one of these kinds — this constant exists so
# tests can pin the contract and future drift fails loudly. Mirror of
# vault-frontend's WAYFINDER_NEEDS_YOU_KINDS.
NEEDS_YOU_KINDS = _OWNER_REVIEW_MARKER_KINDS | frozenset(
    {
        "live_proposal_approval",
        "exhaustion_claim_unauditable",
        "decision_gate_tripped",
        "halt_awaiting_owner_clear",
    }
)

_PROBATION_OPENED_EVENTS = frozenset({"probation_leg_opened", "paper_probation_opened"})
_PROBATION_EVENTS = _PROBATION_OPENED_EVENTS | frozenset(
    {"probation_leg_graduated", "probation_leg_killed"}
)
# Lifecycle parks are mechanical-with-visibility: the sweep already paused
# the loops; the owner sees the decision plus the bounded undo here, never
# an approval queue item.
_LIFECYCLE_PARK_EVENTS = frozenset(
    {"job_parked_unbootstrapped", "job_parked_monitor_decay"}
)


def job_live_capital_risk(job: WayfinderJob) -> bool:
    """Live capital at stake: running live, or wallet-bound (one flip away)."""
    if str(job.script_loop.mode or "paper") == "live":
        return True
    return bool(job.execution_params.get("wallet_label"))


def build_owner_attention(
    store: JobStore,
    job_id: str,
    *,
    job: WayfinderJob | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    try:
        return _build(store, job_id, job=job, now=now)
    except Exception:  # noqa: BLE001 — sync must never die on a feed
        return {"needs_you": [], "decided_autonomously": []}


def _build(
    store: JobStore,
    job_id: str,
    *,
    job: WayfinderJob | None,
    now: dt.datetime | None,
) -> dict[str, Any]:
    job = job or store.load(job_id)
    now = now or dt.datetime.now(dt.UTC)
    journal = store.read_jsonl(job_id, "journal.jsonl", limit=_JOURNAL_SCAN_LIMIT)
    proposals = store.proposals(job_id)
    return {
        "needs_you": _needs_you(store, job_id, job, proposals, journal, now),
        "decided_autonomously": _decided_autonomously(store, job_id, journal, now),
    }


def _needs_you(
    store: JobStore,
    job_id: str,
    job: WayfinderJob,
    proposals: list[dict[str, Any]],
    journal: list[dict[str, Any]],
    now: dt.datetime,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    created_ts = {
        str(event.get("proposal_id")): event.get("ts")
        for event in journal
        if event.get("type") == "proposal_created"
    }
    proposals_by_id = {str(p.get("proposal_id")): p for p in proposals}

    if job_live_capital_risk(job):
        for proposal in proposals:
            if proposal.get("status") != "pending":
                continue
            if not proposal_approvable(store, job_id, proposal)[0]:
                # Owner directive: an un-approvable proposal must never be
                # owner-visible. The watchdog triage owns blocked pendings
                # (revalidate infra freezes, TTL-reject evidence-negatives) —
                # the approval queue shows only items the owner CAN approve.
                continue
            pid = str(proposal.get("proposal_id"))
            summary = str(
                (proposal.get("proposed_change") or {}).get("summary")
                or proposal.get("change_summary")
                or ""
            )
            items.append(
                _item(
                    kind="live_proposal_approval",
                    job_id=job_id,
                    ref_id=pid,
                    summary=f"pending proposal on a live-capable job: {summary}"[:300],
                    evidence_ref=f"proposals/{pid}.json",
                    since_ts=created_ts.get(pid) or proposal.get("updated_at"),
                )
            )

    items.extend(_unauditable_claims(store, job_id, now))
    items.extend(_owner_review_markers(job_id, journal, proposals_by_id, now))
    items.extend(_tripped_gates(store, job_id))

    halt = read_halt(store.job_dir(job_id))
    if halt and str(halt.get("source") or "") in RISK_LATCH_SOURCES:
        source = str(halt.get("source"))
        items.append(
            _item(
                kind="halt_awaiting_owner_clear",
                job_id=job_id,
                ref_id=source,
                summary=(
                    f"risk-latched halt ({source}): {halt.get('reason')} — "
                    "clearing requires the owner (wayfinder job resume-from-halt)"
                )[:300],
                evidence_ref="state/halt.json",
                since_ts=halt.get("ts"),
            )
        )
    items.sort(key=lambda item: str(item.get("since_ts") or ""))
    return items


def _unauditable_claims(
    store: JobStore, job_id: str, now: dt.datetime
) -> list[dict[str, Any]]:
    from wayfinder_paths.jobs.exhaustion import list_exhaustion_claims

    items: list[dict[str, Any]] = []
    for claim in list_exhaustion_claims(store, job_id, status="pending"):
        verdict = str((claim.get("audit") or {}).get("verdict") or "")
        if verdict in _MECHANICAL_AUDIT_VERDICTS:
            # The audit actor produced a verdict — the mechanical path
            # (settle / required experiments) owns this claim, not the owner.
            continue
        filed_at = _parse_time(claim.get("filed_at"))
        if (
            not verdict
            and filed_at
            and (now - filed_at).total_seconds() < CLAIM_AUDIT_GRACE_SECONDS
        ):
            continue  # audit simply hasn't run yet
        claim_id = str(claim.get("claim_id"))
        summary = (
            f"exhaustion claim on lane {claim.get('lane')!r} has no "
            "mechanical audit verdict — owner adjudication required"
        )
        evidence = " ".join(str(claim.get("evidence") or "").split())
        if evidence:
            summary = f"{summary}; evidence: {evidence}"
        items.append(
            _item(
                kind="exhaustion_claim_unauditable",
                job_id=job_id,
                ref_id=claim_id,
                summary=summary[:300],
                evidence_ref=f"research/exhaustion_claims/{claim_id}.json",
                since_ts=claim.get("filed_at"),
            )
        )
    return items


def _owner_review_markers(
    job_id: str,
    journal: list[dict[str, Any]],
    proposals_by_id: dict[str, dict[str, Any]],
    now: dt.datetime,
) -> list[dict[str, Any]]:
    cutoff = now - dt.timedelta(days=MARKER_WINDOW_DAYS)
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for event in journal:  # append-ordered: later events overwrite
        if not event.get("owner_review_required"):
            continue
        if str(event.get("type")) not in _OWNER_REVIEW_MARKER_KINDS:
            # Unknown marker types must not leak into needs_you: the FE only
            # wires action buttons for _OWNER_REVIEW_MARKER_KINDS, so anything
            # else would render as an item the owner cannot act on.
            continue
        ts = _parse_time(event.get("ts"))
        if ts and ts < cutoff:
            continue
        ref = str(
            event.get("proposal_id")
            or event.get("claim_id")
            or event.get("gate_id")
            or ""
        )
        latest[(str(event.get("type")), ref)] = event
    items: list[dict[str, Any]] = []
    for (event_type, ref), event in latest.items():
        if event_type == "proposal_reject_refused":
            # Resolved once the restage went through (flag cleared) or the
            # proposal reached a terminal state — only surface a live stall.
            proposal = proposals_by_id.get(ref)
            if not proposal or proposal.get("status") != "approved":
                continue
            if not proposal["application"].get("restage_requested"):
                continue
        items.append(
            _item(
                kind=event_type,
                job_id=job_id,
                ref_id=ref,
                summary=str(event.get("owner_review_required"))[:300],
                evidence_ref="journal.jsonl",
                since_ts=event.get("ts"),
            )
        )
    return items


def _tripped_gates(store: JobStore, job_id: str) -> list[dict[str, Any]]:
    from wayfinder_paths.jobs.decision_gates import (
        DECISION_GATES_PATH,
        load_decision_gates,
    )

    items: list[dict[str, Any]] = []
    for gate in load_decision_gates(store, job_id).get("gates") or []:
        if gate.get("status") != "tripped_needs_owner":
            continue
        gate_id = str(gate.get("gate_id"))
        items.append(
            _item(
                kind="decision_gate_tripped",
                job_id=job_id,
                ref_id=gate_id,
                summary=(
                    f"decision gate {gate_id} criteria met on a live-capable "
                    "job — pre-registered response is "
                    f"{gate.get('on_met')}; owner resolves or re-arms "
                    "(wayfinder job decision-gate resolve/reopen)"
                )[:300],
                evidence_ref=DECISION_GATES_PATH,
                since_ts=gate.get("tripped_at"),
            )
        )
    return items


def _decided_autonomously(
    store: JobStore, job_id: str, journal: list[dict[str, Any]], now: dt.datetime
) -> list[dict[str, Any]]:
    cutoff = now - dt.timedelta(days=DECIDED_WINDOW_DAYS)
    legs_by_name = _probation_legs(store, job_id, journal)
    items: list[dict[str, Any]] = []
    for event in journal:
        ts = _parse_time(event.get("ts"))
        if ts is None or ts < cutoff:
            continue
        decided = _decided_item(job_id, event, legs_by_name)
        if decided is not None:
            items.append(decided)
    items.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)
    return items[:DECIDED_CAP]


def _probation_legs(
    store: JobStore, job_id: str, journal: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    if not any(str(e.get("type")) in _PROBATION_EVENTS for e in journal):
        return {}
    from wayfinder_paths.jobs.probation import load_probation

    return {
        str(leg.get("name")): leg
        for leg in load_probation(store, job_id).get("legs") or []
        if isinstance(leg, dict)
    }


def _probation_evidence(
    event: dict[str, Any],
    decision: str,
    legs_by_name: dict[str, dict[str, Any]],
) -> str:
    """Owner-readable probation evidence: ALWAYS lead with the leg name (an
    owner read a bare 'Killed' chip as the whole JOB being killed), then the
    registered criterion for the decision when one exists."""
    leg_name = str(event.get("leg") or "")
    text = f"leg {leg_name} {decision}"
    leg = legs_by_name.get(leg_name) or {}
    criterion_key = "kill" if decision == "killed" else "graduate"
    criterion = str((leg.get(criterion_key) or {}).get("criterion") or "").strip()
    if criterion:
        prefix = "graduates when" if decision == "opened" else "criterion"
        text = f"{text} — {prefix}: {criterion}"
    entry = event.get("entry")
    if decision == "opened" and entry:
        text = f"{text} — entry: {_compact(entry)}"
    return text[:300]


def _decided_item(
    job_id: str,
    event: dict[str, Any],
    legs_by_name: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    event_type = str(event.get("type") or "")
    ts = event.get("ts")
    if event_type in {"proposal_auto_applied", "maintenance_auto_applied"}:
        # maintenance_auto_applied is the current maintenance-lane event;
        # proposal_auto_applied with tier=behavior_equivalence is its shape in
        # journals written before the lanes were split.
        tier = str(event.get("tier") or "paper")
        return {
            "kind": (
                "maintenance_auto_apply"
                if tier == "behavior_equivalence"
                else "paper_auto_apply"
            ),
            "job_id": job_id,
            "ref_id": event.get("proposal_id"),
            "ts": ts,
            "decision": "auto_applied",
            "evidence": _compact(event.get("evidence")),
            "undo": event.get("undo"),
        }
    if event_type == "proposal_expired_unapprovable":
        return {
            "kind": "proposal_expired",
            "job_id": job_id,
            "ref_id": event.get("proposal_id"),
            "ts": ts,
            "decision": "rejected_unapprovable",
            "evidence": _compact(event.get("reasons")),
            "undo": event.get("undo"),
        }
    if event_type == "gate_auto_resolved":
        return {
            "kind": "decision_gate",
            "job_id": job_id,
            "ref_id": event.get("gate_id"),
            "ts": ts,
            "decision": str(event.get("action") or "retire_and_pivot"),
            "evidence": _compact(
                {"criteria": event.get("criteria"), "measured": event.get("measured")}
            ),
            "undo": event.get("undo"),
        }
    if event_type == "exhaustion_claim_audit_passed":
        undo: dict[str, Any] = {
            "command": (
                f"wayfinder job exhaustion reopen {job_id} "
                f"{event.get('claim_id')} --reason '<why>'"
            )
        }
        if event.get("owner_override_until"):
            undo["window_expires_ts"] = event.get("owner_override_until")
        return {
            "kind": "exhaustion_claim_audit",
            "job_id": job_id,
            "ref_id": event.get("claim_id"),
            "ts": ts,
            "decision": str(event.get("audit_verdict") or "pass"),
            "evidence": f"coverage-audit certificate {event.get('certificate')}",
            "undo": undo,
        }
    if (
        event_type == "exhaustion_claim_rejected"
        and event.get("by") == "coverage-audit"
    ):
        return {
            "kind": "exhaustion_claim_audit",
            "job_id": job_id,
            "ref_id": event.get("claim_id"),
            "ts": ts,
            "decision": "reject",
            "evidence": _compact(event.get("reason_codes")),
        }
    if event_type in _LIFECYCLE_PARK_EVENTS:
        return {
            "kind": "lifecycle_park",
            "job_id": job_id,
            "ref_id": job_id,
            "ts": ts,
            "decision": event_type.removeprefix("job_parked_"),
            "evidence": _compact(
                {
                    "reason": event.get("reason"),
                    "predicates_failed": event.get("predicates_failed"),
                }
            ),
            "undo": event.get("undo"),
        }
    if event_type in _PROBATION_EVENTS:
        decision = (
            "opened"
            if event_type in _PROBATION_OPENED_EVENTS
            else event_type.removeprefix("probation_leg_")
        )
        return {
            "kind": "probation",
            "job_id": job_id,
            "ref_id": event.get("leg"),
            "ts": ts,
            "decision": decision,
            "evidence": _probation_evidence(event, decision, legs_by_name),
        }
    return None


def _item(
    *,
    kind: str,
    job_id: str,
    ref_id: str,
    summary: str,
    evidence_ref: str,
    since_ts: Any,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "job_id": job_id,
        "ref_id": ref_id,
        "summary": summary,
        "evidence_ref": evidence_ref,
        "since_ts": since_ts,
    }


def _compact(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value[:300]
    return json.dumps(value, default=str, sort_keys=True)[:300]


def _parse_time(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed
