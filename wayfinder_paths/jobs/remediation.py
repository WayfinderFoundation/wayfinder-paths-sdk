"""Durable closed-loop remediation for incumbent health alarms.

The detector is deliberately descriptive and the trading response remains
owner-governed.  This module owns the missing orchestration state between a
warning/critical report and an agent-authored proposal: material evidence
opens (or refreshes) one case, scheduled ticks keep an unhandled case awake,
and proposals are stamped back to the evidence that caused them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

REMEDIATION_PATH = "state/regime_remediation.json"
REMEDIATION_SCHEMA_VERSION = "1.0"
# Re-wake pacing for an open case WITHOUT new material evidence. 30 minutes
# churned in production: nearly every scheduled tick re-woke the agent
# (42/45 trigger wakes were scheduled-tick retries; 27 progress notes vs 2
# evidence updates), and each wake produced another bounded progress note
# that changed nothing before the next retry. Material evidence still wakes
# immediately (the branch above the retry path), a recorded progress/blocker
# note now counts as activity, and quiet retries wait 6 hours.
REMEDIATION_RETRY_SECONDS = 6 * 60 * 60

_ALERT_STATUSES = frozenset({"warning", "critical"})
_STATUS_RANK = {
    "insufficient": 0,
    "healthy": 1,
    "watch": 2,
    "warning": 3,
    "critical": 4,
}
_ACTIONABLE_STATES = frozenset({"open", "evaluating", "blocked"})


def load_remediation(store: JobStore, job_id: str) -> dict[str, Any] | None:
    doc = store.read_json(job_id, REMEDIATION_PATH)
    return dict(doc) if isinstance(doc, Mapping) else None


def sync_remediation_with_health(
    store: JobStore,
    job_id: str,
    report: Mapping[str, Any],
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any] | None:
    """Open/refresh a case and return the agent event that is due.

    Called only by the scheduled driver.  Preflight/manual ticks may compute
    health, but cannot consume or dispatch the durable alert.
    """
    status = str(report.get("status") or "insufficient")
    now = now or dt.datetime.now(dt.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    now_iso = now.astimezone(dt.UTC).isoformat()
    current = load_remediation(store, job_id)
    if status not in _ALERT_STATUSES:
        if (
            current
            and current.get("state") == "monitoring"
            and status in {"healthy", "watch"}
        ):
            current.update(
                {
                    "state": "resolved",
                    "updated_at": now_iso,
                    "resolved_at": now_iso,
                    "resolved_health_status": status,
                }
            )
            store.write_json(job_id, REMEDIATION_PATH, current)
            store.append_journal(
                job_id,
                {
                    "type": "regime_remediation_resolved",
                    "case_id": current.get("case_id"),
                    "health_status": status,
                },
            )
        return None

    evidence = _health_evidence(report)
    material_reasons = _material_reasons(current, evidence)

    if current is None or material_reasons:
        prior_state = str((current or {}).get("state") or "")
        continuing_case = bool(
            current and prior_state in _ACTIONABLE_STATES | {"proposal_pending"}
        )
        case_id = (
            str((current or {}).get("case_id"))
            if continuing_case
            else _case_id(job_id, str(evidence["fingerprint"]))
        )
        state = prior_state if prior_state == "evaluating" else "open"
        case = {
            "schema_version": REMEDIATION_SCHEMA_VERSION,
            "job_id": job_id,
            "case_id": case_id,
            "state": state,
            "opened_at": (
                (current or {}).get("opened_at") if continuing_case else now_iso
            ),
            "updated_at": now_iso,
            "last_wake_requested_at": now_iso,
            "attempts": (
                int((current or {}).get("attempts") or 0) + 1 if continuing_case else 1
            ),
            "proposal_id": None,
            "superseded_proposal_id": (current or {}).get("proposal_id"),
            "health": evidence,
            "material_reasons": material_reasons or ["entered_alert_state"],
            "supersedes_fingerprint": (current or {})
            .get("health", {})
            .get("fingerprint")
            if current
            else None,
        }
        store.write_json(job_id, REMEDIATION_PATH, case)
        store.append_journal(
            job_id,
            {
                "type": "regime_remediation_opened"
                if not continuing_case
                else "regime_remediation_evidence_updated",
                "case_id": case_id,
                "state": state,
                "health_status": status,
                "evidence_fingerprint": evidence["fingerprint"],
                "material_reasons": case["material_reasons"],
            },
        )
        return {
            "event": "regime_shift",
            "case_id": case_id,
            "evidence_fingerprint": evidence["fingerprint"],
            "material_reasons": case["material_reasons"],
        }

    state = str(current.get("state") or "open")
    if state not in _ACTIONABLE_STATES:
        return None
    # Progress-only ticks must not re-wake: a bounded evaluation/blocker note
    # recorded since the last wake is the agent already working the case.
    last_wake = _parse_time(current.get("last_wake_requested_at"))
    progress_at = _parse_time((current.get("progress") or {}).get("recorded_at"))
    anchors = [ts for ts in (last_wake, progress_at) if ts is not None]
    if anchors and (now - max(anchors)).total_seconds() < REMEDIATION_RETRY_SECONDS:
        return None

    current["updated_at"] = now_iso
    current["last_wake_requested_at"] = now_iso
    current["attempts"] = int(current.get("attempts") or 0) + 1
    current["health"] = evidence
    store.write_json(job_id, REMEDIATION_PATH, current)
    return {
        "event": "regime_remediation_due",
        "case_id": current.get("case_id"),
        "evidence_fingerprint": evidence["fingerprint"],
        "material_reasons": ["open_case_without_proposal"],
    }


def proposal_remediation_stamp(store: JobStore, job_id: str) -> dict[str, Any] | None:
    """Trusted evidence stamp for a proposal authored during an open case."""
    case = load_remediation(store, job_id)
    if not case or str(case.get("state")) not in _ACTIONABLE_STATES:
        return None
    health = case.get("health") or {}
    return {
        "case_id": case.get("case_id"),
        "evidence_fingerprint": health.get("fingerprint"),
        "health_status": health.get("status"),
        "health_score": health.get("score"),
        "source_revision": health.get("incumbent_revision"),
        "signals": list(health.get("signals") or []),
    }


def link_remediation_proposal(
    store: JobStore,
    job_id: str,
    proposal: Mapping[str, Any],
) -> None:
    stamp = proposal.get("remediation")
    if not isinstance(stamp, Mapping):
        return
    case = load_remediation(store, job_id)
    if not case or case.get("case_id") != stamp.get("case_id"):
        return
    report = proposal.get("candidate_report") or {}
    gate = report.get("gate") or {}
    economic = report.get("economic") or {}
    candidate_ready = (
        gate.get("live_ready") is True and economic.get("ready") is not False
    )
    case.update(
        {
            "state": "proposal_pending" if candidate_ready else "blocked",
            "proposal_id": proposal.get("proposal_id"),
            "updated_at": utc_now_iso(),
            "candidate_ready": candidate_ready,
            "candidate_gate": {
                "live_ready": gate.get("live_ready"),
                "economic_ready": economic.get("ready"),
                "economic_reasons": list(economic.get("reasons") or []),
            },
        }
    )
    store.write_json(job_id, REMEDIATION_PATH, case)
    store.append_journal(
        job_id,
        {
            "type": "regime_remediation_proposal_linked",
            "case_id": case.get("case_id"),
            "proposal_id": proposal.get("proposal_id"),
            "candidate_ready": candidate_ready,
        },
    )


def update_remediation_progress(
    store: JobStore,
    job_id: str,
    *,
    state: str,
    note: str,
    artifact_path: str | None = None,
) -> dict[str, Any]:
    """Record a bounded evaluation or blocker without closing the case."""
    if state not in {"evaluating", "blocked"}:
        raise ValueError("remediation state must be evaluating or blocked")
    if not note.strip():
        raise ValueError("remediation progress requires a structured note")
    if artifact_path:
        normalized_artifact = str(artifact_path).strip()
        artifact = PurePosixPath(normalized_artifact)
        if (
            len(normalized_artifact) > 500
            or artifact.is_absolute()
            or ".." in artifact.parts
        ):
            raise ValueError("artifact_path must be a bounded job-relative path")
        artifact_path = normalized_artifact
    case = load_remediation(store, job_id)
    if not case or str(case.get("state")) not in _ACTIONABLE_STATES:
        raise ValueError(f"job {job_id} has no actionable remediation case")
    case.update(
        {
            "state": state,
            "updated_at": utc_now_iso(),
            "progress": {
                "note": note.strip()[:2_000],
                "artifact_path": artifact_path,
                "recorded_at": utc_now_iso(),
            },
        }
    )
    store.write_json(job_id, REMEDIATION_PATH, case)
    store.append_journal(
        job_id,
        {
            "type": "regime_remediation_progress",
            "case_id": case.get("case_id"),
            "state": state,
            "artifact_path": artifact_path,
            "note": note.strip()[:300],
        },
    )
    return case


def handle_remediation_rejection(
    store: JobStore,
    job_id: str,
    proposal: Mapping[str, Any],
) -> None:
    stamp = proposal.get("remediation")
    if not isinstance(stamp, Mapping):
        return
    case = load_remediation(store, job_id)
    if not case or case.get("case_id") != stamp.get("case_id"):
        return
    rejection = proposal.get("rejection") or {}
    owner_substantive = (
        rejection.get("by") == "owner"
        and str(rejection.get("kind") or "substantive") == "substantive"
    )
    case.update(
        {
            "state": "owner_accepted_risk" if owner_substantive else "open",
            "proposal_id": None,
            "updated_at": utc_now_iso(),
            "rejection": dict(rejection),
        }
    )
    store.write_json(job_id, REMEDIATION_PATH, case)


def handle_remediation_application(
    store: JobStore,
    job_id: str,
    proposal: Mapping[str, Any],
    *,
    status: str,
) -> None:
    stamp = proposal.get("remediation")
    if not isinstance(stamp, Mapping):
        return
    case = load_remediation(store, job_id)
    if not case or case.get("case_id") != stamp.get("case_id"):
        return
    case.update(
        {
            "state": "monitoring" if status == "applied" else "open",
            "updated_at": utc_now_iso(),
            "application_status": status,
            "applied_at": utc_now_iso() if status == "applied" else None,
        }
    )
    store.write_json(job_id, REMEDIATION_PATH, case)


def compact_remediation(case: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not case:
        return None
    return {
        key: case.get(key)
        for key in (
            "case_id",
            "state",
            "opened_at",
            "updated_at",
            "attempts",
            "proposal_id",
            "superseded_proposal_id",
            "health",
            "material_reasons",
            "candidate_ready",
            "candidate_gate",
            "rejection",
            "progress",
            "application_status",
            "applied_at",
            "resolved_at",
            "resolved_health_status",
        )
        if key in case
    }


def _health_evidence(report: Mapping[str, Any]) -> dict[str, Any]:
    signals = [
        {
            key: signal.get(key)
            for key in ("kind", "severity", "value", "symbol", "window_days")
            if signal.get(key) is not None
        }
        for signal in report.get("signals") or []
        if isinstance(signal, Mapping)
    ]
    incumbent = report.get("incumbent") or {}
    return {
        "fingerprint": report.get("evidence_fingerprint")
        or _fingerprint(
            {
                "status": report.get("status"),
                "score": report.get("score"),
                "signals": signals,
                "incumbent_revision": incumbent.get("workspace_revision"),
            }
        ),
        "status": report.get("status"),
        "score": int(report.get("score") or 0),
        "signals": signals,
        "drawdown": _drawdown_signal(signals),
        "incumbent_revision": incumbent.get("workspace_revision"),
        "computed_at": report.get("computed_at"),
    }


def _material_reasons(
    current: Mapping[str, Any] | None, evidence: Mapping[str, Any]
) -> list[str]:
    if current is None:
        return ["entered_alert_state"]
    prior = current.get("health") or {}
    reasons: list[str] = []
    old_status = str(prior.get("status") or "insufficient")
    new_status = str(evidence.get("status") or "insufficient")
    if _STATUS_RANK.get(new_status, 0) > _STATUS_RANK.get(old_status, 0):
        reasons.append("status_worsened")
    if int(evidence.get("score") or 0) - int(prior.get("score") or 0) >= 2:
        reasons.append("score_increased")
    old_critical = _severity_two_keys(prior.get("signals") or [])
    new_critical = _severity_two_keys(evidence.get("signals") or [])
    if new_critical - old_critical:
        reasons.append("new_severity_two_signal")
    old_drawdown = float(prior.get("drawdown") or 0.0)
    new_drawdown = float(evidence.get("drawdown") or 0.0)
    if new_drawdown - old_drawdown >= 0.02:
        reasons.append("drawdown_worsened_two_points")
    if (
        evidence.get("incumbent_revision")
        and prior.get("incumbent_revision")
        and evidence.get("incumbent_revision") != prior.get("incumbent_revision")
    ):
        reasons.append("incumbent_revision_changed")
    return reasons


def _severity_two_keys(signals: Any) -> set[tuple[str, str, str]]:
    return {
        (
            str(signal.get("kind") or ""),
            str(signal.get("symbol") or ""),
            str(signal.get("window_days") or ""),
        )
        for signal in signals
        if isinstance(signal, Mapping) and int(signal.get("severity") or 0) >= 2
    }


def _drawdown_signal(signals: list[dict[str, Any]]) -> float:
    values = [
        float(signal["value"])
        for signal in signals
        if signal.get("kind") == "drawdown" and signal.get("value") is not None
    ]
    return max(values, default=0.0)


def _case_id(job_id: str, fingerprint: str) -> str:
    return "rem-" + hashlib.sha256(f"{job_id}:{fingerprint}".encode()).hexdigest()[:12]


def _fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(payload), sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _parse_time(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed
