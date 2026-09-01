"""Probation registry: durable, synced state for reduced-size trial legs.

Probation is only honest if its bookkeeping is visible: each leg's size cap,
pre-registered graduate/kill criteria, and progress live in one structured
file (`probation.json`) that rides the job snapshot to the backend — so the
owner watches the same numbers the worker updates, and graduation/kill are
journaled events, not prose."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.archive import find_candidate
from wayfinder_paths.jobs.bundles import copy_job_bundle
from wayfinder_paths.jobs.compute_lock import job_state_lock
from wayfinder_paths.jobs.economics import block_bootstrap_lcb
from wayfinder_paths.jobs.execution.primitives import bar_interval_seconds
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.improver.spec import ImproverSpec, revision_stamp
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.monitor_state import atomic_write_json

PROBATION_PATH = "probation.json"
PROBATION_STATUSES = {"active", "graduated", "killed"}
PAPER_TIER = "paper"
PROBATION_SCHEMA_VERSION = "2.0"
PROBATION_BUNDLE_ROOT = "research/evolution/probation/bundles"
PROBATION_FORWARD_ROOT = "results/forward/probation"
PROBATION_VIEW_PATH = "state/probation_view.json"
TRIAL_ACTIVE_STATUSES = frozenset({"burn_in", "active"})
TRIAL_TERMINAL_STATUSES = frozenset(
    {"graduated", "killed", "inconclusive", "superseded"}
)
# Owner-facing trial identity: a trial card names the STRATEGY, not the
# plumbing that delivered it. "evolution" is the pipeline, never a family.
GENERIC_TRIAL_FAMILY = "unknown-strategy"
TRIAL_SUMMARY_MAX_CHARS = 200
PLACEHOLDER_TRIAL_FAMILIES = frozenset({"", "evolution", GENERIC_TRIAL_FAMILY})
# Circular moving-block bootstrap block length for paired forward deltas.
# Below this many paired days every resample is the full (rotated) sample, so
# the "interval" collapses to the point estimate — report no bounds instead.
FORWARD_BOOTSTRAP_BLOCK_LEN = 5
# Paired equity curve (candidate vs frozen incumbent) on forward trials:
# hourly cumulative realized net PnL buckets since admission, capped so the
# synced payload stays a few KB (14d x 24h = 336 buckets + the seed point).
EQUITY_CURVE_BASIS = "realized"
EQUITY_CURVE_BUCKET_SECONDS = 3600
EQUITY_CURVE_MAX_POINTS = 400


def load_probation(store: JobStore, job_id: str) -> dict[str, Any]:
    doc = store.read_json(job_id, PROBATION_PATH, default={"legs": []}) or {"legs": []}
    if not isinstance(doc, dict):
        doc = {"legs": []}
    doc.setdefault("legs", [])
    doc.setdefault("trials", [])
    doc.setdefault("schema_version", PROBATION_SCHEMA_VERSION)
    return doc


def probation_sync_payload(store: JobStore, job_id: str) -> dict[str, Any]:
    """The synced probation document: probation.json with each trial's paired
    equity curve attached from its sidecar. Trials without a curve (legacy,
    pre-forward, absent streams) simply omit the field — the FE handles it."""
    doc = store.read_json(job_id, PROBATION_PATH, default={"legs": []}) or {"legs": []}
    if not isinstance(doc, dict):
        return {"legs": []}
    for trial in doc.get("trials") or []:
        curve = trial_equity_curve_payload(
            store, job_id, str(trial.get("trial_id") or "")
        )
        if curve is not None:
            trial["equity_curve"] = curve
    return doc


def trial_equity_curve_payload(
    store: JobStore, job_id: str, trial_id: str
) -> dict[str, Any] | None:
    """Wire shape of a trial's equity curve: points/updated_at/basis only —
    the sidecar's byte-offset cursor is producer-internal."""
    if not trial_id or Path(trial_id).name != trial_id:
        return None
    doc = store.read_json(job_id, _equity_curve_relative(trial_id), default=None)
    if not isinstance(doc, dict) or not doc.get("points"):
        return None
    return {
        "points": list(doc["points"])[:EQUITY_CURVE_MAX_POINTS],
        "updated_at": doc.get("updated_at"),
        "basis": doc.get("basis"),
    }


def _equity_curve_relative(trial_id: str) -> str:
    return f"{PROBATION_FORWARD_ROOT}/{trial_id}/equity_curve.json"


def record_probation_leg(
    store: JobStore,
    job_id: str,
    *,
    name: str,
    symbol: str,
    size_fraction: float,
    graduate_criterion: str,
    kill_criterion: str,
    graduate_rules: dict[str, Any] | None = None,
    kill_rules: dict[str, Any] | None = None,
    proposal_id: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    spec = ImproverSpec.load(store.job_dir(job_id))
    max_fraction = spec.probation_max_size_fraction
    max_legs = spec.probation_max_active_legs
    if not 0 < size_fraction <= max_fraction:
        raise ValueError(f"probation size_fraction must be in (0, {max_fraction}]")
    doc = load_probation(store, job_id)
    active = [leg for leg in doc["legs"] if leg.get("status") == "active"]
    if len(active) >= max_legs:
        raise ValueError(
            f"max {max_legs} concurrent probation legs — graduate or kill one first"
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
        # criterion = human-readable rendering; rules = the machine-evaluable
        # predicates the lifecycle controller actually enforces. A leg without
        # rules is legacy: visible but never auto-graduated/killed.
        "graduate": {
            "criterion": graduate_criterion,
            "rules": dict(graduate_rules or {}),
            "progress": None,
        },
        "kill": {
            "criterion": kill_criterion,
            "rules": dict(kill_rules or {}),
            "status": None,
        },
        "notes": notes,
        **revision_stamp(store.job_dir(job_id)),
    }
    doc["legs"].append(leg)
    store.write_json(job_id, PROBATION_PATH, doc)
    store.append_journal(
        job_id,
        {"type": "probation_leg_opened", "leg": name, "proposal_id": proposal_id},
    )
    return leg


def paper_regression_budget(
    baseline_net: float, *, budget_pct: float, budget_frac: float
) -> float:
    """Allowed net_return giveback for paper entry: the larger of an absolute
    floor and a fraction of the baseline's own magnitude."""
    return max(float(budget_pct), float(budget_frac) * abs(float(baseline_net)))


def paper_entry_check(
    *,
    candidate_net: float,
    baseline_net: float,
    backtest_trades: int,
    spec: ImproverSpec,
) -> dict[str, Any]:
    """Mechanical "not clearly worse" test for the paper probation tier."""
    budget = paper_regression_budget(
        baseline_net,
        budget_pct=spec.paper_regression_budget_pct,
        budget_frac=spec.paper_regression_budget_frac,
    )
    min_trades = spec.paper_min_backtest_trades
    reasons: list[str] = []
    if int(backtest_trades) < min_trades:
        reasons.append(
            f"backtest trade count {backtest_trades} below floor {min_trades}"
        )
    floor = float(baseline_net) - budget
    if float(candidate_net) < floor:
        reasons.append(
            f"candidate net_return {candidate_net} clearly worse than baseline "
            f"{baseline_net} (allowed floor {round(floor, 6)})"
        )
    return {
        "eligible": not reasons,
        "budget": round(budget, 6),
        "floor": round(floor, 6),
        "reasons": reasons,
    }


def _comparison_nets(
    store: JobStore, job_id: str, proposal_id: str
) -> tuple[float, float, int]:
    proposal = store.load_proposal(job_id, proposal_id)
    comparison = (proposal.get("candidate_report") or {}).get("comparison") or {}
    candidate_stats = (comparison.get("candidate") or {}).get("stats") or {}
    baseline_stats = (comparison.get("baseline") or {}).get("stats") or {}
    if "net_return" not in candidate_stats or "net_return" not in baseline_stats:
        raise ValueError(
            f"proposal {proposal_id} has no baseline-vs-candidate net_return "
            "comparison — paper entry needs the propose-time backtest"
        )
    return (
        float(candidate_stats["net_return"]),
        float(baseline_stats["net_return"]),
        int(candidate_stats.get("trade_count") or 0),
    )


def open_paper_probation_leg(
    store: JobStore,
    job_id: str,
    *,
    name: str,
    symbol: str,
    kill_criterion: str,
    kill_rules: dict[str, Any] | None = None,
    graduate_criterion: str = "full strict gate + owner approval (unchanged)",
    graduate_rules: dict[str, Any] | None = None,
    proposal_id: str | None = None,
    candidate_net: float | None = None,
    baseline_net: float | None = None,
    backtest_trades: int | None = None,
    notes: str | None = None,
    candidate_bundle_id: str | None = None,
    candidate_bundle: str | None = None,
    campaign_id: str | None = None,
    candidate_revision: str | None = None,
    forward_context_cutoff: str | None = None,
    shadow_stream: str | None = None,
) -> dict[str, Any]:
    """Open a PAPER probation leg for a candidate that is "not clearly worse"
    than baseline — no baseline beat and no owner approval required, because
    probation is the containment: paper only (size_fraction 0.0, never live
    sizing), capped concurrency, and outcome-driven retirement (registered
    kill predicates PLUS the mechanical flat-zero floor the lifecycle
    controller enforces on every paper leg). Graduation to live is UNCHANGED:
    a paper leg's results feed a normal proposal through the full strict gate
    with owner approval — nothing in probation.json confers live execution.

    Entry evidence comes from the proposal's propose-time comparison
    (`proposal_id`) or explicit candidate/baseline `net_return` figures on
    the same full-history window.
    """
    if proposal_id is not None and candidate_net is None:
        candidate_net, baseline_net, backtest_trades = _comparison_nets(
            store, job_id, proposal_id
        )
    if candidate_net is None or baseline_net is None:
        raise ValueError(
            "paper entry needs candidate/baseline net_return — pass "
            "proposal_id or explicit figures"
        )
    spec = ImproverSpec.load(store.job_dir(job_id))
    check = paper_entry_check(
        candidate_net=candidate_net,
        baseline_net=baseline_net,
        backtest_trades=int(backtest_trades or 0),
        spec=spec,
    )
    if not check["eligible"]:
        # A mechanical refusal is a completed candidate evaluation, not an
        # unattempted paper deployment. Keep it durable so coverage audits do
        # not repeatedly mandate a candidate the entry gate already rejected.
        store.append_journal(
            job_id,
            {
                "type": "paper_probation_entry_refused",
                "leg": name,
                "symbol": symbol,
                "proposal_id": proposal_id,
                "entry": {
                    "candidate_net_return": float(candidate_net),
                    "baseline_net_return": float(baseline_net),
                    "backtest_trades": int(backtest_trades or 0),
                    **check,
                },
            },
        )
        raise ValueError(
            "paper probation entry refused: " + "; ".join(check["reasons"])
        )
    doc = load_probation(store, job_id)
    paper_active = [
        leg
        for leg in doc["legs"]
        if leg.get("status") == "active" and leg.get("tier") == PAPER_TIER
    ]
    if len(paper_active) >= spec.paper_max_active_legs:
        raise ValueError(
            f"max {spec.paper_max_active_legs} concurrent paper probation "
            "legs — retire one first"
        )
    if any(leg.get("name") == name for leg in doc["legs"]):
        raise ValueError(f"probation leg {name!r} already exists")
    candidate_linkage = {
        key: value
        for key, value in {
            "candidate_bundle_id": candidate_bundle_id,
            "candidate_bundle": candidate_bundle,
            "campaign_id": campaign_id,
            "candidate_revision": candidate_revision,
            "forward_context_cutoff": forward_context_cutoff,
            "shadow_stream": shadow_stream,
        }.items()
        if value is not None
    }
    leg = {
        "name": name,
        "symbol": symbol,
        "status": "active",
        "tier": PAPER_TIER,
        "opened_by": "improver",
        "deployed_at": utc_now_iso(),
        # Paper legs never carry live sizing; retirement/graduation evidence
        # is the forward paper stream, adjudicated by the controller.
        "size_fraction": 0.0,
        "proposal_id": proposal_id,
        **candidate_linkage,
        "entry": {
            "candidate_net_return": float(candidate_net),
            "baseline_net_return": float(baseline_net),
            "backtest_trades": int(backtest_trades or 0),
            **check,
        },
        "graduate": {
            "criterion": graduate_criterion,
            "rules": dict(graduate_rules or {}),
            "progress": None,
        },
        "kill": {
            "criterion": kill_criterion,
            "rules": dict(kill_rules or {}),
            "status": None,
        },
        "notes": notes,
        **revision_stamp(store.job_dir(job_id)),
    }
    doc["legs"].append(leg)
    store.write_json(job_id, PROBATION_PATH, doc)
    store.append_journal(
        job_id,
        {
            "type": "paper_probation_opened",
            "leg": name,
            "symbol": symbol,
            "proposal_id": proposal_id,
            **{
                key: candidate_linkage[key]
                for key in ("candidate_bundle_id", "campaign_id")
                if key in candidate_linkage
            },
            "entry": leg["entry"],
        },
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


def ensure_unified_probation(
    store: JobStore,
    job_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Normalize the registry and migrate an active legacy evolution A/B.

    Migration is byte/clock preserving: existing c03-style bundles, streams,
    cursors, admission time, and deadline are referenced in place.  The old
    experiment remains as an archived receipt and can no longer stop future
    campaigns.
    """
    current = _aware(now or datetime.now(UTC))
    with job_state_lock(store.repo_root, job_id, name="probation"):
        persisted = store.read_json(job_id, PROBATION_PATH, default={})
        doc = load_probation(store, job_id)
        changed = (
            not isinstance(persisted, dict)
            or persisted.get("schema_version") != PROBATION_SCHEMA_VERSION
            or not isinstance(persisted.get("trials"), list)
        )
        doc["schema_version"] = PROBATION_SCHEMA_VERSION
        experiment = (
            store.read_json(job_id, "state/evolution_experiment.json", default={}) or {}
        )
        if (
            isinstance(experiment, dict)
            and experiment.get("status") == "active"
            and not experiment.get("migrated_to_probation")
        ):
            evolution = (experiment.get("arms") or {}).get("evolution") or {}
            control = (experiment.get("arms") or {}).get("control") or {}
            candidate = evolution.get("champion") or {}
            reference = control.get("champion") or {}
            if candidate.get("source") != "incumbent" and candidate.get("revision"):
                trial_id = _safe_trial_id(
                    str(candidate.get("candidate_id") or candidate["revision"])
                )
                migrated_trial = next(
                    (
                        trial
                        for trial in doc["trials"]
                        if trial.get("legacy_experiment_id")
                        == experiment.get("experiment_id")
                    ),
                    None,
                )
                if migrated_trial is None:
                    family, summary = _resolve_trial_identity(
                        store,
                        job_id,
                        candidate_id=str(
                            candidate.get("candidate_id") or candidate["revision"]
                        ),
                        fallback_family=None,
                        fallback_summary=None,
                    )
                    trial = {
                        "trial_id": trial_id,
                        "candidate_id": candidate.get("candidate_id"),
                        "candidate_revision": candidate.get("revision"),
                        "family": family,
                        "summary": summary,
                        "source": candidate.get("source") or "evolution_campaign",
                        "status": "active",
                        "phase": "forward",
                        "queued_at": candidate.get("admitted_at"),
                        "burn_in": {
                            "status": "passed",
                            "completed_at": experiment.get("started_at"),
                            "basis": "legacy_24h_operational_burn_in",
                        },
                        "forward": {
                            "started_at": experiment.get("started_at"),
                            "deadline_at": experiment.get("ends_at"),
                            "min_paired_days": 7,
                            "max_paired_days": 14,
                            "decision_days": [7, 14],
                            "last_decision_day": 0,
                            "confidence": float(experiment.get("confidence") or 0.90),
                            "metrics": None,
                        },
                        "candidate": {
                            "role": "candidate",
                            "candidate_id": candidate.get("candidate_id"),
                            "revision": candidate.get("revision"),
                            "bundle": candidate.get("bundle"),
                            "bundle_scope": "legacy_experiment",
                            "legacy_shadow_arm": "evolution",
                            "legacy_shadow_role": "champion",
                            "stream": candidate.get("stream"),
                            "last_processed_bar": evolution.get("last_processed_bar"),
                            "error_count": 0,
                        },
                        "reference": {
                            "role": "reference",
                            "candidate_id": reference.get("candidate_id"),
                            "revision": reference.get("revision"),
                            "bundle": reference.get("bundle"),
                            "bundle_scope": "legacy_experiment",
                            "legacy_shadow_arm": "control",
                            "legacy_shadow_role": "champion",
                            "stream": reference.get("stream"),
                            "last_processed_bar": control.get("last_processed_bar"),
                            "error_count": 0,
                        },
                        "evidence": {"legacy_protocol": experiment.get("protocol")},
                        "promotion": {"status": "not_ready", "proposal_id": None},
                        "legacy_experiment_id": experiment.get("experiment_id"),
                        "migrated_at": current.isoformat(),
                        **revision_stamp(store.job_dir(job_id)),
                    }
                    doc["trials"].append(trial)
                    changed = True
                    # Persist the live trial before retiring the legacy owner.
                    # If the second write fails, the next call finds this trial
                    # and finishes the migration without duplicating it.
                    store.write_json(job_id, PROBATION_PATH, doc)
                    changed = False
                    migrated_trial = trial
                experiment["status"] = "migrated"
                experiment["migrated_at"] = current.isoformat()
                experiment["migrated_to_probation"] = migrated_trial["trial_id"]
                store.write_json(job_id, "state/evolution_experiment.json", experiment)
                store.append_journal(
                    job_id,
                    {
                        "type": "evolution_experiment_migrated_to_probation",
                        "experiment_id": experiment.get("experiment_id"),
                        "trial_id": migrated_trial["trial_id"],
                        "candidate_id": candidate.get("candidate_id"),
                    },
                )
        if changed or not (store.job_dir(job_id) / PROBATION_PATH).exists():
            store.write_json(job_id, PROBATION_PATH, doc)
        return doc


def _resolve_trial_identity(
    store: JobStore,
    job_id: str,
    *,
    candidate_id: str,
    fallback_family: str | None,
    fallback_summary: str | None,
) -> tuple[str, str | None]:
    """The candidate's real strategy identity for the owner-facing trial card.

    Archive entry first (authoritative family + hypothesis summary), then the
    campaign candidate record, then an honest generic placeholder — never the
    literal pipeline name "evolution"."""
    entry = find_candidate(store, job_id, candidate_id) or {}
    archive_family = str(entry.get("family") or "").strip()
    archive_summary = str(entry.get("summary") or "").strip()
    family = next(
        (
            value
            for value in (archive_family, str(fallback_family or "").strip())
            if value.lower() not in PLACEHOLDER_TRIAL_FAMILIES
        ),
        GENERIC_TRIAL_FAMILY,
    )
    summary = (archive_summary or str(fallback_summary or "").strip())[
        :TRIAL_SUMMARY_MAX_CHARS
    ]
    return family, summary or None


def _repair_trial_identity(
    store: JobStore, job_id: str, trial: dict[str, Any]
) -> dict[str, Any] | None:
    """Patch a legacy placeholder identity in place once the archive knows the
    candidate — live trials get fixed on the next adjudication pass, no manual
    migration."""
    if str(trial.get("family") or "").strip().lower() not in (
        PLACEHOLDER_TRIAL_FAMILIES
    ):
        return None
    entry = (
        find_candidate(
            store,
            job_id,
            str(trial.get("candidate_id") or trial.get("candidate_revision") or ""),
        )
        or {}
    )
    family = str(entry.get("family") or "").strip()
    if family.lower() in PLACEHOLDER_TRIAL_FAMILIES:
        return None
    trial["family"] = family
    summary = str(entry.get("summary") or "").strip()[:TRIAL_SUMMARY_MAX_CHARS]
    if summary:
        trial["summary"] = summary
    trial["updated_at"] = utc_now_iso()
    return {
        "action": "probation_trial_identity_repaired",
        "trial_id": trial.get("trial_id"),
        "candidate_id": trial.get("candidate_id"),
        "family": family,
    }


def stage_evolution_probation(
    store: JobStore,
    job_id: str,
    *,
    candidate_id: str,
    candidate_root: Path,
    revision: str,
    source: str,
    family: str,
    summary: str | None = None,
    campaign_id: str | None = None,
    evidence: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Freeze a gate-green candidate into the permanent paper probation rail."""
    current = _aware(now or datetime.now(UTC))
    family, summary = _resolve_trial_identity(
        store,
        job_id,
        candidate_id=candidate_id,
        fallback_family=family,
        fallback_summary=summary,
    )
    spec = ImproverSpec.load(store.job_dir(job_id))
    policy = spec.evolution.get("probation") or {}
    max_active = int(policy.get("max_active") or 3)
    max_queued = int(policy.get("max_queued") or 3)
    min_paired_days = int(policy.get("min_paired_days") or 7)
    max_paired_days = int(policy.get("max_paired_days") or 14)
    safe_revision = _safe_component(revision, "candidate revision")
    trial_id = _safe_trial_id(f"{candidate_id}-{safe_revision}")
    root = store.job_dir(job_id).resolve()
    source_root = candidate_root.resolve()
    if not source_root.is_relative_to(root):
        raise ValueError("probation candidate must be inside its job root")
    if compute_workspace_revision(source_root) != safe_revision:
        raise ValueError("probation candidate revision does not match its bundle")
    with job_state_lock(store.repo_root, job_id, name="probation"):
        doc = load_probation(store, job_id)
        duplicate = next(
            (
                trial
                for trial in doc["trials"]
                if trial.get("candidate_revision") == safe_revision
            ),
            None,
        )
        if duplicate is not None:
            return {
                "status": "duplicate",
                "trial_id": duplicate.get("trial_id"),
                "candidate_id": duplicate.get("candidate_id"),
                "candidate_revision": duplicate.get("candidate_revision"),
                "trial_status": duplicate.get("status"),
            }
        active = [
            trial
            for trial in doc["trials"]
            if trial.get("status") in TRIAL_ACTIVE_STATUSES
        ]
        queued = [trial for trial in doc["trials"] if trial.get("status") == "queued"]
        if len(active) >= max_active and len(queued) >= max_queued:
            return {
                "status": "deferred",
                "reason": (
                    f"probation capacity full ({max_active} active, "
                    f"{max_queued} queued)"
                ),
                "candidate_id": candidate_id,
            }
        bundle_relative = f"{PROBATION_BUNDLE_ROOT}/{trial_id}/candidate"
        reference_relative = f"{PROBATION_BUNDLE_ROOT}/{trial_id}/reference"
        copy_job_bundle(source_root, root / bundle_relative, existing_ok=True)
        copy_job_bundle(root, root / reference_relative, existing_ok=True)
        reference_revision = compute_workspace_revision(root / reference_relative)
        status = "burn_in" if len(active) < max_active else "queued"
        trial = {
            "trial_id": trial_id,
            "candidate_id": candidate_id,
            "candidate_revision": safe_revision,
            "reference_revision": reference_revision,
            "campaign_id": campaign_id,
            "family": family,
            "summary": summary,
            "source": source,
            "status": status,
            "phase": "burn_in" if status == "burn_in" else "queued",
            "queued_at": current.isoformat(),
            "burn_in": {
                "status": "running" if status == "burn_in" else "queued",
                "started_at": current.isoformat() if status == "burn_in" else None,
                "duration_hours": float(policy.get("burn_in_hours") or 24),
                "bar_interval_seconds": _probation_bar_interval_seconds(store, job_id),
                "expires_at": (
                    current
                    + timedelta(hours=float(policy.get("burn_in_hours") or 24) + 12)
                ).isoformat()
                if status == "burn_in"
                else None,
                "coverage": 0.0,
                "first_common_bar": None,
                "last_common_bar": None,
            },
            "forward": {
                "started_at": None,
                "deadline_at": None,
                "min_paired_days": min_paired_days,
                "max_paired_days": max_paired_days,
                "decision_days": sorted({min_paired_days, max_paired_days}),
                "last_decision_day": 0,
                "confidence": float(policy.get("confidence") or 0.90),
                "metrics": None,
            },
            "candidate": {
                "role": "candidate",
                "candidate_id": candidate_id,
                "revision": safe_revision,
                "bundle": bundle_relative,
                "bundle_scope": "probation",
                "stream": f"{PROBATION_FORWARD_ROOT}/{trial_id}/burn_in/candidate",
                "last_processed_bar": current.isoformat(),
                "error_count": 0,
            },
            "reference": {
                "role": "reference",
                "candidate_id": f"incumbent-{reference_revision[:12]}",
                "revision": reference_revision,
                "bundle": reference_relative,
                "bundle_scope": "probation",
                "stream": f"{PROBATION_FORWARD_ROOT}/{trial_id}/burn_in/reference",
                "last_processed_bar": current.isoformat(),
                "error_count": 0,
            },
            "evidence": dict(evidence or {}),
            "promotion": {"status": "not_ready", "proposal_id": None},
            **revision_stamp(root),
        }
        doc["trials"].append(trial)
        store.write_json(job_id, PROBATION_PATH, doc)
        store.append_journal(
            job_id,
            {
                "type": "evolution_probation_queued"
                if status == "queued"
                else "evolution_probation_burn_in_started",
                "trial_id": trial_id,
                "candidate_id": candidate_id,
                "revision": safe_revision,
                "source": source,
            },
        )
        return trial


def active_probation_trials(store: JobStore, job_id: str) -> bool:
    doc = ensure_unified_probation(store, job_id)
    return any(
        trial.get("status") in TRIAL_ACTIVE_STATUSES
        for trial in doc.get("trials") or []
    )


def enqueue_probation_view(
    store: JobStore,
    job_id: str,
    *,
    rows: list[dict[str, Any]],
    now: pd.Timestamp,
) -> bool:
    if not rows or not active_probation_trials(store, job_id):
        return False
    latest = max(pd.Timestamp(row["timestamp"]) for row in rows)
    atomic_write_json(
        store.job_dir(job_id) / PROBATION_VIEW_PATH,
        {
            "schema_version": "1.0",
            "captured_at": now.isoformat(),
            "latest_bar": latest.isoformat(),
            "rows": rows,
        },
    )
    return True


def probation_targets(store: JobStore, job_id: str) -> list[dict[str, Any]]:
    doc = ensure_unified_probation(store, job_id)
    targets: list[dict[str, Any]] = []
    for trial in doc.get("trials") or []:
        if trial.get("status") not in TRIAL_ACTIVE_STATUSES:
            continue
        for key in ("candidate", "reference"):
            target = dict(trial[key])
            target.update({"trial_id": trial["trial_id"], "phase": trial["phase"]})
            targets.append(target)
    return targets


def resolve_probation_bundle(
    store: JobStore,
    job_id: str,
    target: dict[str, Any],
) -> Path:
    relative = str(target.get("bundle") or "")
    if not relative or Path(relative).is_absolute():
        raise ValueError("probation bundle must be a non-empty relative path")
    root = store.job_dir(job_id).resolve()
    resolved = (root / relative).resolve()
    scope = target.get("bundle_scope")
    allowed = (
        (root / "research/evolution/experiment").resolve()
        if scope == "legacy_experiment"
        else (root / PROBATION_BUNDLE_ROOT).resolve()
    )
    if not resolved.is_relative_to(allowed):
        raise ValueError("probation bundle escapes its immutable root")
    return resolved


def update_probation_target(
    store: JobStore,
    job_id: str,
    target: dict[str, Any],
    *,
    bar_iso: str,
    error: bool = False,
) -> None:
    with job_state_lock(store.repo_root, job_id, name="probation"):
        doc = load_probation(store, job_id)
        trial = next(
            (
                item
                for item in doc["trials"]
                if item.get("trial_id") == target.get("trial_id")
            ),
            None,
        )
        if trial is None or trial.get("phase") != target.get("phase"):
            return
        key = str(target.get("role"))
        selected = trial.get(key) or {}
        if selected.get("revision") != target.get("revision"):
            return
        selected["last_processed_bar"] = bar_iso
        if error:
            selected["error_count"] = int(selected.get("error_count") or 0) + 1
        store.write_json(job_id, PROBATION_PATH, doc)


def maybe_adjudicate_probation(
    store: JobStore,
    job_id: str,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Advance burn-in/forward trials and retry owner-proposal staging."""
    current = _aware(now or datetime.now(UTC))
    outcomes: list[dict[str, Any]] = []
    promotions: list[str] = []
    spec = ImproverSpec.load(store.job_dir(job_id))
    policy = spec.evolution.get("probation") or {}
    max_active = int(policy.get("max_active") or 3)
    with job_state_lock(store.repo_root, job_id, name="probation"):
        doc = load_probation(store, job_id)
        for trial in doc.get("trials") or []:
            repair = _repair_trial_identity(store, job_id, trial)
            if repair:
                outcomes.append(repair)
            if trial.get("status") == "burn_in":
                outcome = _adjudicate_burn_in(store, job_id, trial, current=current)
            elif trial.get("status") == "active":
                # Display artifact — a curve bug must never stall the
                # graduation/kill adjudication it rides along with.
                try:
                    _update_trial_equity_curve(store, job_id, trial, current=current)
                except Exception:  # noqa: BLE001
                    pass
                outcome = _adjudicate_forward(store, job_id, trial, current=current)
            else:
                outcome = None
            if outcome:
                outcomes.append(outcome)
            promotion = trial.get("promotion") or {}
            promotion_status = promotion.get("status")
            stale_staging = promotion_status == "staging" and _stale_promotion_staging(
                promotion, current=current
            )
            if trial.get("status") == "graduated" and (
                promotion_status in {"pending", "retry"} or stale_staging
            ):
                promotions.append(str(trial["trial_id"]))
        outcomes.extend(
            _activate_queued_trials(
                doc,
                current=current,
                max_active=max_active,
            )
        )
        store.write_json(job_id, PROBATION_PATH, doc)
    for outcome in outcomes:
        _sync_trial_archive(store, job_id, outcome)
        store.append_journal(
            job_id,
            {
                "type": str(outcome.get("action") or "probation_updated"),
                **outcome,
            },
        )
    for trial_id in promotions[:1]:
        promotion = _stage_trial_promotion(store, job_id, trial_id)
        outcomes.append(promotion)
    return outcomes


def _adjudicate_burn_in(
    store: JobStore,
    job_id: str,
    trial: dict[str, Any],
    *,
    current: datetime,
) -> dict[str, Any] | None:
    common = _common_ticks(store, job_id, trial)
    burn = trial["burn_in"]
    if common:
        burn["first_common_bar"] = common[0].isoformat()
        burn["last_common_bar"] = common[-1].isoformat()
    span = common[-1] - common[0] if len(common) >= 2 else pd.Timedelta(0)
    interval_s = max(int(burn.get("bar_interval_seconds") or 300), 1)
    expected = int(span.total_seconds() // interval_s) + 1
    coverage = len(common) / expected if expected else 0.0
    burn["coverage"] = round(coverage, 4)
    errors = int(trial["candidate"].get("error_count") or 0)
    safety = _stream_hard_constraint_metrics(
        store,
        job_id,
        stream=str(trial["candidate"]["stream"]),
        started=_parse(burn["started_at"]),
        current=current,
    )
    burn.update(safety)
    hard_breach = bool(safety["hard_constraint_breach"])
    expired = current >= _parse(burn["expires_at"])
    mature = (
        span >= pd.Timedelta(hours=float(burn["duration_hours"])) and coverage >= 0.95
    )
    if not mature and not expired and not errors and not hard_breach:
        return None
    if errors or hard_breach or not mature:
        if errors:
            reason = "candidate execution error"
        elif hard_breach:
            reason = "hard safety breach"
        else:
            reason = "burn-in coverage expired"
        _close_trial(trial, "killed", reason=reason, current=current)
        return {
            "action": "probation_killed",
            "trial_id": trial["trial_id"],
            "candidate_id": trial.get("candidate_id"),
            "reason": reason,
        }
    burn.update({"status": "passed", "completed_at": common[-1].isoformat()})
    started = common[-1].to_pydatetime()
    trial["status"] = "active"
    trial["phase"] = "forward"
    trial["forward"]["started_at"] = started.isoformat()
    trial["forward"]["deadline_at"] = (
        started + timedelta(days=int(trial["forward"]["max_paired_days"]))
    ).isoformat()
    for key in ("candidate", "reference"):
        trial[key]["stream"] = (
            f"{PROBATION_FORWARD_ROOT}/{trial['trial_id']}/forward/{key}"
        )
        trial[key]["last_processed_bar"] = common[-1].isoformat()
        trial[key]["error_count"] = 0
    trial["updated_at"] = current.isoformat()
    return {"action": "probation_forward_started", "trial_id": trial["trial_id"]}


def _adjudicate_forward(
    store: JobStore,
    job_id: str,
    trial: dict[str, Any],
    *,
    current: datetime,
) -> dict[str, Any] | None:
    metrics = _paired_forward_metrics(store, job_id, trial, current=current)
    trial["forward"]["metrics"] = metrics
    trial["updated_at"] = current.isoformat()
    paired_days = int(metrics["paired_days"])
    min_days = int(trial["forward"]["min_paired_days"])
    max_days = int(trial["forward"]["max_paired_days"])
    last_decision = int(trial["forward"].get("last_decision_day") or 0)
    checkpoint = None
    if paired_days >= max_days and last_decision < max_days:
        checkpoint = max_days
    elif paired_days >= min_days and last_decision < min_days:
        checkpoint = min_days
    if int(trial["candidate"].get("error_count") or 0) > 0:
        _close_trial(
            trial, "killed", reason="candidate execution error", current=current
        )
    elif metrics["hard_constraint_breach"]:
        _close_trial(trial, "killed", reason="hard safety breach", current=current)
    elif checkpoint is not None and metrics["lcb"] is not None and metrics["lcb"] > 0:
        _close_trial(
            trial, "graduated", reason="paired utility LCB > 0", current=current
        )
        trial["promotion"] = {"status": "pending", "proposal_id": None}
    elif checkpoint is not None and metrics["ucb"] is not None and metrics["ucb"] < 0:
        _close_trial(trial, "killed", reason="paired utility UCB < 0", current=current)
    elif checkpoint == max_days:
        _close_trial(
            trial,
            "inconclusive",
            reason="14-day endpoint inconclusive",
            current=current,
        )
    elif checkpoint is not None:
        trial["forward"]["last_decision_day"] = checkpoint
        return {
            "action": "probation_checkpoint_inconclusive",
            "trial_id": trial["trial_id"],
            "candidate_id": trial.get("candidate_id"),
            "checkpoint_day": checkpoint,
            "metrics": metrics,
        }
    elif current >= _parse(trial["forward"]["deadline_at"]):
        _close_trial(
            trial,
            "inconclusive",
            reason="14-day endpoint inconclusive",
            current=current,
        )
    else:
        return None
    return {
        "action": f"probation_{trial['status']}",
        "trial_id": trial["trial_id"],
        "candidate_id": trial.get("candidate_id"),
        "reason": trial.get("verdict_reason"),
        "metrics": metrics,
    }


def _paired_forward_metrics(
    store: JobStore,
    job_id: str,
    trial: dict[str, Any],
    *,
    current: datetime,
) -> dict[str, Any]:
    from wayfinder_paths.jobs.paper_experiment import (
        _daily_pnl_for_stream,
    )

    started = _parse(trial["forward"]["started_at"])
    candidate = _daily_pnl_for_stream(
        store.job_dir(job_id) / trial["candidate"]["stream"],
        since=started,
        until=current,
    )
    reference = _daily_pnl_for_stream(
        store.job_dir(job_id) / trial["reference"]["stream"],
        since=started,
        until=current,
    )
    complete_days = sorted(
        day
        for day in set(candidate) & set(reference)
        if day < current.date().isoformat()
    )
    capital = 10_000.0
    deltas = [
        math.log1p(candidate[day] / capital) - math.log1p(reference[day] / capital)
        for day in complete_days
        if candidate[day] > -capital and reference[day] > -capital
    ]
    confidence = float(trial["forward"].get("confidence") or 0.90)
    lcb: float | None
    ucb: float | None
    if len(deltas) < FORWARD_BOOTSTRAP_BLOCK_LEN:
        # Degenerate bootstrap: fewer paired days than one block makes every
        # resample identical, rendering lcb == ucb == estimate as false
        # precision. Decision checkpoints are unaffected — the day-7/14
        # peeks have >= block-length days, and verdicts null-guard the bounds.
        lcb = None
        ucb = None
    else:
        lcb = block_bootstrap_lcb(
            deltas,
            block_len=FORWARD_BOOTSTRAP_BLOCK_LEN,
            iterations=500,
            confidence=confidence,
        )
        reverse = block_bootstrap_lcb(
            [-value for value in deltas],
            block_len=FORWARD_BOOTSTRAP_BLOCK_LEN,
            iterations=500,
            confidence=confidence,
        )
        ucb = -reverse if reverse is not None else None
    safety = _hard_constraint_metrics(store, job_id, candidate)
    return {
        "paired_days": len(deltas),
        "estimate": round(sum(deltas), 8),
        "lcb": lcb,
        "ucb": ucb,
        "confidence": confidence,
        "candidate_net_pnl": round(sum(candidate.values()), 6),
        "reference_net_pnl": round(sum(reference.values()), 6),
        **safety,
        "updated_at": current.isoformat(),
    }


def _update_trial_equity_curve(
    store: JobStore,
    job_id: str,
    trial: dict[str, Any],
    *,
    current: datetime,
) -> None:
    """Append newly completed hourly buckets to the trial's paired cumulative
    equity curve (candidate vs frozen incumbent reference, zeroed at forward
    admission), stored as a per-trial sidecar next to the streams.

    Basis is REALIZED net PnL from each stream's trades.jsonl — the exact
    series the forward adjudicator sums — attributed to the trade's close bar
    time, so the curve endpoint matches the candidate/reference net PnL on
    the trial card. Tick rows carry no mark price, so an unrealized basis
    would need market fetches. Incremental by construction: a bucket only
    finalizes once BOTH stream cursors have processed past its end, emitted
    points never change, and each pass reads only bytes past the stored
    per-stream offsets.
    """
    started_at = (trial.get("forward") or {}).get("started_at")
    if not started_at:
        return
    started = int(_parse(started_at).timestamp())
    root = store.job_dir(job_id)
    streams: dict[str, Path] = {}
    watermark: float | None = None
    for role in ("candidate", "reference"):
        target = trial.get(role) or {}
        stream = str(target.get("stream") or "")
        last_bar = target.get("last_processed_bar")
        if not stream or not last_bar:
            return
        path = root / stream
        if not path.is_dir():
            return
        streams[role] = path
        bar_epoch = _parse(str(last_bar)).timestamp()
        watermark = bar_epoch if watermark is None else min(watermark, bar_epoch)
    assert watermark is not None
    relative = _equity_curve_relative(str(trial["trial_id"]))
    doc = store.read_json(job_id, relative, default=None)
    if not isinstance(doc, dict) or doc.get("basis") != EQUITY_CURVE_BASIS:
        doc = {
            "basis": EQUITY_CURVE_BASIS,
            "started_at": started_at,
            "updated_at": None,
            "points": [[started, 0.0, 0.0]],
            "cursor": {
                "candidate": {"offset": 0, "cum": 0.0},
                "reference": {"offset": 0, "cum": 0.0},
            },
        }
    points: list[list[float]] = [list(point) for point in doc["points"]]
    if len(points) >= EQUITY_CURVE_MAX_POINTS:
        return
    # Largest emittable bucket end: hour-aligned to admission, bounded by the
    # slower stream's cursor AND the point cap (so consumed bytes always land
    # in an emitted bucket — nothing is read and then dropped).
    complete_hours = int(watermark - started) // EQUITY_CURVE_BUCKET_SECONDS
    cap_hours = (int(points[-1][0]) - started) // EQUITY_CURVE_BUCKET_SECONDS + (
        EQUITY_CURVE_MAX_POINTS - len(points)
    )
    boundary = started + min(complete_hours, cap_hours) * EQUITY_CURVE_BUCKET_SECONDS
    cursor = doc["cursor"]
    changed = False
    pending: dict[str, list[tuple[float, float]]] = {}
    for role, path in streams.items():
        rows, consumed = _consume_stream_trades(
            path, offset=int(cursor[role]["offset"]), boundary=float(boundary)
        )
        pending[role] = rows
        if consumed != int(cursor[role]["offset"]):
            cursor[role]["offset"] = consumed
            changed = True
    next_end = int(points[-1][0]) + EQUITY_CURVE_BUCKET_SECONDS
    while next_end <= boundary and len(points) < EQUITY_CURVE_MAX_POINTS:
        values: list[float] = []
        for role in ("candidate", "reference"):
            cum = float(cursor[role]["cum"]) + sum(
                pnl for close, pnl in pending[role] if close < next_end
            )
            pending[role] = [
                (close, pnl) for close, pnl in pending[role] if close >= next_end
            ]
            cursor[role]["cum"] = cum
            values.append(round(cum, 4))
        points.append([next_end, values[0], values[1]])
        changed = True
        next_end += EQUITY_CURVE_BUCKET_SECONDS
    if not changed and doc.get("updated_at") is not None:
        return
    doc["points"] = points
    doc["updated_at"] = current.isoformat()
    atomic_write_json(root / relative, doc)


def _consume_stream_trades(
    stream: Path, *, offset: int, boundary: float
) -> tuple[list[tuple[float, float]], int]:
    """Parse complete trades.jsonl lines from `offset` as (close_epoch, pnl),
    stopping BEFORE the first close at/after `boundary` — its bucket is not
    final yet, so those bytes are re-read once the watermark passes. Rows are
    appended in bar order, which makes the early stop safe."""
    path = stream / "trades.jsonl"
    if not path.exists() or offset >= path.stat().st_size:
        return [], offset
    with path.open("rb") as handle:
        handle.seek(offset)
        blob = handle.read()
    rows: list[tuple[float, float]] = []
    consumed = offset
    for line in blob.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break  # partially flushed tail — retry next pass
        try:
            row = json.loads(line.decode("utf-8", errors="replace"))
            stamp = pd.Timestamp(
                row.get("closed_at") or row.get("timestamp") or row.get("ts")
            )
            if pd.isna(stamp):
                raise ValueError(line.decode("utf-8", errors="replace"))
            close_epoch = stamp.timestamp()
        except (TypeError, ValueError):
            consumed += len(line)
            continue
        if close_epoch >= boundary:
            break
        try:
            pnl = float(row.get("net_pnl") or row.get("realized_pnl_delta") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        rows.append((close_epoch, pnl))
        consumed += len(line)
    return rows, consumed


def _stream_hard_constraint_metrics(
    store: JobStore,
    job_id: str,
    *,
    stream: str,
    started: datetime,
    current: datetime,
) -> dict[str, Any]:
    from wayfinder_paths.jobs.paper_experiment import _daily_pnl_for_stream

    daily = _daily_pnl_for_stream(
        store.job_dir(job_id) / stream,
        since=started,
        until=current,
    )
    return _hard_constraint_metrics(store, job_id, daily)


def _hard_constraint_metrics(
    store: JobStore, job_id: str, daily: dict[str, float]
) -> dict[str, Any]:
    from wayfinder_paths.jobs.constitution import load_constitution
    from wayfinder_paths.jobs.paper_experiment import _max_drawdown

    drawdown = _max_drawdown(daily, 10_000.0)
    max_drawdown = float(
        load_constitution(store.job_dir(job_id))["hard_constraints"]["max_drawdown_pct"]
    )
    return {
        "candidate_max_drawdown_pct": round(drawdown, 8),
        "hard_constraint_breach": drawdown > max_drawdown,
    }


def _stage_trial_promotion(
    store: JobStore, job_id: str, trial_id: str
) -> dict[str, Any]:
    with job_state_lock(store.repo_root, job_id, name="probation"):
        doc = load_probation(store, job_id)
        trial = next(item for item in doc["trials"] if item["trial_id"] == trial_id)
        promotion = trial.setdefault("promotion", {})
        if promotion.get("proposal_id"):
            return {"action": "probation_promotion_exists", "trial_id": trial_id}
        promotion.update({"status": "staging", "updated_at": utc_now_iso()})
        store.write_json(job_id, PROBATION_PATH, doc)
        candidate_root = resolve_probation_bundle(store, job_id, trial["candidate"])
    try:
        from wayfinder_paths.jobs.proposals import propose_change

        proposal_id = f"prop-probation-{trial_id[:36]}"
        existing = next(
            (
                item
                for item in store.proposals(job_id)
                if item.get("proposal_id") == proposal_id
            ),
            None,
        )
        if existing is not None:
            return _record_existing_promotion(
                store, job_id, trial_id, proposal=existing
            )
        proposal = propose_change(
            store,
            job_id,
            kind="code_change",
            summary=f"Promote probation graduate {trial.get('candidate_id')}",
            intent_contract={
                "goal": "promote the mechanically graduated probation candidate",
                "invariants": [
                    "preserve execution safety constraints",
                    "preserve paper/live operator-owned settings",
                ],
            },
            candidate_source=candidate_root,
            proposal_id=proposal_id,
            memo=(
                "This candidate cleared the historical economic gate, a 24-hour "
                "operational burn-in, and the paired forward probation rule. "
                "Owner approval is still required before application."
            ),
            allow_auto_apply=False,
        )
    except Exception as exc:  # retry infrastructure; evidence failures supersede
        from wayfinder_paths.jobs.failures import classify_failure

        status = (
            "retry" if classify_failure(str(exc)) == "infrastructure" else "superseded"
        )
        with job_state_lock(store.repo_root, job_id, name="probation"):
            doc = load_probation(store, job_id)
            trial = next(item for item in doc["trials"] if item["trial_id"] == trial_id)
            trial["promotion"] = {
                "status": status,
                "proposal_id": None,
                "last_error": str(exc)[:300],
                "updated_at": utc_now_iso(),
            }
            store.write_json(job_id, PROBATION_PATH, doc)
        return {"action": f"probation_promotion_{status}", "trial_id": trial_id}
    with job_state_lock(store.repo_root, job_id, name="probation"):
        doc = load_probation(store, job_id)
        trial = next(item for item in doc["trials"] if item["trial_id"] == trial_id)
        trial["promotion"] = {
            "status": "owner_review",
            "proposal_id": proposal["proposal_id"],
            "created_at": utc_now_iso(),
        }
        store.write_json(job_id, PROBATION_PATH, doc)
    store.append_journal(
        job_id,
        {
            "type": "probation_promotion_proposed",
            "trial_id": trial_id,
            "proposal_id": proposal["proposal_id"],
        },
    )
    return {
        "action": "probation_promotion_proposed",
        "trial_id": trial_id,
        "proposal_id": proposal["proposal_id"],
    }


def _activate_queued_trials(
    doc: dict[str, Any],
    *,
    current: datetime,
    max_active: int,
) -> list[dict[str, Any]]:
    active = [
        trial for trial in doc["trials"] if trial.get("status") in TRIAL_ACTIVE_STATUSES
    ]
    activated: list[dict[str, Any]] = []
    for trial in sorted(
        doc["trials"], key=lambda item: str(item.get("queued_at") or "")
    ):
        if len(active) >= max_active or trial.get("status") != "queued":
            continue
        trial["status"] = "burn_in"
        trial["phase"] = "burn_in"
        trial["burn_in"].update(
            {
                "status": "running",
                "started_at": current.isoformat(),
                "expires_at": (
                    current
                    + timedelta(hours=float(trial["burn_in"]["duration_hours"]) + 12)
                ).isoformat(),
            }
        )
        for key in ("candidate", "reference"):
            trial[key]["last_processed_bar"] = current.isoformat()
        active.append(trial)
        activated.append(
            {
                "action": "evolution_probation_burn_in_started",
                "trial_id": trial["trial_id"],
                "candidate_id": trial.get("candidate_id"),
                "source": "probation_queue",
            }
        )
    return activated


def _sync_trial_archive(store: JobStore, job_id: str, outcome: dict[str, Any]) -> None:
    status_by_action = {
        "probation_graduated": "paper_experiment",
        "probation_killed": "refuted",
        "probation_inconclusive": "archived",
    }
    status = status_by_action.get(str(outcome.get("action") or ""))
    candidate_id = str(outcome.get("candidate_id") or "")
    if not status or not candidate_id:
        return
    from wayfinder_paths.jobs.archive import set_candidate_status

    try:
        set_candidate_status(
            store,
            job_id,
            candidate_id,
            status,
            evidence=str(outcome.get("reason") or "forward probation verdict")[:300],
        )
    except ValueError:
        # Legacy experiments can predate the archive. Their immutable trial
        # remains the authoritative receipt and must still finish.
        return


def _stale_promotion_staging(promotion: dict[str, Any], *, current: datetime) -> bool:
    try:
        updated = _parse(promotion.get("updated_at"))
    except (TypeError, ValueError):
        return True
    return current - updated >= timedelta(minutes=30)


def _record_existing_promotion(
    store: JobStore,
    job_id: str,
    trial_id: str,
    *,
    proposal: dict[str, Any],
) -> dict[str, Any]:
    proposal_id = str(proposal["proposal_id"])
    with job_state_lock(store.repo_root, job_id, name="probation"):
        doc = load_probation(store, job_id)
        trial = next(item for item in doc["trials"] if item["trial_id"] == trial_id)
        trial["promotion"] = {
            "status": "owner_review",
            "proposal_id": proposal_id,
            "created_at": proposal.get("created_at") or utc_now_iso(),
        }
        store.write_json(job_id, PROBATION_PATH, doc)
    return {
        "action": "probation_promotion_proposed",
        "trial_id": trial_id,
        "proposal_id": proposal_id,
        "recovered": True,
    }


def _common_ticks(
    store: JobStore, job_id: str, trial: dict[str, Any]
) -> list[pd.Timestamp]:
    sets: list[set[pd.Timestamp]] = []
    for key in ("candidate", "reference"):
        path = store.job_dir(job_id) / trial[key]["stream"] / "ticks.jsonl"
        stamps: set[pd.Timestamp] = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                    stamp = pd.Timestamp(row.get("bar_ts") or row.get("ts"))
                    if not pd.isna(stamp):
                        stamps.add(stamp)
                except (TypeError, ValueError):
                    continue
        sets.append(stamps)
    return sorted(sets[0] & sets[1])


def _close_trial(
    trial: dict[str, Any], status: str, *, reason: str, current: datetime
) -> None:
    trial["status"] = status
    trial["phase"] = "complete"
    trial["closed_at"] = current.isoformat()
    trial["verdict_reason"] = reason
    trial["updated_at"] = current.isoformat()


def _safe_trial_id(value: str) -> str:
    raw = str(value or "").strip()
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in raw)
    if not safe:
        raise ValueError("probation trial id is required")
    return f"{safe[:48]}-{hashlib.sha256(raw.encode()).hexdigest()[:8]}"


def _safe_component(value: str, label: str) -> str:
    raw = str(value or "").strip()
    if not raw or Path(raw).name != raw or raw in {".", ".."}:
        raise ValueError(f"invalid {label}")
    return raw


def _probation_bar_interval_seconds(store: JobStore, job_id: str) -> int:
    job = store.load(job_id)
    data_contract = (job.execution_spec or {}).get("data_contract") or {}
    declared = bar_interval_seconds(data_contract.get("bar_interval"))
    return max(int(declared or job.script_loop.interval_seconds or 300), 1)


def _parse(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _aware(parsed)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
