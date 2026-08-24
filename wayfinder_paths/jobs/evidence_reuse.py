"""Shared evidence-reuse eligibility across the proposal lifecycle.

One mechanical question, asked at more than one phase: is the frozen
propose-time evidence PROVABLY about the exact candidate + dataset in front
of us right now, and is it green? Apply (see `application.
assess_validation_reuse`) and revalidate (`proposals.revalidate_proposal`)
both call `assess_evidence_reuse` — the conditions are content-derived hash
comparisons and frozen field reads, never trust-based.

Phase differences, deliberately small:
- "apply" requires the WHOLE report green (validation passed AND
  economic.ready True) — apply consumes the report as-is.
- "revalidate" requires only the validation half green: revalidate exists to
  CURE poisoned reports, and it re-runs the economic evaluation
  unconditionally (the #700 incident cure) — reusing the expensive
  validation backtest must never short-circuit that.

Freshness bound (owner policy): for LIVE-CAPABLE jobs, evidence whose
underlying dataset was fetched more than `WAYFINDER_EVIDENCE_MAX_AGE_HOURS`
(default 24) ago is refused for reuse — and the apply path additionally
refuses to blindly recompute against the same stale bars (routing to
refresh + revalidate instead). Paper jobs reuse regardless of age
(containment). A dataset with no recorded `fetched_at` has unknown age and
is not treated as stale — the live gate's 30-day backtest-age ceiling still
bounds it.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.validation import candidate_dataset_fingerprint

# Kill-switches — one per reuse surface, all following #705's env pattern
# ("=1" forces the pre-reuse behavior for that surface only).
APPLY_ALWAYS_REVALIDATE_ENV = "WAYFINDER_APPLY_ALWAYS_REVALIDATE"
REVALIDATE_ALWAYS_RERUN_ENV = "WAYFINDER_REVALIDATE_ALWAYS_RERUN"

EVIDENCE_MAX_AGE_ENV = "WAYFINDER_EVIDENCE_MAX_AGE_HOURS"
DEFAULT_EVIDENCE_MAX_AGE_HOURS = 24.0

_PHASE_KILL_SWITCH = {
    "apply": APPLY_ALWAYS_REVALIDATE_ENV,
    "revalidate": REVALIDATE_ALWAYS_RERUN_ENV,
}


def evidence_max_age_hours() -> float:
    raw = os.environ.get(EVIDENCE_MAX_AGE_ENV, "")
    try:
        return float(raw) if raw else DEFAULT_EVIDENCE_MAX_AGE_HOURS
    except ValueError:
        return DEFAULT_EVIDENCE_MAX_AGE_HOURS


def dataset_fetched_at(fingerprint: dict[str, Any] | None) -> str | None:
    """The `metadata.fetched_at` stamp of the fingerprinted dataset file
    (written by fetch-dataset). None for bare row-list files or torn reads."""
    if not fingerprint or not fingerprint.get("path"):
        return None
    try:
        loaded = json.loads(Path(str(fingerprint["path"])).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    match loaded:
        case {"metadata": dict() as meta}:
            value = meta.get("fetched_at")
            return str(value) if value else None
    return None


def _dataset_age_hours(fetched_at: str | None) -> float | None:
    if not fetched_at:
        return None
    try:
        stamp = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (datetime.now(UTC) - stamp).total_seconds() / 3600.0


def assess_evidence_reuse(
    store: JobStore,
    job_id: str,
    proposal: dict[str, Any],
    candidate_dir: Path,
    *,
    phase: str = "apply",
) -> dict[str, Any]:
    """Mechanical eligibility check for reusing the frozen propose-time
    validation evidence instead of re-running the expensive candidate
    backtest. Returns `{"eligible": bool, "reason": str, "proof": dict}` —
    `reason` names the FIRST failed condition when ineligible; `proof`
    carries the content-derived identity when eligible.

    Conditions, in order:
    1. phase kill-switch off
    2. frozen candidate_report present, mode "full", with a revision
    3. active workspace revision has not drifted past the staged base
       (apply also accepts active == candidate: crash-resume after promotion)
    4. candidate dir still hashes to the report's revision
    5. dataset fingerprint recorded at propose matches the one re-derived now
    6. freshness bound: live-capable jobs refuse evidence whose dataset
       `fetched_at` age exceeds the configured maximum (reason
       "dataset_stale" — the apply path escalates this to a hard refusal)
    7. the frozen evidence is green — validation passed always; economic
       ready additionally required for phase "apply"
    """
    if phase not in _PHASE_KILL_SWITCH:
        raise ValueError(f"unknown evidence-reuse phase: {phase}")
    if os.environ.get(_PHASE_KILL_SWITCH[phase]) == "1":
        return {"eligible": False, "reason": "kill_switch", "proof": {}}
    report = proposal.get("candidate_report") or {}
    report_revision = str(report.get("revision") or "")
    if not report or report.get("mode") != "full" or not report_revision:
        return {"eligible": False, "reason": "report_missing_or_not_full", "proof": {}}
    base_revision = str(proposal.get("base_revision") or "")
    active_revision = compute_workspace_revision(store.job_dir(job_id))
    allowed_active = (
        (base_revision, report_revision) if phase == "apply" else (base_revision,)
    )
    if base_revision and active_revision not in allowed_active:
        return {
            "eligible": False,
            "reason": "baseline_drift",
            "proof": {
                "base_revision": base_revision,
                "active_revision": active_revision,
            },
        }
    candidate_revision = compute_workspace_revision(candidate_dir)
    if candidate_revision != report_revision:
        return {
            "eligible": False,
            "reason": "candidate_mismatch",
            "proof": {
                "report_revision": report_revision,
                "candidate_revision": candidate_revision,
            },
        }
    recorded_fingerprint = report.get("dataset_fingerprint")
    current_fingerprint = candidate_dataset_fingerprint(
        candidate_dir, store.job_dir(job_id)
    )
    if not recorded_fingerprint or recorded_fingerprint != current_fingerprint:
        return {
            "eligible": False,
            "reason": (
                "dataset_changed" if recorded_fingerprint else "no_dataset_fingerprint"
            ),
            "proof": {
                "recorded_fingerprint": recorded_fingerprint,
                "current_fingerprint": current_fingerprint,
            },
        }
    if store._job_is_live_capable(job_id):
        fetched_at = dataset_fetched_at(current_fingerprint)
        age_hours = _dataset_age_hours(fetched_at)
        max_age = evidence_max_age_hours()
        if age_hours is not None and age_hours > max_age:
            return {
                "eligible": False,
                "reason": "dataset_stale",
                "proof": {
                    "fetched_at": fetched_at,
                    "age_hours": round(age_hours, 3),
                    "max_age_hours": max_age,
                    "live_capable": True,
                },
            }
    validation_status = (report.get("validation_summary") or {}).get("status")
    economic_ready = (report.get("economic") or {}).get("ready")
    green = validation_status == "passed" and (
        phase != "apply" or economic_ready is True
    )
    if not green:
        return {
            "eligible": False,
            "reason": "report_not_green",
            "proof": {
                "validation_status": validation_status,
                "economic_ready": economic_ready,
            },
        }
    report_hash = hashlib.sha256(
        json.dumps(report, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return {
        "eligible": True,
        "reason": "",
        "proof": {
            "phase": phase,
            "base_revision": base_revision,
            "candidate_revision": candidate_revision,
            "active_revision": active_revision,
            "dataset_fingerprint": current_fingerprint,
            "report_hash": report_hash,
        },
    }
