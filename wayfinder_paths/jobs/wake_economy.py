"""Evidence-driven LLM wakes for paper research jobs.

Scheduled wakes skip mechanically when the compact decision watermark has
not changed since the last delivered LLM wake. A bounded heartbeat still
runs periodically, while actionable workflow state (an impasse mandate,
pending claim/proposal, or remediation without an accountable outcome)
always defeats the skip. Event-triggered, apply, auto, and live wakes keep
their existing immediate behavior.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Mapping
from typing import Any

from wayfinder_paths.jobs.exhaustion import (
    RESEARCH_IMPASSE_PATH,
    RESEARCH_LANE_PATH,
    list_exhaustion_claims,
)
from wayfinder_paths.jobs.halt import read_halt
from wayfinder_paths.jobs.lifecycle import is_operational
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.probation import load_probation
from wayfinder_paths.jobs.remediation import (
    REMEDIATION_MAX_QUIET_SECONDS,
    _evidence_watermark,
    _fingerprint,
    _watermark_reasons,
    compact_remediation,
    load_remediation,
)
from wayfinder_paths.jobs.store import JobStore

WAKE_ECONOMY_PATH = "state/wake_economy.json"
# Kill-switch: "0" disables the skip path entirely.
WAKE_ECONOMY_ENV = "WAYFINDER_WAKE_ECONOMY"
WAKE_QUIET_MAX_HOURS_ENV = "WAYFINDER_WAKE_QUIET_MAX_HOURS"
DEFAULT_WAKE_QUIET_MAX_HOURS = 12.0
SKIP_REASON = "saturation_watermark_unchanged"

REMEDIATION_QUIET_LINE = (
    "Remediation is evidence-blocked and backed off — it does NOT satisfy "
    "this wake. Proceed to research obligations: experiment | probation leg "
    "| exhaustion claim.\n\n"
)


def wake_economy_enabled() -> bool:
    return os.environ.get(WAKE_ECONOMY_ENV) != "0"


def research_saturation_posture(store: JobStore, job_id: str) -> dict[str, Any]:
    """Return ``saturated`` when no actionable intervention is outstanding.

    Stable research-lane labels, mature forward samples, and a parallel
    evolution campaign are evidence context, not reasons to wake an LLM on
    every timer tick. Their material transitions remain in the watermark.
    """
    blockers: list[str] = []
    if not is_operational(store, job_id):
        # Bootstrap owns a never-operational job; the economy never starves it.
        blockers.append("not_operational")
    impasse = store.read_json(job_id, RESEARCH_IMPASSE_PATH, default={}) or {}
    if impasse.get("alerted_at") or impasse.get("status"):
        blockers.append("impasse_mandate_outstanding")
    if list_exhaustion_claims(store, job_id, status="pending"):
        blockers.append("exhaustion_claim_pending")
    scorecard = store.read_json(job_id, "scorecard.json", default={}) or {}
    if (
        int(scorecard.get("pending_proposals") or 0) > 0
        or int(scorecard.get("queued_proposal_applications") or 0) > 0
        or int(scorecard.get("applying_proposal_applications") or 0) > 0
    ):
        blockers.append("proposals_in_flight")
    case = load_remediation(store, job_id)
    if (
        case
        and str(case.get("state"))
        in {"open", "evaluating", "blocked", "proposal_pending"}
        and not remediation_backed_off(store, job_id, case)
    ):
        blockers.append("remediation_case_active")
    return {"posture": "in_flight" if blockers else "saturated", "blockers": blockers}


def saturation_watermark(
    store: JobStore, job_id: str, *, job: WayfinderJob | None = None
) -> dict[str, Any]:
    """Deterministic snapshot of everything a full wake adjudicates — same
    construction as remediation's evidence watermark. A change in ANY
    component means the next wake runs in full. Cheap file reads only."""
    job = job or store.load(job_id)
    summary = store.read_json(job_id, "results/forward/summary.json", default={}) or {}
    trades = summary.get("trades") or {}
    impasse = store.read_json(job_id, RESEARCH_IMPASSE_PATH, default={}) or {}
    halt = read_halt(store.job_dir(job_id)) or {}
    campaign = (
        store.read_json(job_id, "state/evolution_campaign.json", default={}) or {}
    )
    experiment = (
        store.read_json(job_id, "state/evolution_experiment.json", default={}) or {}
    )
    experiment_arms = experiment.get("arms") or {}
    experiment_proposals = experiment.get("proposals") or {}
    lane = store.read_json(job_id, RESEARCH_LANE_PATH, default={}) or {}
    claims = sorted(
        [
            (str(claim.get("claim_id")), str(claim.get("status") or ""))
            for claim in list_exhaustion_claims(store, job_id)
        ]
    )
    probation_legs = sorted(
        [
            (str(leg.get("name")), str(leg.get("status") or ""))
            for leg in load_probation(store, job_id).get("legs") or []
            if isinstance(leg, Mapping)
        ]
    )
    probation_trials = sorted(
        [
            (
                str(trial.get("trial_id")),
                str(trial.get("status") or ""),
                str(trial.get("phase") or ""),
                str((trial.get("promotion") or {}).get("status") or ""),
            )
            for trial in load_probation(store, job_id).get("trials") or []
            if isinstance(trial, Mapping)
        ]
    )
    risk_overrides = (
        store.read_json(job_id, "state/risk_overrides.json", default={}) or {}
    )
    remediation = load_remediation(store, job_id) or {}
    remediation_progress = (remediation.get("progress") or {}).get("watermark")
    experiment_watermark = None
    if experiment:
        proposal_watermarks = {}
        for arm in ("control", "evolution"):
            proposal = experiment_proposals.get(arm) or {}
            active = proposal.get("active")
            history = proposal.get("history") or []
            proposal_watermarks[arm] = {
                "active": {
                    key: active.get(key)
                    for key in ("candidate_id", "revision", "status")
                }
                if isinstance(active, Mapping)
                else None,
                "latest": {
                    key: history[-1].get(key)
                    for key in ("candidate_id", "revision", "status")
                }
                if history and isinstance(history[-1], Mapping)
                else None,
            }
        experiment_watermark = {
            "experiment_id": experiment.get("experiment_id"),
            "status": experiment.get("status"),
            "admissions": experiment.get("admissions"),
            "champions": {
                arm: ((experiment_arms.get(arm) or {}).get("champion") or {}).get(
                    "revision"
                )
                for arm in ("control", "evolution")
            },
            "proposals": proposal_watermarks,
        }
    return {
        "claims": {
            "count": len(claims),
            "fingerprint": _fingerprint({"claims": claims}),
        },
        "lane": {
            key: lane.get(key)
            for key in (
                "active_lane",
                "opened_from_claim",
                "settled_lane",
                "reopened_claim",
            )
            if lane.get(key) is not None
        },
        "mandate": {
            "status": impasse.get("status"),
            "alerted_at": impasse.get("alerted_at"),
        },
        "closed_trades": int(trades.get("closed_count") or 0),
        "last_trade_at": trades.get("last_trade_at"),
        "probation_legs": {
            "count": len(probation_legs),
            "fingerprint": _fingerprint({"legs": probation_legs}),
        },
        "probation_trials": {
            "count": len(probation_trials),
            "fingerprint": _fingerprint({"trials": probation_trials}),
        },
        "pending_proposals": sorted(
            str(proposal.get("proposal_id"))
            for proposal in store.proposals(job_id)
            if proposal.get("status") == "pending"
        ),
        "incumbent_revision": job.versioning.get("active_revision"),
        "remediation": {
            "case_id": remediation.get("case_id"),
            "state": remediation.get("state"),
            "proposal_id": remediation.get("proposal_id"),
            "health_fingerprint": (remediation.get("health") or {}).get("fingerprint"),
            "evidence_fingerprint": _fingerprint(
                {"watermark": dict(remediation_progress)}
            )
            if isinstance(remediation_progress, Mapping)
            else None,
        }
        if remediation
        else None,
        "evolution_campaign": {
            key: campaign.get(key)
            for key in (
                "campaign_id",
                "status",
                "stage",
                "deadline_at",
                "finalize_attempts",
            )
        }
        if campaign
        else None,
        "evolution_experiment": experiment_watermark,
        "halt": {"source": halt.get("source"), "ts": halt.get("ts")} if halt else None,
        "risk_symbol_blocks": sorted(
            symbol
            for symbol, block in (risk_overrides.get("symbols") or {}).items()
            if isinstance(block, Mapping) and block.get("status") == "blocked"
        ),
    }


def remediation_backed_off(
    store: JobStore, job_id: str, case: Mapping[str, Any] | None
) -> bool:
    """Quiet = the agent already recorded its bounded evaluation/blocker and
    no forward evidence has moved since the note — the exact evidence-blocked
    state the remediation backoff was built for. Open and proposal_pending
    cases (and any evidence movement) are mid-flight, never quiet."""
    if not case or str(case.get("state")) not in {"evaluating", "blocked"}:
        return False
    progress = case.get("progress") or {}
    prior = progress.get("watermark")
    if not isinstance(prior, Mapping):
        return False  # no accountable outcome recorded yet
    current = _evidence_watermark(
        store,
        job_id,
        incumbent_revision=(case.get("health") or {}).get("incumbent_revision"),
    )
    return not _watermark_reasons(prior, current)


def remediation_wake_block(case: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Compact remediation view for the standing-checks block: current state,
    the recheck ladder, and when the next evidence recheck is due."""
    compact = compact_remediation(case)
    if not compact or case is None:
        return None
    recheck_raw = compact.get("recheck")
    recheck: Mapping[str, Any] = recheck_raw if isinstance(recheck_raw, Mapping) else {}
    next_recheck_at = None
    anchors = [
        _parse_time(case.get("last_wake_requested_at")),
        _parse_time((case.get("progress") or {}).get("recorded_at")),
    ]
    anchored = [anchor for anchor in anchors if anchor is not None]
    if anchored and recheck:
        wait = min(
            int(recheck.get("next_retry_seconds") or 0),
            REMEDIATION_MAX_QUIET_SECONDS,
        )
        next_recheck_at = (max(anchored) + dt.timedelta(seconds=wait)).isoformat()
    return {
        "state": compact.get("state"),
        "recheck": dict(recheck) if recheck else None,
        "next_recheck_at": next_recheck_at,
    }


def maybe_skip_wake(
    store: JobStore,
    job: WayfinderJob,
    *,
    mode: str,
    apply_proposal_id: str | None,
    force: bool = False,
    wake_source: str = "scheduled_timer",
    wake_triggers: list[str] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any] | None:
    """Pre-spawn skip decision, called BEFORE any OpenCode session work.

    Returns the quiet report (already written to reports/{mode}/latest.json)
    when this wake should be skipped, else None for a normal full wake.
    """
    if not wake_economy_enabled() or force:
        return None
    if apply_proposal_id is not None or mode == "auto":
        # Apply wakes always run; auto wakes can act on markets — the
        # economy meters research wakes only.
        return None
    if str(job.script_loop.mode or "paper") == "live":
        return None  # live jobs NEVER skip
    now = now or dt.datetime.now(dt.UTC)
    state = store.read_json(job.id, WAKE_ECONOMY_PATH, default={}) or {}
    last_full = _parse_time(state.get("last_full_wake_at"))
    if last_full is None or (now - last_full).total_seconds() >= _quiet_max_seconds():
        return None  # max-quiet floor: a full wake is due regardless
    if research_saturation_posture(store, job.id)["posture"] != "saturated":
        return None
    watermark = saturation_watermark(store, job.id, job=job)
    if watermark != state.get("watermark"):
        return None  # evidence moved — the full wake adjudicates it
    next_full_wake_by = (
        last_full + dt.timedelta(seconds=_quiet_max_seconds())
    ).isoformat()
    prior_skips = dict(state.get("skips") or {})
    skips = {
        "count": int(prior_skips.get("count") or 0) + 1,
        "first_skipped_at": prior_skips.get("first_skipped_at") or now.isoformat(),
        "last_skipped_at": now.isoformat(),
    }
    store.write_json(job.id, WAKE_ECONOMY_PATH, {**state, "skips": skips})
    report = {
        "job_id": job.id,
        "mode": mode,
        "status": "quiet",
        "outcome": "no_change",
        "material_change": False,
        "skip_reason": SKIP_REASON,
        "wake_source": wake_source,
        "wake_triggers": sorted(set(wake_triggers or [])),
        "decision_watermark_hash": _fingerprint({"watermark": watermark}),
        "summary": (
            "wake skipped: research saturated and the evidence watermark is "
            f"unchanged since the last full wake; next full wake by "
            f"{next_full_wake_by}"
        ),
        "watermark": watermark,
        "next_full_wake_by": next_full_wake_by,
        "skips": skips,
        "queued": False,
        "session_id": None,
        "created_at": now.isoformat(),
    }
    report_dir = store.job_dir(job.id) / "reports" / mode
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "latest.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # Deduped heartbeat (the remediation note-dedupe pattern): one journal
    # entry per saturation episode; repeat skips roll into the state counter.
    if not prior_skips:
        store.append_journal(
            job.id,
            {
                "type": "wake_skipped_saturated",
                "mode": mode,
                "wake_source": wake_source,
                "next_full_wake_by": next_full_wake_by,
            },
        )
    return report


def record_full_wake(
    store: JobStore,
    job: WayfinderJob,
    *,
    wake_source: str = "scheduled_timer",
    now: dt.datetime | None = None,
) -> None:
    """Stamp the wake-economy state after a full wake is queued: the next
    skip window anchors here and the skip episode counter resets."""
    now = now or dt.datetime.now(dt.UTC)
    watermark = saturation_watermark(store, job.id, job=job)
    store.write_json(
        job.id,
        WAKE_ECONOMY_PATH,
        {
            "last_full_wake_at": now.isoformat(),
            "last_full_wake_source": wake_source,
            "decision_watermark_hash": _fingerprint({"watermark": watermark}),
            "watermark": watermark,
        },
    )


def _quiet_max_seconds() -> float:
    return (
        float(os.environ.get(WAKE_QUIET_MAX_HOURS_ENV) or DEFAULT_WAKE_QUIET_MAX_HOURS)
        * 3600
    )


def _parse_time(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed
