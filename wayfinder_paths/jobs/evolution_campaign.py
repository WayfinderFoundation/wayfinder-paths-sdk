"""Isolated, budgeted open-ended strategy evolution campaigns.

The model remains the code mutation operator; this module owns everything
that must not be left to model discretion: cadence, immutable context,
lineage, stage budgets, causal paper proposals, archive accounting, and the
paper-only terminal state.  Candidate bundles never replace the active
workspace and no function in this module can authorize live trading.
"""

from __future__ import annotations

import gc
import hashlib
import importlib
import json
import math
import os
import shutil
import uuid
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

import pandas as pd
import yaml

from wayfinder_paths.jobs.archive import (
    ARCHIVE_STATUSES,
    elite_activity_eligible,
    evolution_lessons_block,
    load_archive,
    participation_score,
    quality_diversity_snapshot,
    record_candidate,
    set_candidate_status,
)
from wayfinder_paths.jobs.bundles import copy_job_bundle
from wayfinder_paths.jobs.compute_lock import (
    ComputeLockBusy,
    experiment_compute_lock,
    job_state_lock,
    machine_state_lock,
)
from wayfinder_paths.jobs.evidence import verify_job_evidence_refs
from wayfinder_paths.jobs.evolution_diagnostics import (
    attempt_made_progress,
    build_diagnostic_pack,
    build_postmortem,
    compact_postmortem,
    resolve_json_pointer,
    result_receipt,
    valid_evidence_pointers,
)
from wayfinder_paths.jobs.evolution_funnel import summarize_evolution_funnel
from wayfinder_paths.jobs.execution.features import parse_feature_specs
from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
from wayfinder_paths.jobs.execution.optimize import (
    is_search_space,
    run_optuna_search,
    search_space_probe_variants,
)
from wayfinder_paths.jobs.execution.primitives import (
    DEFAULT_WARMUP_BARS,
    ExecutionSpec,
    bar_interval_seconds,
    resolve_compute_window,
)
from wayfinder_paths.jobs.execution.simulator import (
    ExecutionGridResult,
    PreparedExecutionDataset,
    _load_strategy,
    simulate_execution,
)
from wayfinder_paths.jobs.execution.validation import (
    BOUNDED_WINDOW_HINT,
    parameter_behavior_probe,
    resolve_execution_spec,
    validate_execution_job,
    window_invariance_probe,
)
from wayfinder_paths.jobs.execution.walk_forward import _slice, _test_window_stats
from wayfinder_paths.jobs.failures import TransientInfrastructureError, classify_failure
from wayfinder_paths.jobs.forward_experience import (
    CALIBRATION_PATH,
    build_forward_experience,
    execution_cost_assumptions,
)
from wayfinder_paths.jobs.gating import (
    compute_workspace_revision,
    evaluate_economic_gate,
)
from wayfinder_paths.jobs.improver.spec import ImproverSpec, revision_stamp
from wayfinder_paths.jobs.isolated_phase import run_isolated_phase
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.resource_envelope import (
    evolution_resource_phase,
    require_evolution_headroom,
    require_evolution_launch_headroom,
)
from wayfinder_paths.jobs.robustness import _strategy_warmup_bars
from wayfinder_paths.jobs.starter_casebook import select_starter_cases
from wayfinder_paths.jobs.starters import (
    STARTER_DEFINITIONS,
    StarterDefinition,
    starter_lookback_bars,
    starter_warmup_bars,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.monitor_state import atomic_write_json, atomic_write_text

CAMPAIGN_STATE_PATH = "state/evolution_campaign.json"
CAMPAIGN_ROOT = "research/evolution/campaigns"
PARENT_BUNDLE_ROOT = "research/evolution/parents"
RESEARCH_SEED_ROOT = "research/evolution/research_seeds"
RESEARCH_SEED_STATE_PATH = "state/evolution_research_seeds.json"
CAMPAIGN_DATA_ROOT = "dataset"
FORWARD_SNAPSHOT = "forward_experience.json"
DIAGNOSTIC_PACK = "diagnostic_pack.json"
CAMPAIGN_DESIGN = "campaign_design.json"
SCHEMA_VERSION = "2.0"
_PARENT_SOURCES = ("incumbent", "qd_elite", "crossover", "de_novo")
_EXECUTABLE_PARENT_STATUSES = {
    "dev_frontier",
    "paper_proposal",
    "paper_experiment",
    "incumbent",
    "frontier",
    "probation",
}
_TARGET_EXECUTION_PARAM_KEYS = {
    "fee_bps",
    "initial_capital",
    "leverage",
    "min_trade_notional",
    "slippage_bps",
    "stop_market_slippage_bps",
    "symbols",
    "venue",
    "wallet_label",
}
CAMPAIGN_DRAIN = timedelta(minutes=30)
_MAX_SEARCH_DIMENSIONS = 3
_OPTUNA_SEED = 42
_PARAMETER_SEARCH_GUIDANCE = (
    "create search_space.json with at most three bounded typed Optuna dimensions, "
    "for example "
    '{"lookback":{"type":"int","low":12,"high":96}}; do not replace the '
    "search with one hand-picked value."
)
_STRUCTURAL_SEARCH_GUIDANCE = (
    "Make the named causal code change. If it introduces meaningful numeric "
    "behavior knobs, also create search_space.json with at most three bounded "
    "typed dimensions covering only those new knobs. Otherwise omit it; do not "
    "invent tuning axes for a boolean or parameterless change."
)


def campaign_status(store: JobStore, job_id: str) -> dict[str, Any]:
    return store.read_json(job_id, CAMPAIGN_STATE_PATH, default={}) or {}


def _fleet_campaign_turn(
    store: JobStore,
    job_id: str,
    *,
    now: datetime,
) -> bool:
    """One campaign per box; oldest due eligible job wins deterministically."""
    jobs = sorted(store.list_jobs(), key=lambda item: item.id)
    for job in jobs:
        state = campaign_status(store, job.id)
        if state.get("status") in {"active", "finalizing"}:
            return job.id == job_id
    due: list[tuple[datetime, str]] = []
    for job in jobs:
        try:
            spec = ImproverSpec.load(store.job_dir(job.id))
            if not spec.evolution_eligibility(store.job_dir(job.id), job.id)[
                "eligible"
            ]:
                continue
            state = campaign_status(store, job.id)
            anchor = state.get("started_at")
            anchor_time = _parse(anchor) if anchor else datetime.min.replace(tzinfo=UTC)
            cadence = timedelta(
                hours=float(
                    spec.evolution.get("start_interval_hours")
                    or spec.evolution.get("cooldown_hours")
                    or 24
                )
            )
            if anchor and now - anchor_time < cadence:
                continue
            if not evolution_compute_window_open(
                store, job.id, now=now, reserve_campaign=True
            ):
                continue
            due.append((anchor_time, job.id))
        except (OSError, TypeError, ValueError):
            continue
    due.sort(key=lambda item: (item[0], item[1]))
    return bool(due and due[0][1] == job_id)


def evolution_compute_window_open(
    store: JobStore,
    job_id: str,
    *,
    now: datetime | None = None,
    reserve_campaign: bool = False,
) -> bool:
    """Return whether evolution model work fits outside priced peak windows."""
    spec = ImproverSpec.load(store.job_dir(job_id))
    schedule = spec.evolution.get("pricing_schedule") or {}
    windows = schedule.get("blocked_windows_utc") or []
    if not windows:
        return True
    current = _campaign_now(now)
    duration = timedelta(microseconds=1)
    if reserve_campaign:
        duration = timedelta(
            hours=float(spec.evolution["campaign_hours"]),
            minutes=float(schedule.get("campaign_guard_minutes") or 0),
        )
    return not _overlaps_daily_utc_windows(current, current + duration, windows)


def _overlaps_daily_utc_windows(
    start: datetime, end: datetime, windows: list[Any]
) -> bool:
    """Check a UTC interval against recurring half-open daily windows."""
    first_day = start.date() - timedelta(days=1)
    last_day = end.date()
    day = first_day
    while day <= last_day:
        for raw_window in windows:
            if not isinstance(raw_window, (list, tuple)) or len(raw_window) != 2:
                raise ValueError("pricing windows must be [start, end] UTC pairs")
            blocked_start = datetime.combine(
                day, _parse_utc_clock(raw_window[0]), tzinfo=UTC
            )
            blocked_end = datetime.combine(
                day, _parse_utc_clock(raw_window[1]), tzinfo=UTC
            )
            if blocked_end <= blocked_start:
                blocked_end += timedelta(days=1)
            if start < blocked_end and end > blocked_start:
                return True
        day += timedelta(days=1)
    return False


def _parse_utc_clock(value: Any) -> time:
    try:
        return time.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"invalid UTC pricing-window time: {value!r}") from exc


def campaign_due(store: JobStore, job_id: str, *, now: datetime | None = None) -> bool:
    """Cheap, read-only cadence check used before spawning campaign setup."""
    spec = ImproverSpec.load(store.job_dir(job_id))
    if not spec.evolution_eligibility(store.job_dir(job_id), job_id)["eligible"]:
        return False
    existing = campaign_status(store, job_id)
    if existing.get("status") in {"active", "finalizing"}:
        return False
    current = _campaign_now(now)
    if not evolution_compute_window_open(
        store, job_id, now=current, reserve_campaign=True
    ):
        return False
    if not _fleet_campaign_turn(store, job_id, now=current):
        return False
    anchor = existing.get("started_at")
    if not anchor:
        return True
    cadence = timedelta(
        hours=float(
            spec.evolution.get("start_interval_hours")
            or spec.evolution["cooldown_hours"]
        )
    )
    return current - _parse(anchor) >= cadence


def maybe_start_campaign(
    store: JobStore, job_id: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Start the due rollout campaign; return None outside its feature gate."""
    with machine_state_lock(store.repo_root, name="evolution_campaign_slot"):
        with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
            spec = ImproverSpec.load(store.job_dir(job_id))
            if not spec.evolution_eligibility(store.job_dir(job_id), job_id)[
                "eligible"
            ]:
                return None
            existing = campaign_status(store, job_id)
            if existing.get("status") in {"active", "finalizing"}:
                return existing
            cadence_anchor = existing.get("started_at")
            if cadence_anchor:
                elapsed = _campaign_now(now) - _parse(cadence_anchor)
                cadence = timedelta(
                    hours=float(
                        spec.evolution.get("start_interval_hours")
                        or spec.evolution["cooldown_hours"]
                    )
                )
                if elapsed < cadence:
                    return existing
            if not evolution_compute_window_open(
                store, job_id, now=now, reserve_campaign=True
            ):
                return existing or None
            return _start_campaign(store, job_id, now=now)


def start_campaign(
    store: JobStore,
    job_id: str,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    with machine_state_lock(store.repo_root, name="evolution_campaign_slot"):
        with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
            return _start_campaign(store, job_id, now=now, force=force)


def _start_campaign(
    store: JobStore,
    job_id: str,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    spec = ImproverSpec.load(store.job_dir(job_id))
    eligibility = spec.evolution_eligibility(store.job_dir(job_id), job_id)
    if not eligibility["eligible"]:
        raise ValueError(
            f"open evolution is not eligible for job {job_id!r}: "
            + ", ".join(eligibility["reasons"])
        )
    existing = campaign_status(store, job_id)
    current = _campaign_now(now)
    if existing.get("status") in {"active", "finalizing"} and not force:
        return existing
    cadence_anchor = existing.get("started_at")
    if cadence_anchor and not force:
        elapsed = current - _parse(cadence_anchor)
        cadence = timedelta(
            hours=float(
                spec.evolution.get("start_interval_hours")
                or spec.evolution["cooldown_hours"]
            )
        )
        if elapsed < cadence:
            raise ValueError("evolution campaign start interval has not elapsed")
    if not force and not evolution_compute_window_open(
        store, job_id, now=current, reserve_campaign=True
    ):
        raise ValueError("evolution campaign does not fit outside peak pricing")
    if not _fleet_campaign_turn(store, job_id, now=current):
        raise TransientInfrastructureError(
            "another eligible job owns the machine evolution campaign slot"
        )
    require_evolution_launch_headroom()
    from wayfinder_paths.jobs.worker import job_worker_session_busy

    if job_worker_session_busy(job_id, "intervene"):
        raise TransientInfrastructureError(
            "evolution campaign deferred while the intervention worker is active"
        )

    root = store.job_dir(job_id)
    dataset_path = root / "results" / "backtest" / "input_bars.json"
    if not dataset_path.exists():
        raise FileNotFoundError("evolution needs the job's canonical backtest dataset")
    source_revision = compute_workspace_revision(root)
    from wayfinder_paths.jobs.probation import ensure_unified_probation

    ensure_unified_probation(store, job_id, now=current)
    campaign_stem = f"{current.strftime('%Y%m%dT%H%M%SZ')}-{source_revision[:8]}"
    campaign_id = campaign_stem
    suffix = 2
    while (root / CAMPAIGN_ROOT / campaign_id).exists():
        campaign_id = f"{campaign_stem}-{suffix}"
        suffix += 1
    deadline = current + timedelta(hours=float(spec.evolution["campaign_hours"]))
    with experiment_compute_lock(store, job_id, label=f"evolution-start:{job_id}"):
        experience = build_forward_experience(store, job_id, now=current)
    if existing and existing.get("status") not in {"active", "finalizing"}:
        _sync_campaign_archive(store, job_id, existing)
    historical_lessons = evolution_lessons_block(store, job_id)
    cases = select_starter_cases(_job_tags(store, job_id))
    relative_root = f"{CAMPAIGN_ROOT}/{campaign_id}"
    campaign_root = root / relative_root
    with experiment_compute_lock(store, job_id, label=f"evolution-snapshot:{job_id}"):
        snapshots = _snapshot_campaign_inputs(
            root,
            campaign_root,
            dataset_path=dataset_path,
            experience=experience,
            development_fraction=1.0,
        )
    # Seed the frozen source's compute window BEFORE any candidate copies it:
    # candidates inherit the declaration without their bundle diverging from
    # the baseline, so the identical-to-source guard still catches unedited
    # candidates. Behavior-neutral — the seeded value is exactly what
    # resolve_compute_window already produced for the source.
    _seed_bundle_window(store, job_id, campaign_root / "source")
    snapshots["source_bundle"]["revision"] = compute_workspace_revision(
        campaign_root / "source"
    )
    parent_pool = _freeze_parent_pool(store, job_id, campaign_root)
    research_seeds = _freeze_research_seeds(store, job_id, campaign_root)
    starter_seeds = _snapshot_starter_seeds(store, job_id, campaign_root)
    research_context = _freeze_research_context(store, job_id)
    campaign_policy = {
        **spec.evolution,
        "same_family_non_wins": spec.stuck_same_family_non_wins,
    }
    campaign_policy.setdefault("max_attempts_per_idea", 3)
    campaign_policy.setdefault(
        "max_quick_attempts",
        int(campaign_policy["generated_programs"])
        * int(campaign_policy["max_attempts_per_idea"]),
    )
    campaign_policy.setdefault("wildcard_slots", 2)
    campaign_policy.setdefault("elite_min_validation_trades", 8)
    campaign_policy.setdefault("elite_participation_target_trades", 12)
    campaign_schema = (
        SCHEMA_VERSION
        if campaign_policy.get("investigation_design_enabled", True)
        else "1.1"
    )
    manifest = {
        "schema_version": campaign_schema,
        "campaign_id": campaign_id,
        "job_id": job_id,
        "started_at": current.isoformat(),
        "deadline_at": deadline.isoformat(),
        "source_revision": source_revision,
        "source_bundle": snapshots["source_bundle"],
        "dataset": snapshots["dataset"],
        "features": snapshots["features"],
        "forward_experience": snapshots["forward_experience"],
        "forward_context_cutoff": experience["forward_context_cutoff"],
        "execution_calibration": experience["live_execution"]["recommended"],
        "historical_lessons": historical_lessons,
        "casebook": cases,
        "parent_pool": parent_pool,
        "research_seeds": research_seeds,
        "starter_seeds": starter_seeds,
        "research_context": research_context,
        "policy": campaign_policy,
        **revision_stamp(root),
    }
    manifest_path = campaign_root / "manifest.json"
    atomic_write_json(manifest_path, manifest)
    # The design pack aggregates checked-in diagnostics only. Candidate
    # evaluation owns fresh simulations; campaign start must stay a cheap
    # control-plane operation rather than adding another incumbent backtest.
    baseline = _existing_baseline_receipt(root)
    diagnostic_pack = build_diagnostic_pack(
        root,
        campaign_id=campaign_id,
        created_at=current.isoformat(),
        baseline=baseline,
        historical_lessons=historical_lessons,
        research_context=research_context,
    )
    diagnostic_path = campaign_root / DIAGNOSTIC_PACK
    atomic_write_json(diagnostic_path, diagnostic_pack)
    manifest["diagnostic_pack"] = {
        "path": f"{relative_root}/{DIAGNOSTIC_PACK}",
        "sha256": _file_hash(diagnostic_path),
    }
    atomic_write_json(manifest_path, manifest)
    state = {
        "schema_version": campaign_schema,
        "campaign_id": campaign_id,
        "status": "active",
        "stage": "design" if campaign_schema == SCHEMA_VERSION else "generate",
        "started_at": current.isoformat(),
        "deadline_at": deadline.isoformat(),
        "manifest": f"{relative_root}/manifest.json",
        "diagnostic_pack": manifest["diagnostic_pack"]["path"],
        "campaign_design": f"{relative_root}/{CAMPAIGN_DESIGN}",
        "forward_context_cutoff": manifest["forward_context_cutoff"],
        "candidates": [],
        "counts": {
            "generated": 0,
            "quick_evaluated": 0,
            "full_dev": 0,
            "proposed": 0,
            **(
                {"quick_attempts": 0, "repairs": 0}
                if campaign_schema == SCHEMA_VERSION
                else {}
            ),
        },
        "budgets": _campaign_budgets(campaign_policy),
    }
    store.write_json(job_id, CAMPAIGN_STATE_PATH, state)
    store.append_journal(
        job_id,
        {
            "type": "evolution_campaign_started",
            "campaign_id": campaign_id,
            "stage": state["stage"],
            "diagnostic_pack": manifest["diagnostic_pack"],
        },
    )
    return state


def _existing_baseline_receipt(root: Path) -> dict[str, Any]:
    path = root / "results/backtest/baseline.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False, "reason": "baseline backtest unavailable"}
    if not isinstance(document, dict):
        return {"available": False, "reason": "baseline backtest is not an object"}
    nested_result = document.get("result")
    result = nested_result if isinstance(nested_result, dict) else document
    return {
        "available": True,
        "run_id": result.get("run_id"),
        "stats": result.get("stats") or {},
        "validation": result.get("validation") or {},
        "source": {
            "path": "results/backtest/baseline.json",
            "sha256": _file_hash(path),
        },
    }


def prepare_candidate(
    store: JobStore,
    job_id: str,
    *,
    family: str | None = None,
    summary: str | None = None,
    mutation_kind: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        return _prepare_candidate(
            store,
            job_id,
            family=family,
            summary=summary,
            mutation_kind=mutation_kind,
            now=now,
        )


def submit_campaign_design(
    store: JobStore,
    job_id: str,
    *,
    campaign_design: dict[str, Any],
) -> dict[str, Any]:
    """Validate and freeze the one thinking turn before generation."""
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = _active_campaign(store, job_id)
        if str(state.get("schema_version") or "") != SCHEMA_VERSION:
            raise ValueError("legacy evolution campaigns do not accept a design")
        campaign_id = str(state["campaign_id"])
        manifest = _campaign_manifest(store, job_id, campaign_id)
        root = store.job_dir(job_id)
        pack = store.read_json(job_id, str(state["diagnostic_pack"]), default={}) or {}
        normalized = _validate_campaign_design(
            campaign_design, manifest=manifest, diagnostic_pack=pack
        )
        design_path = root / str(state["campaign_design"])
        if design_path.exists():
            existing = json.loads(design_path.read_text(encoding="utf-8"))
            comparable_existing = {
                key: value for key, value in existing.items() if key != "created_at"
            }
            comparable_normalized = {
                key: value for key, value in normalized.items() if key != "created_at"
            }
            if comparable_existing != comparable_normalized:
                raise ValueError("campaign design is immutable once accepted")
            return existing
        atomic_write_json(design_path, normalized)
        state["stage"] = "generate"
        state["design"] = {
            "path": str(state["campaign_design"]),
            "sha256": _file_hash(design_path),
            "hypotheses": len(normalized["hypotheses"]),
            "slots": len(normalized["slots"]),
            "wildcards": sum(bool(slot["wildcard"]) for slot in normalized["slots"]),
            "falsified_hypotheses": [],
        }
        _save_campaign(store, job_id, state)
    store.append_journal(
        job_id,
        {
            "type": "evolution_campaign_design_committed",
            "campaign_id": campaign_id,
            **state["design"],
        },
    )
    return normalized


def _research_parent_available(
    manifest: dict[str, Any], diagnostic_pack: dict[str, Any]
) -> bool:
    context = diagnostic_pack.get("research_context") or {}
    return bool(
        manifest.get("research_seeds")
        or context.get("validated_positives")
        or context.get("refuted_families")
    )


def _validate_campaign_design(
    raw: dict[str, Any], *, manifest: dict[str, Any], diagnostic_pack: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("campaign_design must be an object")
    hypotheses = raw.get("hypotheses")
    slots = raw.get("slots")
    if not isinstance(hypotheses, list) or not 3 <= len(hypotheses) <= 5:
        raise ValueError("campaign design requires 3-5 grounded hypotheses")
    expected_slots = int(manifest["policy"]["generated_programs"])
    if not isinstance(slots, list) or len(slots) != expected_slots:
        raise ValueError(f"campaign design requires exactly {expected_slots} slots")
    hypothesis_by_id: dict[str, dict[str, Any]] = {}
    normalized_hypotheses: list[dict[str, Any]] = []
    for index, hypothesis in enumerate(hypotheses, start=1):
        if not isinstance(hypothesis, dict):
            raise ValueError("every hypothesis must be an object")
        hypothesis_id = str(hypothesis.get("id") or f"h{index:02d}").strip()
        if hypothesis_id in hypothesis_by_id:
            raise ValueError(f"duplicate hypothesis id: {hypothesis_id}")
        family = str(hypothesis.get("family") or "").strip()
        mechanism = str(hypothesis.get("causal_mechanism") or "").strip()
        falsifier = str(hypothesis.get("falsifier") or "").strip()
        raw_refs = hypothesis.get("evidence_refs")
        if (
            not isinstance(raw_refs, list)
            or not raw_refs
            or not all(isinstance(ref, str) and ref.strip() for ref in raw_refs)
        ):
            raise ValueError(
                f"hypothesis {hypothesis_id} evidence_refs must be a non-empty "
                "list of JSON pointers"
            )
        refs = [ref.strip() for ref in raw_refs]
        if not family or not mechanism or not falsifier or not refs:
            raise ValueError(
                f"hypothesis {hypothesis_id} needs family, causal_mechanism, "
                "falsifier, and evidence_refs"
            )
        for ref in refs:
            resolve_json_pointer(diagnostic_pack, ref)
        normalized = {
            "id": hypothesis_id,
            "family": family[:120],
            "causal_mechanism": mechanism[:800],
            "falsifier": falsifier[:500],
            "evidence_refs": refs[:12],
        }
        hypothesis_by_id[hypothesis_id] = normalized
        normalized_hypotheses.append(normalized)
    allowed_sources = {
        "incumbent",
        "qd_elite",
        "crossover",
        "de_novo",
        "starter_seed",
        "research_seed",
        "research_context",
    }
    available_starter_ids = {
        str(item.get("starter_id") or "")
        for item in manifest.get("starter_seeds") or []
        if item.get("starter_id")
    }
    available_research_seed_ids = {
        str(item.get("seed_id") or "")
        for item in manifest.get("research_seeds") or []
        if item.get("seed_id")
    }
    normalized_slots: list[dict[str, Any]] = []
    seen_slot_ids: set[str] = set()
    for index, slot in enumerate(slots, start=1):
        if not isinstance(slot, dict):
            raise ValueError("every design slot must be an object")
        slot_id = str(slot.get("slot_id") or f"s{index:02d}").strip()
        if slot_id in seen_slot_ids:
            raise ValueError(f"duplicate design slot id: {slot_id}")
        seen_slot_ids.add(slot_id)
        wildcard_value = slot.get("wildcard")
        if not isinstance(wildcard_value, bool):
            raise ValueError(f"slot {slot_id} wildcard must be true or false")
        wildcard = wildcard_value
        hypothesis_id = str(slot.get("hypothesis_id") or "").strip()
        if wildcard and hypothesis_id:
            raise ValueError(f"wildcard slot {slot_id} cannot cite a hypothesis")
        if not wildcard and hypothesis_id not in hypothesis_by_id:
            raise ValueError(f"grounded slot {slot_id} has an unknown hypothesis")
        source = str(slot.get("parent_source") or "").strip()
        if source not in allowed_sources:
            raise ValueError(f"slot {slot_id} has unsupported parent_source {source!r}")
        starter_seed_id = str(slot.get("starter_seed_id") or "").strip()
        research_seed_id = str(slot.get("research_seed_id") or "").strip()
        if starter_seed_id:
            if source != "starter_seed":
                raise ValueError(
                    f"slot {slot_id} starter_seed_id requires parent_source "
                    "starter_seed"
                )
            if starter_seed_id not in available_starter_ids:
                raise ValueError(
                    f"slot {slot_id} names unavailable starter_seed_id "
                    f"{starter_seed_id!r}"
                )
        if research_seed_id:
            if source != "research_seed":
                raise ValueError(
                    f"slot {slot_id} research_seed_id requires parent_source "
                    "research_seed"
                )
            if research_seed_id not in available_research_seed_ids:
                raise ValueError(
                    f"slot {slot_id} names unavailable research_seed_id "
                    f"{research_seed_id!r}"
                )
        mutation = str(slot.get("mutation_kind") or "structural").strip()
        if mutation not in {"structural", "parameter"}:
            raise ValueError(
                f"slot {slot_id} mutation_kind must be structural or parameter"
            )
        family = str(slot.get("family") or "").strip()
        summary = str(slot.get("summary") or "").strip()
        if not family or not summary:
            raise ValueError(f"slot {slot_id} requires family and summary")
        normalized_slot = {
            "slot_id": slot_id,
            "wildcard": wildcard,
            "hypothesis_id": hypothesis_id or None,
            "parent_source": source,
            "mutation_kind": mutation,
            "family": family[:120],
            "summary": summary[:240],
        }
        if starter_seed_id:
            normalized_slot["starter_seed_id"] = starter_seed_id
        if research_seed_id:
            normalized_slot["research_seed_id"] = research_seed_id
        normalized_slots.append(normalized_slot)
    wildcard_count = sum(bool(slot["wildcard"]) for slot in normalized_slots)
    if wildcard_count != int(manifest["policy"].get("wildcard_slots") or 0):
        raise ValueError("campaign design has the wrong number of wildcard slots")
    grounded = [slot for slot in normalized_slots if not slot["wildcard"]]
    sources = {str(slot["parent_source"]) for slot in grounded}
    if "starter_seed" not in sources:
        raise ValueError("grounded design requires an explicit starter_seed slot")
    if _research_parent_available(manifest, diagnostic_pack) and not sources & {
        "research_seed",
        "research_context",
    }:
        raise ValueError("grounded design requires an explicit research slot")
    if "de_novo" not in sources:
        raise ValueError("grounded design requires an explicit de_novo slot")
    available_parents = (manifest.get("parent_pool") or {}).get("candidates") or []
    exploration_parents = [
        parent
        for parent in available_parents
        if str(parent.get("status") or "") != "incumbent"
    ]
    if exploration_parents and not sources & {"qd_elite", "crossover"}:
        raise ValueError(
            "grounded design requires a qd_elite or crossover slot when parents exist"
        )
    if sum(slot["parent_source"] == "incumbent" for slot in grounded) > 1:
        raise ValueError("grounded design permits at most one incumbent slot")
    if sum(slot["mutation_kind"] == "parameter" for slot in normalized_slots) > 2:
        raise ValueError("campaign design permits at most two parameter slots")
    if not any(
        slot["wildcard"] and slot["parent_source"] == "de_novo"
        for slot in normalized_slots
    ):
        raise ValueError("one wildcard slot must be de_novo")
    for slot in grounded:
        if slot["parent_source"] != "research_context":
            continue
        hypothesis = hypothesis_by_id[str(slot["hypothesis_id"])]
        if not any(
            ref.startswith(
                (
                    "/baseline",
                    "/attribution",
                    "/trade_forensics",
                    "/counterfactual",
                    "/regime_health",
                    "/research_context",
                    "/prior_campaign_lessons",
                )
            )
            for ref in hypothesis["evidence_refs"]
        ):
            raise ValueError("research_context slots must cite checked-in research")
    return {
        "schema_version": "1.0",
        "campaign_id": manifest["campaign_id"],
        "created_at": utc_now_iso(),
        "hypotheses": normalized_hypotheses,
        "slots": normalized_slots,
    }


def submit_research_seed(
    store: JobStore,
    job_id: str,
    *,
    candidate_root: Path,
    family: str,
    hypothesis: str,
    base_revision: str,
    evidence_refs: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Freeze a sensor-authored executable seed for the next campaign.

    Submission confers no evidence or deployment authority.  The seed is a
    source-aware parent only and must re-earn every evolution/probation gate.
    """
    current = _campaign_now(now)
    root = store.job_dir(job_id).resolve()
    source = candidate_root.resolve()
    if not source.is_relative_to(root):
        raise ValueError("research seed must be authored inside its job root")
    current_revision = compute_workspace_revision(root)
    if str(base_revision) != current_revision:
        raise ValueError(
            "research seed base revision is stale; rebase it on the current job"
        )
    validation = validate_execution_job(job_id, candidate_dir=source, store=store)
    if not _candidate_validation_passed(validation):
        raise ValueError("research seed does not satisfy the executable job contract")
    revision = compute_workspace_revision(source)
    if revision == current_revision:
        raise ValueError("research seed is byte-identical to the incumbent")
    seed_raw = f"{family}|{revision}"
    seed_id = f"seed-{hashlib.sha256(seed_raw.encode()).hexdigest()[:12]}"
    seed_family = str(family).strip() or "research"
    seed_hypothesis = str(hypothesis).strip()[:500]
    seed_evidence = verify_job_evidence_refs(
        root,
        list(evidence_refs or [])[:20],
        allowed_roots=("results/research",),
        now=current,
    )
    if not seed_evidence:
        raise ValueError("research seed requires a checked-in research result")
    relative = f"{RESEARCH_SEED_ROOT}/{seed_id}/{revision}"
    destination = root / relative
    with job_state_lock(store.repo_root, job_id, name="evolution_research_seeds"):
        state = store.read_json(job_id, RESEARCH_SEED_STATE_PATH, default={}) or {}
        seeds = list(state.get("seeds") or [])
        duplicate = next(
            (item for item in seeds if item.get("revision") == revision), None
        )
        if duplicate is not None:
            return {
                "status": "duplicate",
                "seed_id": duplicate.get("seed_id"),
                "revision": duplicate.get("revision"),
                "seed_status": duplicate.get("status"),
            }
        copy_job_bundle(source, destination)
        if compute_workspace_revision(destination) != revision:
            raise ValueError("research seed revision changed during freeze")
        seed = {
            "seed_id": seed_id,
            "family": seed_family,
            "hypothesis": seed_hypothesis,
            "base_revision": current_revision,
            "revision": revision,
            "bundle": relative,
            "status": "pending",
            "submitted_at": current.isoformat(),
            "evidence_refs": seed_evidence,
            **revision_stamp(root),
        }
        seeds.append(seed)
        atomic_write_json(
            root / RESEARCH_SEED_STATE_PATH,
            {"schema_version": "1.0", "seeds": seeds},
        )
    record_candidate(
        store,
        job_id,
        candidate_id=seed_id,
        family=seed_family,
        summary=seed_hypothesis,
        status="research_seed",
        objective=None,
        revision=revision,
        parent_id=current_revision,
        evidence="; ".join(seed_evidence)[:300],
        metadata={"executable_bundle": relative, "source": "research_sensor"},
    )
    store.append_journal(
        job_id,
        {
            "type": "evolution_research_seed_submitted",
            "seed_id": seed_id,
            "family": seed_family,
            "revision": revision,
        },
    )
    return seed


def _prepare_candidate(
    store: JobStore,
    job_id: str,
    *,
    family: str | None,
    summary: str | None,
    mutation_kind: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    state = _active_campaign(store, job_id)
    if _campaign_now(now) >= _parse(state["deadline_at"]):
        raise ValueError("evolution campaign generation deadline has elapsed")
    manifest = _campaign_manifest(store, job_id, str(state["campaign_id"]))
    policy = manifest["policy"]
    limit = int(policy["generated_programs"])
    slot = len(state["candidates"]) + 1
    if slot > limit:
        raise ValueError(f"campaign generated-program budget exhausted ({limit})")

    designed = str(state.get("schema_version") or "") == SCHEMA_VERSION
    design_slot: dict[str, Any] = {}
    hypothesis: dict[str, Any] = {}
    if designed:
        if state.get("stage") != "generate":
            raise ValueError("campaign design must be accepted before preparation")
        design = (
            store.read_json(job_id, str(state["campaign_design"]), default={}) or {}
        )
        try:
            design_slot = dict(design["slots"][slot - 1])
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("campaign design has no next slot") from exc
        falsified = set((state.get("design") or {}).get("falsified_hypotheses") or [])
        if (
            not design_slot.get("wildcard")
            and design_slot.get("hypothesis_id") in falsified
        ):
            used = {
                str(item.get("hypothesis_id") or "")
                for item in state.get("candidates") or []
            }
            replacement = next(
                (
                    item
                    for item in design.get("hypotheses") or []
                    if item.get("id") not in falsified and item.get("id") not in used
                ),
                next(
                    (
                        item
                        for item in design.get("hypotheses") or []
                        if item.get("id") not in falsified
                    ),
                    None,
                ),
            )
            if replacement is not None:
                design_slot["hypothesis_id"] = replacement["id"]
                design_slot["family"] = replacement["family"]
                design_slot["summary"] = str(replacement["causal_mechanism"])[:240]
        family = str(design_slot["family"])
        summary = str(design_slot["summary"])
        requested_source = str(design_slot["parent_source"])
        chosen_mutation = str(design_slot["mutation_kind"])
        hypothesis = next(
            (
                dict(item)
                for item in design.get("hypotheses") or []
                if item.get("id") == design_slot.get("hypothesis_id")
            ),
            {},
        )
        forced_jump = False
    else:
        if not family or not summary:
            raise ValueError("legacy evolution_prepare requires family and summary")
        forced_jump = _same_family_nonwins(
            state, family, int(policy["same_family_non_wins"])
        )
        requested_source = (
            "de_novo" if forced_jump else _parent_source(slot, policy["parent_mix"])
        )
        research_seed_slots = int(policy.get("research_seed_slots") or 0)
        if slot <= research_seed_slots and manifest.get("research_seeds"):
            requested_source = "research_seed"
        required_structural = math.ceil(
            limit * float(policy.get("min_structural_fraction") or 0.0)
        )
        allowed_parameter_slots = max(
            0,
            min(
                int(limit * float(policy["max_parameter_fraction"])),
                limit - required_structural,
            ),
        )
        parameter_slots = (
            {
                max(1, round((index + 1) * limit / allowed_parameter_slots))
                for index in range(allowed_parameter_slots)
            }
            if allowed_parameter_slots
            else set()
        )
        chosen_mutation = mutation_kind or (
            "parameter" if slot in parameter_slots else "structural"
        )
        if chosen_mutation not in {"structural", "parameter"}:
            raise ValueError("mutation_kind must be structural or parameter")
        if chosen_mutation == "parameter" and slot not in parameter_slots:
            chosen_mutation = "structural"
        if forced_jump:
            chosen_mutation = "structural"

    parent_plan = _select_parent_plan(
        manifest,
        requested_source=requested_source,
        requested_starter_id=str(design_slot.get("starter_seed_id") or "") or None,
        requested_research_seed_id=(
            str(design_slot.get("research_seed_id") or "") or None
        ),
        slot=slot,
        candidates=state["candidates"],
    )
    source = str(parent_plan["source"])
    parents = [str(item["candidate_id"]) for item in parent_plan.get("parents") or []]
    candidate_id = f"{state['campaign_id']}-c{slot:02d}"
    relative = f"{CAMPAIGN_ROOT}/{state['campaign_id']}/candidates/{candidate_id}"
    candidate_root = store.job_dir(job_id) / relative
    seeded_window = _materialize_candidate_seed(
        store,
        job_id,
        campaign_id=str(state["campaign_id"]),
        candidate_root=candidate_root,
        plan=parent_plan,
    )
    seed_revision = compute_workspace_revision(candidate_root)
    reference_relative = (
        f"{CAMPAIGN_ROOT}/{state['campaign_id']}/references/{candidate_id}"
    )
    copy_job_bundle(
        store.job_dir(job_id) / relative, store.job_dir(job_id) / reference_relative
    )
    candidate = {
        "candidate_id": candidate_id,
        "campaign_id": state["campaign_id"],
        "slot": slot,
        "family": str(family),
        "summary": str(summary)[:160],
        "status": "prepared",
        "parent_source": source,
        "requested_parent_source": requested_source,
        "parent_candidate_ids": parents,
        "starter_seed_id": (parent_plan.get("starter") or {}).get("starter_id"),
        "research_seed_id": (parent_plan.get("research_seed") or {}).get("seed_id"),
        "secondary_parent_bundle": (parent_plan.get("secondary") or {}).get("bundle"),
        "mutation_kind": chosen_mutation,
        "forced_jump": forced_jump,
        "bundle": relative,
        "warmup_bars": seeded_window,
        "seed_revision": seed_revision,
        "reference_bundle": reference_relative,
        "evidence_reset": source in {"starter_seed", "research_seed"},
        "design_slot_id": design_slot.get("slot_id"),
        "hypothesis_id": design_slot.get("hypothesis_id"),
        "wildcard": bool(design_slot.get("wildcard")),
        "evidence_refs": list(hypothesis.get("evidence_refs") or []),
        "causal_mechanism": hypothesis.get("causal_mechanism"),
        "falsifier": hypothesis.get("falsifier"),
        "attempt_count": 0,
        "attempts": [],
        "prepared_at": utc_now_iso(),
    }
    atomic_write_json(candidate_root / "candidate.json", candidate)
    state["candidates"].append(candidate)
    state["counts"]["generated"] = slot
    store.write_json(job_id, CAMPAIGN_STATE_PATH, state)
    record_candidate(
        store,
        job_id,
        candidate_id=candidate_id,
        family=str(family),
        summary=str(summary),
        status="generated",
        objective=None,
        parent_candidate_ids=parents,
        metadata={
            "campaign_id": state["campaign_id"],
            "bundle": relative,
            "mutation_kind": chosen_mutation,
            "parent_source": source,
            "requested_parent_source": requested_source,
            "starter_seed_id": candidate.get("starter_seed_id"),
            "research_seed_id": candidate.get("research_seed_id"),
            "seed_revision": seed_revision,
            "evidence_reset": candidate["evidence_reset"],
            "design_slot_id": candidate.get("design_slot_id"),
            "hypothesis_id": candidate.get("hypothesis_id"),
            "wildcard": candidate.get("wildcard"),
        },
    )
    # ``bundle`` stays durable and job-relative.  The tool response also gives
    # the short-lived mutation worker an exact authorized path so a fresh stage
    # does not spend context discovering the SDK's hosted symlink layout.
    result = dict(candidate)
    result["bundle_path"] = str(candidate_root.resolve())
    secondary = parent_plan.get("secondary") or {}
    if secondary.get("bundle"):
        result["secondary_parent_bundle_path"] = str(
            _resolve_frozen_parent_bundle(
                store,
                job_id,
                str(state["campaign_id"]),
                str(secondary["bundle"]),
            )
        )
    return result


def _source_baseline_revision(
    manifest: dict[str, Any], candidate: dict[str, Any] | None = None
) -> str:
    """Revision an unedited candidate seed carries.

    New campaigns stamp the actual incumbent/QD/starter/de-novo seed; legacy
    campaigns fall back to their frozen incumbent source revision.
    """
    return str(
        (candidate or {}).get("seed_revision")
        or (manifest.get("source_bundle") or {}).get("revision")
        or manifest.get("source_revision")
        or ""
    )


def _seed_bundle_window(store: JobStore, job_id: str, bundle_root: Path) -> int:
    """Campaign candidates must declare their compute window up front —
    backtest and live hand decide() the same bounded view only when
    warmup_bars is explicit in params. Inherit the source bundle's declared
    window; otherwise seed the simulator's default cap."""
    job_data = _load_job_yaml(bundle_root)
    params = dict(job_data.get("execution_params") or {})
    if params.get("warmup_bars"):
        return int(params["warmup_bars"])
    script = store.resolve_script_entrypoint(
        job_id, job_data, candidate_dir=bundle_root
    )
    strategy = (
        _load_strategy(script, dict(params))
        if script is not None and script.exists()
        else None
    )
    window = resolve_compute_window(params, strategy)
    seeded = window.size if window.declared and window.size else DEFAULT_WARMUP_BARS
    params["warmup_bars"] = seeded
    job_data["execution_params"] = params
    atomic_write_text(
        bundle_root / "job.yaml", yaml.safe_dump(job_data, sort_keys=False)
    )
    return seeded


def _require_declared_window(subject: dict[str, Any], params: dict[str, Any]) -> None:
    """Undeclared (or full_history) candidates are inadmissible: the campaign
    contract is that whatever the backtest measured is what live would run."""
    window = resolve_compute_window(
        params, _load_strategy(subject["script"], dict(params))
    )
    if not window.declared:
        raise ValueError(
            "candidate does not declare a bounded compute window "
            f"(execution_params.warmup_bars) — {BOUNDED_WINDOW_HINT}"
        )


def evaluate_candidate(
    store: JobStore, job_id: str, candidate_id: str
) -> dict[str, Any]:
    """Claim, compute outside campaign state, then commit idempotently."""
    claim_id = uuid.uuid4().hex
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = _active_campaign(store, job_id)
        candidate = _candidate(state, candidate_id)
        if candidate["status"] == "quick_running":
            try:
                claim_age = _campaign_now() - _parse(candidate["evaluation_claimed_at"])
            except (KeyError, TypeError, ValueError):
                claim_age = timedelta.max
            if claim_age < timedelta(hours=2):
                return candidate
        elif candidate["status"] not in {
            "prepared",
            "quick_failed",
            "repair_pending",
        }:
            return candidate
        campaign_id = str(state["campaign_id"])
        candidate.update(
            {
                "status": "quick_running",
                "evaluation_claim_id": claim_id,
                "evaluation_claimed_at": utc_now_iso(),
            }
        )
        candidate_snapshot = dict(candidate)
        _save_campaign(store, job_id, state)
    try:
        with experiment_compute_lock(
            store, job_id, label=f"evolution-evaluate:{job_id}"
        ):
            with evolution_resource_phase(
                store,
                job_id,
                phase="quick_evaluate",
                candidate_id=candidate_id,
            ):
                outcome = _evaluate_candidate(
                    store, job_id, candidate_snapshot, campaign_id=campaign_id
                )
    except (ComputeLockBusy, TransientInfrastructureError):
        _release_finalize_claim(
            store,
            job_id,
            campaign_id=campaign_id,
            candidate_id=candidate_id,
            claim_id=claim_id,
            claim_field="evaluation_claim_id",
            restored_status="quick_failed",
        )
        raise
    except Exception as exc:  # noqa: BLE001 - candidate failure is evidence
        outcome = {"status": "invalid", "evidence": {"error": str(exc)[:500]}}
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = campaign_status(store, job_id)
        if str(state.get("campaign_id") or "") != campaign_id:
            return {**outcome, "commit_skipped": "campaign changed"}
        candidate = _candidate(state, candidate_id)
        if candidate.get("evaluation_claim_id") != claim_id:
            return candidate
        if str(state.get("schema_version") or "") != SCHEMA_VERSION:
            candidate.update(outcome)
            candidate["evaluated_at"] = utc_now_iso()
            state["counts"]["quick_evaluated"] += 1
        else:
            _commit_designed_attempt(
                store,
                job_id,
                state=state,
                candidate=candidate,
                outcome=outcome,
            )
        candidate.pop("evaluation_claim_id", None)
        candidate.pop("evaluation_claimed_at", None)
        _save_campaign(store, job_id, state)
    _archive_campaign_candidate(store, job_id, candidate)
    return candidate


def _evaluate_candidate(
    store: JobStore,
    job_id: str,
    candidate: dict[str, Any],
    *,
    campaign_id: str,
) -> dict[str, Any]:
    """Compute one low-fidelity screen without mutating campaign state."""
    candidate_root = resolve_candidate_bundle(
        store, job_id, candidate, campaign_id=campaign_id
    )
    try:
        search_space = _load_candidate_search_space(
            candidate_root,
            required=candidate.get("mutation_kind") == "parameter",
        )
    except ValueError as exc:
        return {"status": "invalid", "evidence": {"error": str(exc)[:500]}}
    report = validate_execution_job(job_id, candidate_dir=candidate_root, store=store)
    if not _candidate_validation_passed(report):
        return {"status": "invalid", "evidence": {"validation": report}}
    revision = compute_workspace_revision(candidate_root)
    manifest = (
        store.read_json(
            job_id, f"{CAMPAIGN_ROOT}/{campaign_id}/manifest.json", default={}
        )
        or {}
    )
    if (
        revision == _source_baseline_revision(manifest, candidate)
        and search_space is None
    ):
        return {
            "status": "invalid",
            "evidence": {
                "error": "candidate bundle is identical to its source revision"
            },
        }
    policy = manifest.get("policy") or {}
    tuning_preview: dict[str, Any] | None = None
    behavior_preview: dict[str, Any] | None = None
    try:
        subject = _load_subject(store, job_id, candidate_root, campaign_id=campaign_id)
        train_end, validation_end = _split_bounds(
            store, job_id, campaign_id=campaign_id
        )
        train, _, _ = _split_dataset(
            subject["dataset"], train_end=train_end, validation_end=validation_end
        )
        quick = _tail(train, 10_000)
        params, _, calibration = _calibrated_params(store, job_id, subject)
        _require_declared_window(subject, params)
        probe = window_invariance_probe(
            subject["script"], quick.bars, subject["spec"], params
        )
        if probe["status"] == "failed":
            return {
                "status": "invalid",
                "evidence": {
                    "error": (
                        f"window-invariance probe failed at bar {probe['bar']}: "
                        "decide() consumed history beyond its declared window "
                        f"of {probe['window']} bars — {BOUNDED_WINDOW_HINT}"
                    ),
                    "probe": {
                        key: value
                        for key, value in probe.items()
                        if not key.endswith("_intents")
                    },
                },
            }
        if (
            bool(policy.get("behavior_preview_enabled"))
            and candidate.get("mutation_kind") == "parameter"
            and search_space is not None
        ):
            behavior_preview = parameter_behavior_probe(
                subject["script"],
                quick.bars,
                subject["spec"],
                params,
                search_space_probe_variants(search_space),
            )
            if behavior_preview["status"] == "unchanged":
                bars_probed = int(behavior_preview.get("bars_probed") or 0)
                variants = int(behavior_preview.get("variants_declared") or 0)
                error = (
                    "Declared parameter endpoints changed no material order "
                    f"intents across {bars_probed} sampled bars and {variants} "
                    "bounded variants; change the search space or mechanism "
                    "before simulation."
                )
                return {
                    "status": "low_fidelity_rejected",
                    "evidence": (
                        "parameter behavior preview found no material intent change"
                    ),
                    "quick_simulation_ran": False,
                    "behavior_preview": behavior_preview,
                    "postmortem": {
                        "viable": False,
                        "primary_failure": "no_behavior_change",
                        "failure_codes": ["no_behavior_change"],
                        "behavior_diff": {"material_change": False},
                        "repair_context": {"error": error},
                    },
                }
        result = simulate_execution(subject["script"], quick, subject["spec"], params)
        if (
            candidate.get("mutation_kind") == "parameter"
            and search_space is not None
            and result.validation.get("execution_valid")
            and int(result.stats.get("trade_count") or 0) > 0
        ):
            tuning_preview = _parameter_tuning_preview(
                subject,
                train,
                params,
                search_space,
                policy,
            )
    except Exception as exc:  # noqa: BLE001 - candidate failure is evidence
        # Contract/validation failures are candidate evidence even when their
        # explanation mentions "memory" (for example, bounded incremental
        # memory). Do not let the deliberately broad infrastructure string
        # classifier turn those into retry loops.
        if isinstance(exc, ValueError):
            return {"status": "invalid", "evidence": {"error": str(exc)[:500]}}
        if isinstance(exc, MemoryError) or classify_failure(str(exc)) == (
            "infrastructure"
        ):
            raise TransientInfrastructureError(str(exc)) from exc
        return {"status": "invalid", "evidence": {"error": str(exc)[:500]}}
    compact = _compact_result(result)
    if not result.validation.get("execution_valid"):
        return {
            "status": "low_fidelity_rejected",
            "evidence": compact,
            "quick_simulation_ran": True,
        }
    common: dict[str, Any] = {
        "revision": revision,
        "quick": compact,
        "objective": _objective(result.stats, params),
        "behavior": _behavior(result, quick, subject["spec"]),
        "execution_calibration": calibration,
        "tuning_eligible": search_space is not None,
        "quick_simulation_ran": True,
    }
    if candidate.get("reference_bundle"):
        receipt = result_receipt(
            result,
            revision=revision,
            objective=common["objective"],
            behavior=common["behavior"],
        )
        reference = _candidate_reference_receipt(
            store,
            job_id,
            candidate,
            campaign_id=campaign_id,
        )
        previous = _latest_attempt_receipt(store, job_id, candidate)
        common["attempt_receipt"] = receipt
        common["postmortem"] = build_postmortem(
            receipt,
            reference,
            previous=previous,
            # The quick screen only establishes that the mechanism runs and
            # changes behavior. The eight-trade participation floor belongs
            # to independent validation, where elite eligibility is decided.
            min_trades=1,
        )
    if search_space is None:
        common["tuning_skip_reason"] = "no_typed_search_space"
    if tuning_preview is not None:
        common["tuning_preview"] = tuning_preview
    if behavior_preview is not None:
        common["behavior_preview"] = behavior_preview
    if int(result.stats.get("trade_count") or 0) <= 0:
        return {
            **common,
            "status": "low_fidelity_rejected",
            "evidence": "quick screen produced no closed trades",
        }
    return {
        **common,
        "status": "quick_complete",
        "evidence": "low-fidelity train screen passed",
    }


def _candidate_reference_receipt(
    store: JobStore,
    job_id: str,
    candidate: dict[str, Any],
    *,
    campaign_id: str,
) -> dict[str, Any]:
    relative = (
        f"{CAMPAIGN_ROOT}/{campaign_id}/reference_results/"
        f"{candidate['candidate_id']}.json"
    )
    cached = store.read_json(job_id, relative, default={}) or {}
    if cached.get("revision") == candidate.get("seed_revision"):
        return cached
    root = store.job_dir(job_id).resolve()
    reference = (root / str(candidate["reference_bundle"])).resolve()
    allowed = (root / CAMPAIGN_ROOT / campaign_id / "references").resolve()
    if (
        not reference.is_relative_to(allowed)
        or reference.parent != allowed
        or reference.name != candidate["candidate_id"]
    ):
        raise ValueError("candidate reference bundle escapes its campaign root")
    if compute_workspace_revision(reference) != candidate.get("seed_revision"):
        raise ValueError("candidate reference bundle revision mismatch")
    subject = _load_subject(store, job_id, reference, campaign_id=campaign_id)
    train_end, validation_end = _split_bounds(store, job_id, campaign_id=campaign_id)
    train, _, _ = _split_dataset(
        subject["dataset"], train_end=train_end, validation_end=validation_end
    )
    quick = _tail(train, 10_000)
    params, _, _ = _calibrated_params(store, job_id, subject)
    result = simulate_execution(subject["script"], quick, subject["spec"], params)
    receipt = result_receipt(
        result,
        revision=compute_workspace_revision(reference),
        objective=_objective(result.stats, params),
        behavior=_behavior(result, quick, subject["spec"]),
    )
    atomic_write_json(root / relative, receipt)
    return receipt


def _latest_attempt_receipt(
    store: JobStore, job_id: str, candidate: dict[str, Any]
) -> dict[str, Any] | None:
    attempts = candidate.get("attempts") or []
    if not attempts:
        return None
    relative = str(attempts[-1].get("receipt_path") or "")
    if not relative:
        return None
    receipt = store.read_json(job_id, relative, default={}) or {}
    return receipt if isinstance(receipt, dict) and receipt else None


def _commit_designed_attempt(
    store: JobStore,
    job_id: str,
    *,
    state: dict[str, Any],
    candidate: dict[str, Any],
    outcome: dict[str, Any],
) -> None:
    campaign_id = str(state["campaign_id"])
    policy = _campaign_policy(store, job_id, campaign_id)
    attempt_index = int(candidate.get("attempt_count") or 0) + 1
    candidate_root = resolve_candidate_bundle(
        store, job_id, candidate, campaign_id=campaign_id
    )
    attempt_relative = (
        f"{CAMPAIGN_ROOT}/{campaign_id}/attempts/{candidate['candidate_id']}/"
        f"a{attempt_index:02d}"
    )
    attempt_root = store.job_dir(job_id) / attempt_relative
    bundle_revision = compute_workspace_revision(candidate_root)
    attempt_bundle = attempt_root / "bundle"
    if attempt_bundle.exists():
        if compute_workspace_revision(attempt_bundle) != bundle_revision:
            raise ValueError("attempt snapshot revision mismatch")
    else:
        _copy_attempt_bundle(candidate_root, attempt_bundle)
    receipt = outcome.pop("attempt_receipt", None)
    if not isinstance(receipt, dict):
        receipt = {
            "revision": bundle_revision,
            "execution_valid": False,
            "stats": {},
            "objective": {},
            "behavior": {},
            "trades": [],
        }
    elif receipt.get("revision") != bundle_revision:
        outcome = {
            "status": "invalid",
            "evidence": {"error": "candidate changed during quick evaluation"},
        }
        receipt = {
            "revision": bundle_revision,
            "execution_valid": False,
            "stats": {},
            "objective": {},
            "behavior": {},
            "trades": [],
        }
    postmortem = outcome.pop("postmortem", None)
    if not isinstance(postmortem, dict):
        evidence = outcome.get("evidence")
        if isinstance(evidence, dict):
            error = str(evidence.get("error") or "").strip()
        else:
            error = str(evidence or "").strip()
        postmortem = {
            "viable": False,
            "primary_failure": "invalid_execution",
            "failure_codes": ["invalid_execution"],
            "behavior_diff": {"material_change": False},
        }
        if error:
            # The repair worker reads only this bounded artifact. Preserve the
            # deterministic validator cause so it does not have to rediscover
            # the failure through broad reads or forbidden tool discovery.
            postmortem["repair_context"] = {"error": error[:500]}
    receipt_relative = f"{attempt_relative}/receipt.json"
    postmortem_relative = f"{attempt_relative}/postmortem.json"
    atomic_write_json(store.job_dir(job_id) / receipt_relative, receipt)
    atomic_write_json(store.job_dir(job_id) / postmortem_relative, postmortem)
    stored_outcome = {
        key: value
        for key, value in outcome.items()
        if key not in {"status", "evidence"}
    }
    attempt = {
        "attempt": attempt_index,
        "evaluated_at": utc_now_iso(),
        "status": outcome.get("status"),
        "revision": receipt.get("revision"),
        "execution_valid": bool(receipt.get("execution_valid")),
        "bundle": f"{attempt_relative}/bundle",
        "receipt_path": receipt_relative,
        "postmortem_path": postmortem_relative,
        "postmortem": postmortem,
        "outcome": stored_outcome,
        "evidence": _compact_evidence(outcome.get("evidence") or postmortem),
    }
    candidate.setdefault("attempts", []).append(attempt)
    candidate["attempt_count"] = attempt_index
    candidate["latest_postmortem_path"] = postmortem_relative
    candidate["evaluated_at"] = attempt["evaluated_at"]
    counts = state.setdefault("counts", {})
    counts["quick_attempts"] = int(counts.get("quick_attempts") or 0) + 1
    max_attempts = int(policy.get("max_attempts_per_idea") or 3)
    global_cap = int(
        policy.get("max_quick_attempts")
        or int(policy["generated_programs"]) * max_attempts
    )
    before_drain = _campaign_now() < _parse(state["deadline_at"]) - CAMPAIGN_DRAIN
    room = attempt_index < max_attempts and counts["quick_attempts"] < global_cap
    repair = bool(
        before_drain
        and room
        and not bool(postmortem.get("viable"))
        and (attempt_index == 1 or attempt_made_progress(postmortem))
    )
    if repair:
        candidate["status"] = "repair_pending"
        candidate["evidence"] = postmortem
        counts["repairs"] = int(counts.get("repairs") or 0) + 1
        atomic_write_json(candidate_root / "candidate.json", candidate)
        return
    _close_designed_candidate(store, job_id, state=state, candidate=candidate)


def _close_designed_candidate(
    store: JobStore,
    job_id: str,
    *,
    state: dict[str, Any],
    candidate: dict[str, Any],
) -> None:
    campaign_id = str(state["campaign_id"])
    candidate_root = resolve_candidate_bundle(
        store, job_id, candidate, campaign_id=campaign_id
    )
    viable = [
        item
        for item in candidate["attempts"]
        if bool((item.get("postmortem") or {}).get("viable"))
    ]
    pool = viable or list(candidate["attempts"])
    best = max(
        pool,
        key=lambda item: (
            bool(item.get("execution_valid")),
            bool(
                (item.get("postmortem") or {})
                .get("behavior_diff", {})
                .get("material_change")
            ),
            _candidate_score(item.get("outcome") or {}),
            int(item.get("attempt") or 0),
        ),
    )
    best_bundle = store.job_dir(job_id) / str(best["bundle"])
    if compute_workspace_revision(candidate_root) != best.get("revision"):
        _replace_candidate_bundle(best_bundle, candidate_root)
    selected = dict(best.get("outcome") or {})
    candidate.update(selected)
    candidate["revision"] = best.get("revision")
    candidate["best_attempt"] = best.get("attempt")
    candidate["status"] = "quick_complete" if viable else "low_fidelity_rejected"
    candidate["evidence"] = (
        "best postmortem-qualified attempt selected"
        if viable
        else (best.get("postmortem") or {})
    )
    counts = state.setdefault("counts", {})
    counts["quick_evaluated"] = int(counts.get("quick_evaluated") or 0) + 1
    _update_hypothesis_adjudication(state, candidate)
    atomic_write_json(candidate_root / "candidate.json", candidate)


def _update_hypothesis_adjudication(
    state: dict[str, Any], candidate: dict[str, Any]
) -> None:
    hypothesis_id = str(candidate.get("hypothesis_id") or "")
    if not hypothesis_id or candidate.get("status") == "quick_complete":
        return
    related = [
        item
        for item in state.get("candidates") or []
        if item.get("hypothesis_id") == hypothesis_id
    ]
    exhausted = sum(item.get("status") == "low_fidelity_rejected" for item in related)
    failures: dict[str, int] = {}
    for item in related:
        for attempt in item.get("attempts") or []:
            code = str((attempt.get("postmortem") or {}).get("primary_failure") or "")
            if code:
                failures[code] = failures.get(code, 0) + 1
    repeated = max(failures.values(), default=0)
    if exhausted < 2 and repeated < 4:
        return
    design_state = state.setdefault("design", {})
    falsified = design_state.setdefault("falsified_hypotheses", [])
    if hypothesis_id not in falsified:
        falsified.append(hypothesis_id)


def _copy_attempt_bundle(source: Path, destination: Path) -> None:
    copy_job_bundle(source, destination)
    search_space = source / "search_space.json"
    if search_space.is_file():
        shutil.copy2(search_space, destination / "search_space.json")


def _replace_candidate_bundle(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.restore-{uuid.uuid4().hex}")
    backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
    _copy_attempt_bundle(source, temporary)
    try:
        os.replace(destination, backup)
        os.replace(temporary, destination)
    except Exception:
        if backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)


def _parameter_tuning_preview(
    subject: dict[str, Any],
    train: PreparedExecutionDataset,
    params: dict[str, Any],
    search_space: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any] | None:
    """Rank parameter slots cheaply without touching validation data."""
    trials = int(policy.get("inner_optuna_preview_trials") or 0)
    if trials <= 0:
        return None
    bars = max(int(policy.get("inner_optuna_preview_bars") or 0), 1)
    timeout = float(policy.get("inner_optuna_preview_timeout_seconds") or 0) or None
    grid, preview = _run_evolution_optuna(
        subject,
        train,
        {**params, **search_space},
        trials=trials,
        bars=bars,
        timeout=timeout,
    )
    dimensions = _typed_search_dimensions(search_space)
    if grid.ranked:
        best = grid.ranked[0]
        preview.update(
            {
                "objective": _objective(best["stats"], best["params"]),
                "selected_params": {
                    name: best["params"][name]
                    for name in dimensions
                    if name in best["params"]
                },
            }
        )
    del grid
    gc.collect()
    return preview


def _run_evolution_optuna(
    subject: dict[str, Any],
    dataset: PreparedExecutionDataset,
    search_space: dict[str, Any],
    *,
    trials: int,
    bars: int,
    timeout: float | None,
) -> tuple[ExecutionGridResult, dict[str, Any]]:
    search_data = _tail(dataset, bars) if bars > 0 else dataset
    started = perf_counter()
    grid = run_optuna_search(
        subject["script"],
        search_data,
        subject["spec"],
        search_space,
        rank_by="net_return",
        n_trials=trials,
        seed=_OPTUNA_SEED,
        timeout=timeout,
        objectives=["net_return", "max_drawdown_pct"],
    )
    return grid, {
        "status": "complete" if grid.ranked else "no_valid_trials",
        "trials": len(grid.runs),
        "valid_trials": max(len(grid.runs) - len(grid.invalid), 0),
        "bars": len(search_data.bars.timestamps),
        "seed": _OPTUNA_SEED,
        "wall_seconds": round(perf_counter() - started, 3),
    }


def _typed_search_dimensions(search_space: dict[str, Any]) -> list[str]:
    return sorted(
        name
        for name, value in search_space.items()
        if isinstance(value, dict)
        and value.get("type") in {"float", "int", "categorical"}
    )


def finalize_campaign(store: JobStore, job_id: str) -> dict[str, Any]:
    """Resume bounded full-dev work without holding campaign state during sims."""
    # A dedicated ownership lock prevents two CLI/watchdog finalizers from
    # duplicating compute. It is intentionally NOT the campaign-state lock:
    # evaluators and watchdog state transitions remain free while sims run.
    with job_state_lock(
        store.repo_root, job_id, name="evolution_finalize_owner", timeout_s=1
    ):
        return _finalize_campaign(store, job_id)


def _finalize_campaign(store: JobStore, job_id: str) -> dict[str, Any]:
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = campaign_status(store, job_id)
        for candidate in state.get("candidates") or []:
            if candidate.get("status") == "repair_pending" and candidate.get(
                "attempts"
            ):
                _close_designed_candidate(
                    store,
                    job_id,
                    state=state,
                    candidate=candidate,
                )
        _save_campaign(store, job_id, state)
        pending = [
            str(candidate.get("candidate_id") or "")
            for candidate in state.get("candidates") or []
            if candidate.get("status")
            in {"prepared", "quick_failed", "quick_running", "repair_pending"}
        ]
    if pending:
        raise TransientInfrastructureError(
            "candidate evaluation must finish before finalization: "
            + ", ".join(candidate_id for candidate_id in pending if candidate_id)
        )
    while True:
        claimed = _claim_full_dev(store, job_id)
        if claimed is None:
            break
        campaign_id, claim_id, candidate, tune = claimed
        try:
            require_evolution_headroom()
            with experiment_compute_lock(
                store,
                job_id,
                label=f"evolution-full-dev:{job_id}:{candidate['candidate_id']}",
                completion_reserve=True,
            ):
                outcome = _isolated_full_dev(store, job_id, candidate, tune=tune)
        except (ComputeLockBusy, TransientInfrastructureError):
            _release_finalize_claim(
                store,
                job_id,
                campaign_id=campaign_id,
                candidate_id=str(candidate["candidate_id"]),
                claim_id=claim_id,
                claim_field="full_dev_claim_id",
                restored_status="quick_complete",
            )
            raise
        except Exception as exc:  # noqa: BLE001 - isolate candidate failures
            outcome = {
                "status": "invalid",
                "evidence": f"full development evaluation failed: {str(exc)[:300]}",
            }
        committed = _commit_full_dev(
            store,
            job_id,
            campaign_id=campaign_id,
            candidate_id=str(candidate["candidate_id"]),
            claim_id=claim_id,
            outcome=outcome,
        )
        if committed is None:
            continue
        candidate = committed
        _archive_campaign_candidate(store, job_id, candidate)
        gc.collect()

    while True:
        claimed_proposal = _claim_proposal(store, job_id)
        if claimed_proposal is None:
            break
        campaign_id, claim_id, candidate = claimed_proposal
        try:
            require_evolution_headroom()
            with experiment_compute_lock(
                store,
                job_id,
                label=f"evolution-proposal-gate:{job_id}:{candidate['candidate_id']}",
                completion_reserve=True,
            ):
                candidate_root = resolve_candidate_bundle(
                    store, job_id, candidate, campaign_id=campaign_id
                )
                economic = _isolated_economic_gate(
                    store, job_id, candidate, campaign_id=campaign_id
                )
                if economic.get("ready") is not True:
                    outcome = {
                        "status": "proposal_rejected",
                        "evidence": (
                            "; ".join(
                                str(reason) for reason in economic.get("reasons") or []
                            )
                            or "paired development evidence did not clear the economic gate"
                        ),
                        "economic": economic,
                    }
                else:
                    from wayfinder_paths.jobs.probation import (
                        stage_evolution_probation,
                    )

                    staged = stage_evolution_probation(
                        store,
                        job_id,
                        candidate_id=str(candidate["candidate_id"]),
                        candidate_root=candidate_root,
                        revision=str(candidate.get("revision") or ""),
                        source="evolution_campaign",
                        family=str(candidate.get("family") or ""),
                        summary=str(candidate.get("summary") or "") or None,
                        campaign_id=campaign_id,
                        evidence=economic,
                    )
                    status = str(staged.get("status") or "queued")
                    outcome = {
                        "status": (
                            "probation_deferred"
                            if status == "deferred"
                            else "probation"
                        ),
                        "proposal": staged,
                        "economic": economic,
                        "evidence": (
                            staged.get("reason")
                            or "staged for immutable burn-in and paired probation"
                        ),
                    }
        except (ComputeLockBusy, TransientInfrastructureError):
            _release_finalize_claim(
                store,
                job_id,
                campaign_id=campaign_id,
                candidate_id=str(candidate["candidate_id"]),
                claim_id=claim_id,
                claim_field="proposal_claim_id",
                restored_status="dev_frontier",
            )
            raise
        except Exception as exc:  # noqa: BLE001 - isolate candidate failures
            outcome = {
                "status": "proposal_rejected",
                "evidence": f"paper proposal staging failed: {str(exc)[:300]}",
            }
        committed = _commit_proposal(
            store,
            job_id,
            campaign_id=campaign_id,
            candidate_id=str(candidate["candidate_id"]),
            claim_id=claim_id,
            outcome=outcome,
        )
        if committed is None:
            continue
        candidate = committed
        set_candidate_status(
            store,
            job_id,
            str(candidate["candidate_id"]),
            str(candidate["status"]),
            evidence=str(candidate.get("evidence") or "paper proposal")[:300],
        )
        gc.collect()

    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = campaign_status(store, job_id)
        if state.get("status") not in {"active", "finalizing"}:
            return state
        policy = _campaign_policy(store, job_id, str(state["campaign_id"]))
        state.setdefault("budgets", _campaign_budgets(policy))
        state["status"] = "complete"
        state["stage"] = "complete"
        state["completed_at"] = utc_now_iso()
        _save_campaign(store, job_id, state)
    store.append_journal(
        job_id,
        {
            "type": "evolution_campaign_completed",
            "campaign_id": state["campaign_id"],
            "counts": state["counts"],
            "funnel": summarize_evolution_funnel(state),
            "probation_trials": sum(
                candidate.get("status") == "probation"
                for candidate in state.get("candidates") or []
            ),
        },
    )
    return state


def _campaign_budgets(policy: dict[str, Any]) -> dict[str, int]:
    budgets = {
        "generated": int(policy["generated_programs"]),
        "full_development": int(policy["full_dev_survivors"]),
        "optuna": int(policy["inner_optuna_finalists"]),
        "optuna_minimum": int(policy.get("inner_optuna_min_finalists") or 0),
        "finalist_gate": int(policy["proposal_finalists"]),
    }
    if policy.get("investigation_design_enabled") is True:
        budgets["quick_attempts"] = int(
            policy.get("max_quick_attempts")
            or int(policy["generated_programs"])
            * int(policy.get("max_attempts_per_idea") or 1)
        )
    return budgets


def _claim_full_dev(
    store: JobStore, job_id: str
) -> tuple[str, str, dict[str, Any], bool] | None:
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = campaign_status(store, job_id)
        if state.get("status") not in {"active", "finalizing"}:
            raise ValueError(f"job {job_id!r} has no open evolution campaign")
        campaign_id = str(state["campaign_id"])
        policy = _campaign_policy(store, job_id, campaign_id)
        remaining = max(
            0,
            int(policy["full_dev_survivors"]) - int(state["counts"]["full_dev"]),
        )
        eligible = [
            item
            for item in state["candidates"]
            if item.get("status") in {"quick_complete", "full_dev_running"}
        ]
        eligible.sort(key=_candidate_score, reverse=True)
        if not remaining or not eligible:
            return None
        tuning_limit = int(policy["inner_optuna_finalists"])
        tuning_allocated = sum(
            item.get("full_dev_tune") is True for item in state["candidates"]
        )
        parameter_tuning_allocated = sum(
            item.get("mutation_kind") == "parameter"
            and item.get("full_dev_tune") is True
            for item in state["candidates"]
        )
        typed_search = {
            str(item.get("candidate_id")): _candidate_has_typed_search_space(
                store, job_id, campaign_id, item
            )
            for item in eligible
        }
        selection = _select_full_dev_candidate(
            eligible,
            typed_search=typed_search,
            remaining=remaining,
            tuning_limit=tuning_limit,
            tuning_allocated=tuning_allocated,
            parameter_tuning_allocated=parameter_tuning_allocated,
            parameter_tuning_minimum=int(policy.get("inner_optuna_min_finalists") or 0),
        )
        if selection is None:
            return None
        candidate, tune, selection_reason = selection
        candidate["full_dev_tune"] = tune
        candidate["full_dev_selection_reason"] = selection_reason
        claim_id = uuid.uuid4().hex
        candidate.update(
            {
                "status": "full_dev_running",
                "full_dev_claim_id": claim_id,
                "full_dev_claimed_at": utc_now_iso(),
            }
        )
        state["status"] = "finalizing"
        state["stage"] = "full_dev"
        state.setdefault("finalize_started_at", utc_now_iso())
        _save_campaign(store, job_id, state)
        return campaign_id, claim_id, dict(candidate), tune


def _select_full_dev_candidate(
    eligible: list[dict[str, Any]],
    *,
    typed_search: dict[str, bool],
    remaining: int,
    tuning_limit: int,
    tuning_allocated: int,
    parameter_tuning_allocated: int,
    parameter_tuning_minimum: int,
) -> tuple[dict[str, Any], bool, str] | None:
    available = max(tuning_limit - tuning_allocated, 0)
    required_parameters = max(
        min(parameter_tuning_minimum, tuning_limit) - parameter_tuning_allocated,
        0,
    )
    parameter_searches = [
        item
        for item in eligible
        if item.get("mutation_kind") == "parameter"
        and typed_search.get(str(item.get("candidate_id")), False)
        and item.get("full_dev_tune") is not False
    ]
    if available and parameter_searches and remaining <= required_parameters:
        candidate = max(parameter_searches, key=_parameter_preview_score)
        reason = str(candidate.get("full_dev_selection_reason") or "reserved_parameter")
        return candidate, True, reason

    for candidate in eligible:
        existing_tune = candidate.get("full_dev_tune")
        if existing_tune is not None:
            if candidate.get("mutation_kind") == "parameter" and not existing_tune:
                continue
            return (
                candidate,
                bool(existing_tune),
                str(candidate.get("full_dev_selection_reason") or "retry"),
            )
        has_search = typed_search.get(str(candidate.get("candidate_id")), False)
        if candidate.get("mutation_kind") == "parameter":
            if not has_search or not available:
                continue
            return candidate, True, "ranked_parameter_search"
        tune = bool(has_search and available > required_parameters)
        reason = "ranked_structural_search" if tune else "ranked_structural"
        return candidate, tune, reason
    return None


def _parameter_preview_score(candidate: dict[str, Any]) -> float:
    preview = candidate.get("tuning_preview") or {}
    objective = preview.get("objective") if isinstance(preview, dict) else None
    if objective:
        return _candidate_score({"objective": objective})
    return _candidate_score(candidate)


def _candidate_has_typed_search_space(
    store: JobStore,
    job_id: str,
    campaign_id: str,
    candidate: dict[str, Any],
) -> bool:
    try:
        root = resolve_candidate_bundle(
            store, job_id, candidate, campaign_id=campaign_id
        )
        return _load_candidate_search_space(root, required=False) is not None
    except (OSError, ValueError):
        return False


def _isolated_full_dev(
    store: JobStore, job_id: str, candidate: dict[str, Any], *, tune: bool
) -> dict[str, Any]:
    return run_isolated_phase(
        _full_dev_child,
        store.repo_root,
        job_id,
        candidate,
        tune,
        timeout_s=float(
            os.environ.get("WAYFINDER_EVOLUTION_FULL_DEV_TIMEOUT_S", "5400")
        ),
    )


def _full_dev_child(
    repo_root: Path, job_id: str, candidate: dict[str, Any], tune: bool
) -> dict[str, Any]:
    store = JobStore(repo_root=repo_root)
    with evolution_resource_phase(
        store,
        job_id,
        phase="full_dev",
        candidate_id=str(candidate["candidate_id"]),
    ):
        return _full_dev(store, job_id, candidate, tune=tune)


def _commit_full_dev(
    store: JobStore,
    job_id: str,
    *,
    campaign_id: str,
    candidate_id: str,
    claim_id: str,
    outcome: dict[str, Any],
) -> dict[str, Any] | None:
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = campaign_status(store, job_id)
        if str(state.get("campaign_id") or "") != campaign_id:
            return None
        candidate = _candidate(state, candidate_id)
        if candidate.get("full_dev_claim_id") != claim_id:
            return None
        candidate.update(outcome)
        candidate.pop("full_dev_claim_id", None)
        candidate.pop("full_dev_claimed_at", None)
        candidate["full_dev_at"] = utc_now_iso()
        state["counts"]["full_dev"] += 1
        _save_campaign(store, job_id, state)
        return dict(candidate)


def _claim_proposal(
    store: JobStore, job_id: str
) -> tuple[str, str, dict[str, Any]] | None:
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = campaign_status(store, job_id)
        if state.get("status") not in {"active", "finalizing"}:
            raise ValueError(f"job {job_id!r} has no open evolution campaign")
        campaign_id = str(state["campaign_id"])
        policy = _campaign_policy(store, job_id, campaign_id)
        remaining = max(
            0,
            int(policy.get("proposal_finalists") or 1)
            - int(state["counts"].get("proposed") or 0),
        )
        eligible = [
            item
            for item in state["candidates"]
            if item.get("status") in {"dev_frontier", "proposal_running"}
        ]
        eligible.sort(key=_candidate_score, reverse=True)
        if not remaining or not eligible:
            return None
        candidate = eligible[0]
        claim_id = uuid.uuid4().hex
        candidate.update(
            {
                "status": "proposal_running",
                "proposal_claim_id": claim_id,
                "proposal_claimed_at": utc_now_iso(),
            }
        )
        state["stage"] = "probation"
        _save_campaign(store, job_id, state)
        return campaign_id, claim_id, dict(candidate)


def _commit_proposal(
    store: JobStore,
    job_id: str,
    *,
    campaign_id: str,
    candidate_id: str,
    claim_id: str,
    outcome: dict[str, Any],
) -> dict[str, Any] | None:
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = campaign_status(store, job_id)
        if str(state.get("campaign_id") or "") != campaign_id:
            return None
        candidate = _candidate(state, candidate_id)
        if candidate.get("proposal_claim_id") != claim_id:
            return None
        candidate.update(outcome)
        candidate.pop("proposal_claim_id", None)
        candidate.pop("proposal_claimed_at", None)
        candidate["proposed_at"] = utc_now_iso()
        state["counts"]["proposed"] = int(state["counts"].get("proposed") or 0) + 1
        _save_campaign(store, job_id, state)
        return dict(candidate)


def _isolated_economic_gate(
    store: JobStore,
    job_id: str,
    candidate: dict[str, Any],
    *,
    campaign_id: str,
) -> dict[str, Any]:
    return run_isolated_phase(
        _economic_gate_child,
        store.repo_root,
        job_id,
        candidate,
        campaign_id,
        timeout_s=float(os.environ.get("WAYFINDER_EVOLUTION_GATE_TIMEOUT_S", "3600")),
    )


def _economic_gate_child(
    repo_root: Path,
    job_id: str,
    candidate: dict[str, Any],
    campaign_id: str,
) -> dict[str, Any]:
    store = JobStore(repo_root=repo_root)
    candidate_root = resolve_candidate_bundle(
        store, job_id, candidate, campaign_id=campaign_id
    )
    with evolution_resource_phase(
        store,
        job_id,
        phase="economic_gate",
        candidate_id=str(candidate["candidate_id"]),
    ):
        return evaluate_economic_gate(
            job_id,
            candidate_dir=candidate_root,
            baseline_dir=(
                store.job_dir(job_id) / CAMPAIGN_ROOT / campaign_id / "source"
            ),
            probation=True,
            store=store,
            dataset_root=(
                store.job_dir(job_id) / CAMPAIGN_ROOT / campaign_id / CAMPAIGN_DATA_ROOT
            ),
        )


def _release_finalize_claim(
    store: JobStore,
    job_id: str,
    *,
    campaign_id: str,
    candidate_id: str,
    claim_id: str,
    claim_field: str,
    restored_status: str,
) -> None:
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = campaign_status(store, job_id)
        if str(state.get("campaign_id") or "") != campaign_id:
            return
        candidate = _candidate(state, candidate_id)
        if candidate.get(claim_field) != claim_id:
            return
        candidate["status"] = restored_status
        candidate.pop(claim_field, None)
        candidate.pop(claim_field.replace("_id", "ed_at"), None)
        _save_campaign(store, job_id, state)


def recover_lost_candidate_evaluations(
    store: JobStore,
    job_id: str,
    *,
    reason: str,
    now: datetime | None = None,
) -> list[str]:
    """Release evaluator claims after their detached owner disappears.

    Candidate bundles are durable, so a fresh stage can safely relaunch the
    low-fidelity evaluation. The watchdog calls this only after independently
    proving that no evaluator process is running.
    """
    recovered: list[str] = []
    recovered_at = _campaign_now(now).isoformat()
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = campaign_status(store, job_id)
        if state.get("status") not in {"active", "finalizing"}:
            return recovered
        for candidate in state.get("candidates") or []:
            if candidate.get("status") != "quick_running":
                continue
            candidate_id = str(candidate.get("candidate_id") or "")
            candidate.update(
                {
                    "status": "quick_failed",
                    "evaluation_recovered_at": recovered_at,
                    "evaluation_recovery_reason": reason[:300],
                }
            )
            candidate.pop("evaluation_claim_id", None)
            candidate.pop("evaluation_claimed_at", None)
            if candidate_id:
                recovered.append(candidate_id)
        if recovered:
            _save_campaign(store, job_id, state)
    for candidate_id in recovered:
        store.append_journal(
            job_id,
            {
                "type": "evolution_candidate_evaluation_recovered",
                "campaign_id": state.get("campaign_id"),
                "candidate_id": candidate_id,
                "reason": reason[:300],
            },
        )
    return recovered


def campaign_prompt_block(
    store: JobStore, job_id: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Bounded dynamic context; the full casebook is never reloaded into prompts."""
    try:
        state = maybe_start_campaign(store, job_id, now=now)
    except (FileNotFoundError, TransientInfrastructureError, ValueError) as exc:
        return {"status": "blocked", "reason": str(exc)}
    if not state or state.get("status") != "active":
        return None
    if not evolution_compute_window_open(store, job_id, now=now):
        return {
            "status": "blocked",
            "reason": "evolution worker paused during peak model pricing",
        }
    current = _campaign_now(now)
    deadline = _parse(state["deadline_at"])
    manifest = store.read_json(job_id, str(state["manifest"]), default={}) or {}
    candidates = state.get("candidates") or []
    running = [item for item in candidates if item.get("status") == "quick_running"]
    if running:
        return {
            "status": "blocked",
            "campaign_id": state["campaign_id"],
            "reason": (
                f"candidate {running[0].get('candidate_id')} evaluation is running"
            ),
        }
    if state.get("stage") == "design":
        if current >= deadline:
            return {
                "job_id": job_id,
                "campaign_id": state["campaign_id"],
                "stage": "design",
                "session_stage": "finalize",
                "artifact_key": "finalize",
                "agent_name": "wayfinder-evolution-worker",
                "deadline_at": state["deadline_at"],
                "counts": state["counts"],
                "next_action": (
                    'Call wayfinder_core_jobs with action="evolution_finalize", '
                    f'job_id="{job_id}", background=true, then end this stage. '
                    "The design deadline elapsed without candidate generation."
                ),
                "deadline_elapsed": True,
            }
        pack_path = (store.job_dir(job_id) / str(state["diagnostic_pack"])).resolve()
        manifest_path = (store.job_dir(job_id) / str(state["manifest"])).resolve()
        policy = manifest.get("policy") or {}
        diagnostic_pack = (
            store.read_json(job_id, str(state["diagnostic_pack"]), default={}) or {}
        )
        slots = int(policy.get("generated_programs") or 8)
        wildcards = int(policy.get("wildcard_slots") or 2)
        research_parent_required = _research_parent_available(manifest, diagnostic_pack)
        research_instruction = (
            "Include at least one research_seed or research_context slot. "
            if research_parent_required
            else "Do not allocate a decorative research_seed/research_context "
            "slot: this campaign has no executable research seed or prior "
            "research outcome. "
        )
        starter_ids = [
            str(item.get("starter_id"))
            for item in manifest.get("starter_seeds") or []
            if item.get("starter_id")
        ]
        research_seed_ids = [
            str(item.get("seed_id"))
            for item in manifest.get("research_seeds") or []
            if item.get("seed_id")
        ]
        return {
            "job_id": job_id,
            "campaign_id": state["campaign_id"],
            "stage": "design",
            "session_stage": "design",
            "artifact_key": "design",
            "agent_name": "wayfinder-evolution-designer",
            "deadline_at": state["deadline_at"],
            "counts": state["counts"],
            "next_action": (
                f"Read `{pack_path}` and `{manifest_path}`. Design 3-5 grounded "
                f"causal hypotheses and exactly {slots} idea slots, including "
                f"exactly {wildcards} explicit wildcards. Grounded slots must "
                "cite existing JSON pointers from the diagnostic pack. Include "
                "at least one starter_seed and one grounded de_novo slot. "
                f"{research_instruction}Use at most one incumbent slot and at "
                "most two parameter slots. One wildcard must be de_novo. "
                'Call wayfinder_core_jobs with action="evolution_design", '
                f'job_id="{job_id}", and campaign_design={{"hypotheses": [...], '
                '"slots": [...]}}, background=true. Each hypothesis needs id, family, '
                "causal_mechanism, falsifier, evidence_refs. Each slot needs "
                "slot_id, wildcard, hypothesis_id (null for wildcard), "
                "parent_source, mutation_kind, family, summary. parent_source "
                "must be exactly one of incumbent, qd_elite, crossover, de_novo, "
                "starter_seed, research_seed, research_context; it is an enum, "
                "so do not append a starter id or other qualifier. mutation_kind "
                "must be exactly structural or parameter. For a starter_seed "
                "slot, set "
                "optional starter_seed_id to one of "
                f"{starter_ids}; this structured id, not summary prose, selects "
                "the executable seed. For a research_seed slot, set optional "
                f"research_seed_id to one of {research_seed_ids}. Do not wait "
                "for the detached result; end immediately after launch."
            ),
            "diagnostic_pack": str(pack_path),
            "manifest_path": str(manifest_path),
            "valid_evidence_pointers": valid_evidence_pointers(diagnostic_pack),
            "constraints": {
                "paper_only": True,
                "facts_constrain_claims_not_mechanisms": True,
                "wildcards": wildcards,
                "idea_slots": slots,
                "research_parent_required": research_parent_required,
            },
            "deadline_elapsed": current >= deadline,
        }
    awaiting_evaluation = [
        item
        for item in candidates
        if item.get("status") in {"prepared", "quick_failed", "repair_pending"}
    ]
    policy = manifest.get("policy") or {}
    budget = int(policy.get("generated_programs") or 0)
    deadline_elapsed = current >= deadline
    draining = deadline - CAMPAIGN_DRAIN <= current < deadline
    designed = str(state.get("schema_version") or "") == SCHEMA_VERSION
    design = (
        store.read_json(job_id, str(state.get("campaign_design") or ""), default={})
        or {}
        if designed
        else {}
    )
    try:
        requested_next_source = str(design["slots"][len(candidates)]["parent_source"])
    except (KeyError, IndexError, TypeError):
        requested_next_source = _parent_source(
            len(candidates) + 1, policy.get("parent_mix") or {}
        )
    next_parent_plan = _select_parent_plan(
        manifest,
        requested_source=requested_next_source,
        slot=len(candidates) + 1,
        candidates=candidates,
    )
    research_instruction = _research_context_instruction(
        manifest.get("research_context") or {}
    )
    prepare_call = (
        'Call wayfinder_core_jobs with action="evolution_prepare", '
        f'job_id="{job_id}". The accepted campaign design supplies the family, '
        "hypothesis, source, and mutation kind."
        if designed
        else (
            'Call wayfinder_core_jobs with action="evolution_prepare", '
            f'job_id="{job_id}", family="<specific strategy family>", and '
            'summary="<concise testable hypothesis>". Do not pass mutation_kind; '
            "the campaign policy assigns structural and parameter slots."
        )
    )
    artifact_key = "finalize"
    work_inputs: list[str] = []
    editable_paths: list[str] = []
    candidate_id: str | None = None
    postmortem_path: str | None = None
    if awaiting_evaluation:
        candidate = awaiting_evaluation[0]
        candidate_root = resolve_candidate_bundle(
            store,
            job_id,
            candidate,
            campaign_id=str(state["campaign_id"]),
        )
        session_stage = f"candidate-{int(candidate.get('slot') or 0):02d}"
        artifact_key = (
            f"{session_stage}-attempt-"
            f"{int(candidate.get('attempt_count') or 0) + 1:02d}"
        )
        candidate_id = str(candidate["candidate_id"])
        work_inputs = [str(candidate_root / "candidate.json")]
        editable_paths = [str(candidate_root)]
        mutation_instruction = (
            f"This is a parameter candidate: {_PARAMETER_SEARCH_GUIDANCE} "
            if candidate.get("mutation_kind") == "parameter"
            else f"This is a structural candidate: {_STRUCTURAL_SEARCH_GUIDANCE} "
        )
        seed_instruction = _candidate_seed_instruction(
            store, job_id, str(state["campaign_id"]), candidate
        )
        repair_instruction = ""
        if candidate.get("status") == "repair_pending":
            latest_attempt = (candidate.get("attempts") or [])[-1]
            postmortem_path = str(
                store.job_dir(job_id) / str(latest_attempt.get("postmortem_path") or "")
            )
            work_inputs.append(postmortem_path)
            repair_instruction = (
                f"This is repair {int(candidate.get('attempt_count') or 0)} of "
                f"at most {int(policy.get('max_attempts_per_idea') or 3)}. Read "
                f"the deterministic postmortem at "
                f"`{store.job_dir(job_id) / str(latest_attempt.get('postmortem_path') or '')}`. "
                "Change the named causal mechanism in response to that evidence; "
                "do not rename the family or substitute a generic new idea. "
            )
        next_action = (
            f"{seed_instruction}{mutation_instruction}{repair_instruction}Edit only files inside "
            f"{candidate_root} "
            "(workspace, job.yaml, "
            "and optional search_space.json), then launch "
            'wayfinder_core_jobs with action="evolution_evaluate", '
            f'job_id="{job_id}", candidate_id="{candidate["candidate_id"]}", '
            "and background=true. Do not wait for the detached result. END THIS "
            "STAGE immediately after launch; do not prepare another candidate. "
            "A fresh bounded attempt session will receive the compact postmortem "
            "if a repair is warranted."
        )
    elif deadline_elapsed:
        session_stage = "finalize"
        artifact_key = "finalize"
        next_action = (
            "Generation deadline elapsed. Call wayfinder_core_jobs with "
            f'action="evolution_finalize", job_id="{job_id}", background=true, '
            "then END THIS STAGE. Finalization is detached and deterministic."
        )
    elif draining:
        return {
            "status": "blocked",
            "campaign_id": state["campaign_id"],
            "reason": "evolution campaign is draining before finalization",
        }
    elif len(candidates) < budget:
        session_stage = f"candidate-{len(candidates) + 1:02d}"
        artifact_key = f"{session_stage}-attempt-01"
        preview = _parent_plan_handoff(next_parent_plan)
        next_action = (
            f"Next source plan: {json.dumps(preview, sort_keys=True)}. "
            f"{research_instruction} {prepare_call} Then follow the returned "
            "design assignment and edit only the exact `bundle_path` returned by "
            "that call and "
            'launch wayfinder_core_jobs with action="evolution_evaluate", '
            f'job_id="{job_id}", candidate_id="<returned candidate_id>", '
            "background=true. Do not wait for the detached result. END THIS "
            "STAGE immediately after launch; do not prepare another candidate. "
            "If the returned mutation_kind is parameter, "
            f"{_PARAMETER_SEARCH_GUIDANCE} If it is structural, "
            f"{_STRUCTURAL_SEARCH_GUIDANCE}"
        )
    else:
        session_stage = "finalize"
        artifact_key = "finalize"
        next_action = (
            'Call wayfinder_core_jobs with action="evolution_finalize", '
            f'job_id="{job_id}", background=true, then END THIS STAGE. '
            "Finalization is detached and deterministic."
        )
    return {
        "job_id": job_id,
        "campaign_id": state["campaign_id"],
        "stage": state["stage"],
        "session_stage": session_stage,
        "artifact_key": artifact_key,
        "deadline_at": state["deadline_at"],
        "counts": state["counts"],
        "next_action": next_action,
        "agent_name": "wayfinder-evolution-worker",
        "candidate_id": candidate_id,
        "work_inputs": work_inputs,
        "editable_paths": editable_paths,
        "postmortem_path": postmortem_path,
        "candidate_outcomes": [_candidate_handoff(item) for item in candidates],
        "historical_lessons": manifest.get("historical_lessons") or {},
        "research_context": manifest.get("research_context") or {},
        "next_parent_plan": _parent_plan_handoff(next_parent_plan),
        "cases": manifest.get("casebook") or [],
        "forward_context_cutoff": state.get("forward_context_cutoff"),
        "constraints": {
            "paper_only": True,
            "live_requires_owner": True,
            "candidate_inputs_frozen_at_campaign_start": True,
            "finalist_requires_24h_operational_burn_in": True,
            "starter_seed_evidence_resets": True,
            "refuted_family_matching": "exact_free_form_family_v1",
        },
        "deadline_elapsed": deadline_elapsed,
    }


def _parent_plan_handoff(plan: dict[str, Any]) -> dict[str, Any]:
    starter = plan.get("starter") or {}
    return {
        "source": plan.get("source"),
        "parent_candidate_ids": [
            item.get("candidate_id") for item in plan.get("parents") or []
        ],
        "starter_seed_id": starter.get("starter_id"),
        "starter_family": starter.get("family"),
        "starter_timeframe": starter.get("timeframe"),
        "starter_warmup_bars": starter.get("warmup_bars"),
        "evidence_reset": bool(starter),
    }


def _research_context_instruction(context: dict[str, Any]) -> str:
    refuted = [
        str(item.get("family") or "")
        for item in context.get("refuted_families") or []
        if item.get("family")
    ][:8]
    positives = [
        str(item.get("id") or "")
        for item in context.get("validated_positives") or []
        if item.get("id")
    ][:8]
    return (
        "Research context is frozen for this campaign. Do not re-propose an "
        f"exact refuted family without naming new evidence (refuted={refuted}); "
        f"seed from validated wins when relevant (validated={positives}). A win "
        "is named new evidence, not deletion of a refutation."
    )


def _candidate_seed_instruction(
    store: JobStore, job_id: str, campaign_id: str, candidate: dict[str, Any]
) -> str:
    source = str(candidate.get("parent_source") or "")
    if source == "starter_seed":
        return (
            f"The bundle contains audited starter `{candidate.get('starter_seed_id')}` "
            "as an adaptation seed. Its own warmup/lookback is already set, but "
            "its historical evidence was reset: adapt the tactic to this job's "
            "target universe and make it re-earn every gate. "
        )
    if source == "research_seed":
        return (
            f"The bundle contains sensor-authored research seed "
            f"`{candidate.get('research_seed_id')}`. Treat its hypothesis as a "
            "starting point only: its prior evidence was reset and it must re-earn "
            "every campaign gate. Preserve the current job's operational contract."
        )
    if source == "research_context":
        return (
            "The bundle is a clean scaffold for the assigned checked-in research "
            "hypothesis. Implement the named mechanism; do not copy incumbent alpha. "
        )
    if source == "crossover":
        secondary = _resolve_frozen_parent_bundle(
            store,
            job_id,
            campaign_id,
            str(candidate.get("secondary_parent_bundle") or ""),
        )
        return (
            "The candidate is seeded from the stronger executable parent. "
            f"Use `{secondary}` as READ-ONLY secondary parent context; edit only "
            "the candidate bundle and implement an explicit recombination. "
        )
    if source == "qd_elite":
        return "The bundle is an executable QD elite, not an incumbent clone. "
    if source == "de_novo":
        return (
            "The bundle is a clean target-compatible scaffold with no incumbent "
            "alpha or research memory; author a genuinely new causal strategy. "
        )
    return "The bundle is the frozen incumbent baseline. "


def _candidate_handoff(candidate: dict[str, Any]) -> dict[str, Any]:
    """Bounded durable context passed between isolated authoring stages."""
    handoff = {
        "candidate_id": candidate.get("candidate_id"),
        "slot": candidate.get("slot"),
        "family": candidate.get("family"),
        "mutation_kind": candidate.get("mutation_kind"),
        "parent_source": candidate.get("parent_source"),
        "requested_parent_source": candidate.get("requested_parent_source"),
        "parent_candidate_ids": candidate.get("parent_candidate_ids") or [],
        "starter_seed_id": candidate.get("starter_seed_id"),
        "research_seed_id": candidate.get("research_seed_id"),
        "status": candidate.get("status"),
        "summary": str(candidate.get("summary") or "")[:240],
        "design_slot_id": candidate.get("design_slot_id"),
        "hypothesis_id": candidate.get("hypothesis_id"),
        "wildcard": bool(candidate.get("wildcard")),
        "attempt_count": int(candidate.get("attempt_count") or 0),
        "best_attempt": candidate.get("best_attempt"),
    }
    for key in ("objective", "behavior"):
        value = candidate.get(key)
        if isinstance(value, dict):
            handoff[key] = value
    quick = candidate.get("quick")
    quick_stats = quick.get("stats") if isinstance(quick, dict) else None
    if isinstance(quick_stats, dict):
        handoff["quick_stats"] = {
            key: quick_stats.get(key)
            for key in (
                "net_return",
                "cagr",
                "sharpe",
                "sortino",
                "max_drawdown_pct",
                "trade_count",
                "win_rate",
                "profit_factor",
            )
            if quick_stats.get(key) is not None
        }
    evidence = candidate.get("evidence")
    if evidence:
        handoff["evidence"] = _compact_evidence(evidence, limit=240)
    recovery_reason = candidate.get("evaluation_recovery_reason")
    if recovery_reason:
        handoff["evaluation_recovery_reason"] = str(recovery_reason)[:240]
    attempts = candidate.get("attempts") or []
    if attempts:
        latest = attempts[-1]
        handoff["latest_postmortem"] = {
            "attempt": latest.get("attempt"),
            "path": latest.get("postmortem_path"),
            **compact_postmortem(latest.get("postmortem") or {}),
        }
    return handoff


def _sync_campaign_archive(store: JobStore, job_id: str, state: dict[str, Any]) -> None:
    """Reconcile a terminal campaign before its state file is replaced."""
    for candidate in state.get("candidates") or []:
        _archive_campaign_candidate(store, job_id, candidate)


def _archive_campaign_candidate(
    store: JobStore, job_id: str, candidate: dict[str, Any]
) -> None:
    status = str(candidate.get("status") or "generated")
    archive_status = status if status in ARCHIVE_STATUSES else "generated"
    executable_bundle: str | None = None
    revision = str(candidate.get("revision") or "")
    if status == "dev_frontier" and revision:
        source = resolve_candidate_bundle(store, job_id, candidate)
        executable_bundle, _ = _persist_executable_bundle(
            store,
            job_id,
            candidate_id=str(candidate["candidate_id"]),
            revision=revision,
            source=source,
        )
    metadata = {
        key: candidate[key]
        for key in (
            "campaign_id",
            "bundle",
            "mutation_kind",
            "parent_source",
            "requested_parent_source",
            "starter_seed_id",
            "research_seed_id",
            "seed_revision",
            "evidence_reset",
            "quick",
            "dev",
            "tuning_eligible",
            "tuning_skip_reason",
            "tuning_preview",
            "full_dev_tune",
            "full_dev_selection_reason",
            "tuning",
            "elite_eligible",
            "elite_activity",
            "design_slot_id",
            "hypothesis_id",
            "wildcard",
            "evidence_refs",
            "attempt_count",
            "best_attempt",
            "latest_postmortem_path",
        )
        if candidate.get(key) is not None
    }
    if executable_bundle is not None:
        metadata["executable_bundle"] = executable_bundle
    attempts = candidate.get("attempts") or []
    if attempts:
        metadata["latest_postmortem"] = compact_postmortem(
            attempts[-1].get("postmortem") or {}
        )
    record_candidate(
        store,
        job_id,
        candidate_id=str(candidate["candidate_id"]),
        family=str(candidate.get("family") or "unknown"),
        summary=str(candidate.get("summary") or "evolution candidate"),
        status=archive_status,
        objective=candidate.get("objective"),
        revision=candidate.get("revision"),
        parent_candidate_ids=list(candidate.get("parent_candidate_ids") or []),
        behavior=candidate.get("behavior"),
        evidence=_compact_evidence(candidate.get("evidence") or archive_status),
        metadata=metadata,
    )


def _compact_evidence(value: Any, *, limit: int = 300) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)[:limit]
    return str(value)[:limit]


def _load_candidate_search_space(
    candidate_root: Path, *, required: bool
) -> dict[str, Any] | None:
    search_path = candidate_root / "search_space.json"
    if not search_path.exists():
        if required:
            raise ValueError(
                "parameter candidate requires search_space.json with typed "
                "Optuna dimensions"
            )
        return None
    try:
        payload = json.loads(search_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"candidate search space is unreadable: {search_path}"
        ) from exc
    if not isinstance(payload, dict) or not is_search_space(payload):
        raise ValueError(f"candidate search space is not typed: {search_path}")
    dimensions = len(_typed_search_dimensions(payload))
    if dimensions > _MAX_SEARCH_DIMENSIONS:
        raise ValueError(
            "candidate search space exceeds the three-dimension evolution budget"
        )
    return payload


def _full_dev(
    store: JobStore, job_id: str, candidate: dict[str, Any], *, tune: bool
) -> dict[str, Any]:
    campaign_id = str(
        candidate.get("campaign_id")
        or str(candidate["candidate_id"]).rsplit("-c", 1)[0]
    )
    root = resolve_candidate_bundle(store, job_id, candidate, campaign_id=campaign_id)
    subject = _load_subject(store, job_id, root, campaign_id=campaign_id)
    train_end, validation_end = _split_bounds(store, job_id, campaign_id=campaign_id)
    train, validation, _ = _split_dataset(
        subject["dataset"], train_end=train_end, validation_end=validation_end
    )
    params, stress_params, calibration = _calibrated_params(store, job_id, subject)
    _require_declared_window(subject, params)
    tuning: dict[str, Any] | None = None
    candidate_search = _load_candidate_search_space(
        root, required=candidate.get("mutation_kind") == "parameter"
    )
    if tune and candidate_search is not None:
        search = {
            **params,
            **candidate_search,
        }
        policy = _campaign_policy(store, job_id, campaign_id)
        search_bars = int(policy.get("inner_optuna_train_bars") or 0)
        search_timeout = float(policy.get("inner_optuna_timeout_seconds") or 0) or None
        grid, tuning = _run_evolution_optuna(
            subject,
            train,
            search,
            trials=min(int(policy["inner_optuna_trials"]), 20),
            bars=search_bars,
            timeout=search_timeout,
        )
        if grid.ranked:
            params = dict(grid.ranked[0]["params"])
            params["slippage_bps"] = max(
                float(params.get("slippage_bps") or 0.0),
                float(calibration["p50_bps"]),
            )
            stress_params = {
                **params,
                "slippage_bps": max(
                    float(params.get("slippage_bps") or 0.0),
                    float(calibration["p90_bps"]),
                ),
            }
            job_data = _load_job_yaml(root)
            job_data["execution_params"] = params
            atomic_write_text(
                root / "job.yaml", yaml.safe_dump(job_data, sort_keys=False)
            )
        tuning["selected_params"] = {
            name: params[name]
            for name in _typed_search_dimensions(candidate_search)
            if name in params
        }
        del grid
        gc.collect()
    # Post-tune probe: optuna may have moved the window params, so prove the
    # FINAL configuration is window-invariant before spending dev backtests.
    probe = window_invariance_probe(
        subject["script"], train.bars, subject["spec"], params
    )
    if probe["status"] == "failed":
        raise ValueError(
            f"window-invariance probe failed at bar {probe['bar']}: decide() "
            "consumed history beyond its declared window of "
            f"{probe['window']} bars — {BOUNDED_WINDOW_HINT}"
        )
    revision = compute_workspace_revision(root)
    manifest = (
        store.read_json(
            job_id, f"{CAMPAIGN_ROOT}/{campaign_id}/manifest.json", default={}
        )
        or {}
    )
    if revision == _source_baseline_revision(manifest, candidate):
        raise ValueError("candidate has no effective mutation after tuning")
    train_result, train_stats = _window_result(subject, 0.0, train_end, params)
    train_valid = bool(train_result.validation.get("execution_valid"))
    compact_train = _compact_result(train_result, stats=train_stats)
    del train_result
    gc.collect()
    validation_result, validation_stats = _window_result(
        subject, train_end, validation_end, params
    )
    validation_valid = bool(validation_result.validation.get("execution_valid"))
    objective = _objective(validation_stats, params)
    behavior = _behavior(
        validation_result,
        validation,
        subject["spec"],
        stats=validation_stats,
        start_at=validation.bars.timestamps[0],
    )
    compact_validation = _compact_result(validation_result, stats=validation_stats)
    del validation_result
    gc.collect()
    stress_result, stress_stats = _window_result(
        subject, train_end, validation_end, stress_params
    )
    stress_valid = bool(stress_result.validation.get("execution_valid"))
    compact_stress = _compact_result(stress_result, stats=stress_stats)
    del stress_result
    gc.collect()
    minimum_validation_trades = (
        int((manifest.get("policy") or {}).get("elite_min_validation_trades") or 8)
        if str(manifest.get("schema_version") or "") == SCHEMA_VERSION
        else 1
    )
    passed = (
        train_valid
        and validation_valid
        and stress_valid
        and int(validation_stats.get("trade_count") or 0) >= minimum_validation_trades
        and float(validation_stats.get("net_return") or 0.0) > 0.0
        and float(stress_stats.get("net_return") or 0.0) > 0.0
        and bool(calibration["audit_passed"])
    )
    return {
        "status": "dev_frontier" if passed else "low_fidelity_rejected",
        "revision": revision,
        "params": params,
        "tuning": tuning,
        "execution_calibration": calibration,
        "dev": {
            "train": compact_train,
            "validation": compact_validation,
            "validation_stress": compact_stress,
        },
        "objective": objective,
        "behavior": behavior,
        "elite_eligible": passed,
        "elite_activity": {
            "validation_trades": int(validation_stats.get("trade_count") or 0),
            "minimum": minimum_validation_trades,
            "target": int(
                (manifest.get("policy") or {}).get("elite_participation_target_trades")
                or 12
            ),
        },
        "evidence": "positive independent validation with sufficient activity"
        if passed
        else (
            "failed independent validation: activity below elite floor"
            if int(validation_stats.get("trade_count") or 0) < minimum_validation_trades
            else "failed independent validation"
        ),
    }


def _load_subject(
    store: JobStore,
    job_id: str,
    root: Path,
    *,
    campaign_id: str | None = None,
    dataset_root: Path | None = None,
) -> dict[str, Any]:
    job_data = _load_job_yaml(root)
    spec_data, _ = resolve_execution_spec(root, job_data)
    if not spec_data:
        raise FileNotFoundError("candidate execution_spec missing")
    spec = ExecutionSpec.from_dict(spec_data)
    script = store.resolve_script_entrypoint(
        job_id,
        job_data,
        candidate_dir=root if root != store.job_dir(job_id) else None,
    )
    if script is None or not script.exists():
        raise FileNotFoundError("candidate execution script missing")
    resolved_dataset_root = dataset_root or (
        store.job_dir(job_id) / CAMPAIGN_ROOT / campaign_id / CAMPAIGN_DATA_ROOT
        if campaign_id
        else root
    )
    dataset = _load_dataset(
        resolved_dataset_root,
        spec,
        job_data,
        feature_roots=(resolved_dataset_root,),
    )
    return {
        "root": root,
        "campaign_id": campaign_id,
        "job_data": job_data,
        "spec": spec,
        "script": script,
        "dataset": dataset,
        "params": dict(job_data.get("execution_params") or {}),
    }


def _split_dataset(
    dataset: PreparedExecutionDataset,
    *,
    train_end: float = 0.70,
    validation_end: float = 0.85,
) -> tuple[
    PreparedExecutionDataset, PreparedExecutionDataset, PreparedExecutionDataset
]:
    timestamps = dataset.bars.timestamps
    if len(timestamps) < 30:
        raise ValueError("evolution needs at least 30 timestamps for its data splits")
    train_index = max(1, int(len(timestamps) * train_end))
    validation_index = max(train_index + 1, int(len(timestamps) * validation_end))
    validation_index = min(validation_index, len(timestamps) - 1)
    return (
        _slice(dataset, timestamps, 0, train_index),
        _slice(dataset, timestamps, train_index, validation_index),
        _slice(dataset, timestamps, validation_index, len(timestamps)),
    )


def _tail(dataset: PreparedExecutionDataset, bars: int) -> PreparedExecutionDataset:
    timestamps = dataset.bars.timestamps
    return _slice(dataset, timestamps, max(0, len(timestamps) - bars), len(timestamps))


def _calibrated_params(
    store: JobStore,
    job_id: str,
    subject: dict[str, Any],
    *,
    base: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    params = dict(base or subject["params"])
    symbols = {
        str(symbol)
        for symbol in (
            params.get("symbols") or subject["spec"].data_contract.get("symbols") or []
        )
    }
    campaign_id = subject.get("campaign_id")
    profile = (
        store.read_json(
            job_id,
            f"{CAMPAIGN_ROOT}/{campaign_id}/{FORWARD_SNAPSHOT}",
            default={},
        )
        if campaign_id
        else store.read_json(job_id, CALIBRATION_PATH, default={})
    ) or {}
    assumptions = execution_cost_assumptions(profile, symbols=symbols)
    configured = float(params.get("slippage_bps") or 0.0)
    params["slippage_bps"] = max(configured, float(assumptions["p50_bps"]))
    stress = {
        **params,
        "slippage_bps": max(configured, float(assumptions["p90_bps"])),
    }
    return params, stress, assumptions


def _window_result(
    subject: dict[str, Any],
    start_fraction: float,
    end_fraction: float,
    params: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    dataset: PreparedExecutionDataset = subject["dataset"]
    timestamps = dataset.bars.timestamps
    start = int(len(timestamps) * start_fraction)
    end = (
        len(timestamps) if end_fraction >= 1.0 else int(len(timestamps) * end_fraction)
    )
    end = max(start + 1, min(end, len(timestamps)))
    warmup = _strategy_warmup_bars(subject["script"], params)
    evaluation = _slice(dataset, timestamps, max(0, start - warmup), end)
    result = simulate_execution(subject["script"], evaluation, subject["spec"], params)
    stats = (
        result.stats
        if start == 0
        else _test_window_stats(result, timestamps[start], subject["spec"], params)
    )
    return result, stats


def _compact_result(
    result: Any, *, stats: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "stats": stats if stats is not None else result.stats,
        "validation": result.validation,
        "profile": result.profile,
    }


def _objective(stats: dict[str, Any], params: dict[str, Any]) -> dict[str, float]:
    net = max(float(stats.get("net_return") or 0.0), -0.999999)
    capital = float(params.get("initial_capital") or 10_000.0)
    worst = min(float(stats.get("worst_trade_pnl") or 0.0), 0.0)
    return {
        "net_log_growth": round(math.log1p(net), 8),
        "downside_deviation": round(abs(float(stats.get("avg_drawdown") or 0.0)), 8),
        "tail_loss": round(abs(worst) / max(capital, 1.0), 8),
        "max_drawdown_pct": round(abs(float(stats.get("max_drawdown_pct") or 0.0)), 8),
    }


def _behavior(
    result: Any,
    dataset: PreparedExecutionDataset,
    spec: ExecutionSpec,
    *,
    stats: dict[str, Any] | None = None,
    start_at: Any | None = None,
) -> dict[str, Any]:
    entries = [
        row
        for row in result.trades
        if not row.get("reduce_only")
        and (
            start_at is None
            or row.get("timestamp") is None
            or _parse(str(row["timestamp"])) >= _parse(str(start_at))
        )
    ]
    signed = [
        1 if str(row.get("side") or "").lower() in {"buy", "long"} else -1
        for row in entries
    ]
    direction = sum(signed) / len(signed) if signed else 0.0
    interval = bar_interval_seconds(spec.data_contract.get("bar_interval")) or 1
    hold = float((stats or result.stats).get("avg_trade_duration_s") or 0.0) / interval
    timestamps = dataset.bars.timestamps
    days = (
        max((timestamps[-1] - timestamps[0]).total_seconds() / 86_400, 1.0)
        if timestamps
        else 1.0
    )
    assets = max(len(dataset.bars.symbols), 1)
    density = len(entries) / assets / days * 30
    return {
        "direction_bias": round(direction, 4),
        "average_hold_bars": round(hold, 2),
        "trades_per_asset_30d": round(density, 2),
    }


def _candidate_score(candidate: dict[str, Any]) -> float:
    objective = candidate.get("objective") or {}
    return (
        float(objective.get("net_log_growth") or 0.0)
        - float(objective.get("downside_deviation") or 0.0)
        - float(objective.get("tail_loss") or 0.0)
        - float(objective.get("max_drawdown_pct") or 0.0)
    )


def _candidate_validation_passed(report: dict[str, Any]) -> bool:
    """Ignore only deployment artifacts that an isolated bundle cannot own."""
    research_only = {
        "declared_features_available",
        "preflight_report_present",
        "preflight_passed",
        "wallet_label_declared",
    }
    return not any(
        not check.get("passed") and check.get("name") not in research_only
        for check in report.get("checks") or []
    )


def _freeze_parent_pool(
    store: JobStore, job_id: str, campaign_root: Path
) -> dict[str, Any]:
    """Freeze executable QD/frontier parents for the campaign.

    Legacy campaign bundles are lazily copied into the stable parent root
    only when their archived revision still matches their bytes.
    """
    archive = load_archive(store, job_id).get("candidates") or []
    qd_ids = list(
        dict.fromkeys(
            str(item["candidate_id"])
            for entries in quality_diversity_snapshot(store, job_id).values()
            for item in entries
        )
    )
    selected_ids = set(qd_ids)
    selected_ids.update(
        str(item.get("candidate_id") or "")
        for item in archive
        if (
            item.get("status") == "incumbent"
            or (
                (
                    item.get("on_frontier")
                    or item.get("status") in _EXECUTABLE_PARENT_STATUSES
                )
                and elite_activity_eligible(item)
            )
        )
    )
    frozen: list[dict[str, Any]] = []
    for entry in archive:
        # Backfill all candidates that reached full development, even if a
        # later proposal verdict made them ineligible for selection. This
        # preserves the executable receipt without reviving a rejected branch.
        metadata = entry.get("metadata") or {}
        reached_development = bool(
            entry.get("status") in _EXECUTABLE_PARENT_STATUSES or metadata.get("dev")
        )
        if not reached_development:
            continue
        stable = _ensure_executable_parent(store, job_id, entry)
        candidate_id = str(entry.get("candidate_id") or "")
        if stable is None or candidate_id not in selected_ids:
            continue
        destination = campaign_root / "parents" / candidate_id
        copy_job_bundle(stable, destination)
        frozen.append(
            {
                "candidate_id": candidate_id,
                "family": str(entry.get("family") or "unknown"),
                "summary": str(entry.get("summary") or "")[:160],
                "status": entry.get("status"),
                "revision": entry.get("revision"),
                "objective": entry.get("objective") or {},
                "behavior": entry.get("behavior") or {},
                "bundle": f"parents/{candidate_id}",
            }
        )
    frozen.sort(key=participation_score, reverse=True)
    frozen_ids = {str(item["candidate_id"]) for item in frozen}
    return {
        "candidates": frozen,
        "qd_elite_ids": [
            candidate_id for candidate_id in qd_ids if candidate_id in frozen_ids
        ],
        "frozen_at_campaign_start": True,
    }


def _ensure_executable_parent(
    store: JobStore, job_id: str, entry: dict[str, Any]
) -> Path | None:
    candidate_id = str(entry.get("candidate_id") or "")
    revision = str(entry.get("revision") or "")
    if not candidate_id or not revision:
        return None
    metadata = entry.get("metadata") or {}
    stable_relative = str(metadata.get("executable_bundle") or "")
    if stable_relative:
        try:
            stable = _resolve_stable_parent_bundle(
                store, job_id, candidate_id, revision, stable_relative
            )
            if compute_workspace_revision(stable) == revision:
                return stable
        except (OSError, ValueError):
            pass
    legacy_relative = str(metadata.get("bundle") or "")
    campaign_id = str(metadata.get("campaign_id") or "")
    if not legacy_relative or not campaign_id:
        return None
    try:
        legacy = resolve_candidate_bundle(
            store,
            job_id,
            {
                "candidate_id": candidate_id,
                "campaign_id": campaign_id,
                "bundle": legacy_relative,
            },
        )
        if compute_workspace_revision(legacy) != revision:
            return None
        stable_relative, stable = _persist_executable_bundle(
            store,
            job_id,
            candidate_id=candidate_id,
            revision=revision,
            source=legacy,
        )
    except (OSError, ValueError):
        return None
    record_candidate(
        store,
        job_id,
        candidate_id=candidate_id,
        family=str(entry.get("family") or "unknown"),
        summary=str(entry.get("summary") or "evolution candidate"),
        status=str(entry.get("status") or "generated"),
        objective=entry.get("objective"),
        revision=revision,
        parent_candidate_ids=list(entry.get("parent_candidate_ids") or []),
        behavior=entry.get("behavior"),
        evidence=entry.get("evidence"),
        metadata={"executable_bundle": stable_relative},
    )
    return stable


def _persist_executable_bundle(
    store: JobStore,
    job_id: str,
    *,
    candidate_id: str,
    revision: str,
    source: Path,
) -> tuple[str, Path]:
    _validate_parent_component(candidate_id, "candidate id")
    _validate_parent_component(revision, "candidate revision")
    if compute_workspace_revision(source) != revision:
        raise ValueError("source executable parent revision mismatch")
    relative = f"{PARENT_BUNDLE_ROOT}/{candidate_id}/{revision}"
    destination = store.job_dir(job_id) / relative
    if destination.exists():
        if compute_workspace_revision(destination) != revision:
            raise ValueError("stable executable parent revision mismatch")
    else:
        copy_job_bundle(source, destination)
    return relative, destination


def _resolve_stable_parent_bundle(
    store: JobStore,
    job_id: str,
    candidate_id: str,
    revision: str,
    relative: str,
) -> Path:
    _validate_parent_component(candidate_id, "candidate id")
    _validate_parent_component(revision, "candidate revision")
    expected = f"{PARENT_BUNDLE_ROOT}/{candidate_id}/{revision}"
    if relative != expected:
        raise ValueError("stable executable parent path does not match its lineage")
    root = store.job_dir(job_id).resolve()
    allowed = (root / PARENT_BUNDLE_ROOT).resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(allowed):
        raise ValueError("stable executable parent escapes its root")
    return resolved


def _validate_parent_component(value: str, label: str) -> None:
    if not value or value in {".", ".."} or Path(value).name != value:
        raise ValueError(f"invalid evolution parent {label}")


def _snapshot_starter_seeds(
    store: JobStore, job_id: str, campaign_root: Path
) -> list[dict[str, Any]]:
    job_data = _load_job_yaml(campaign_root / "source")
    target_params = dict(job_data.get("execution_params") or {})
    target_symbols = {str(symbol) for symbol in target_params.get("symbols") or []}
    spec_data, _ = resolve_execution_spec(campaign_root / "source", job_data)
    target_timeframe = str(
        ((spec_data or {}).get("data_contract") or {}).get("bar_interval") or ""
    )

    def relevance(definition: StarterDefinition) -> tuple[int, int, str]:
        overlap = len(target_symbols & set(definition.symbols))
        return (
            int(definition.timeframe == target_timeframe),
            overlap,
            definition.id,
        )

    snapshots: list[dict[str, Any]] = []
    for definition in sorted(STARTER_DEFINITIONS, key=relevance, reverse=True):
        module = importlib.import_module(definition.module)
        raw_source = getattr(module, "__file__", None)
        if not raw_source:
            continue
        source = Path(raw_source)
        relative = f"starters/{definition.id}/strategy.py"
        destination = campaign_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(destination, source.read_text(encoding="utf-8"))
        snapshots.append(
            {
                "starter_id": definition.id,
                "family": definition.family,
                "summary": definition.summary,
                "timeframe": definition.timeframe,
                "source_symbols": list(definition.symbols),
                "target_symbols": sorted(target_symbols),
                "params": {
                    **definition.configured_params(),
                    "symbols": list(definition.symbols),
                    "venue": "hyperliquid",
                },
                "warmup_bars": starter_warmup_bars(definition),
                "lookback_bars": starter_lookback_bars(definition),
                "source": relative,
                "source_sha256": _file_hash(destination),
                "adaptation_required": True,
                "research_evidence_reset": True,
            }
        )
    return snapshots


def _freeze_research_seeds(
    store: JobStore, job_id: str, campaign_root: Path
) -> list[dict[str, Any]]:
    state = store.read_json(job_id, RESEARCH_SEED_STATE_PATH, default={}) or {}
    frozen: list[dict[str, Any]] = []
    for seed in state.get("seeds") or []:
        if seed.get("status") != "pending":
            continue
        relative = str(seed.get("bundle") or "")
        source = (store.job_dir(job_id) / relative).resolve()
        allowed = (store.job_dir(job_id) / RESEARCH_SEED_ROOT).resolve()
        revision = str(seed.get("revision") or "")
        if (
            not source.is_relative_to(allowed)
            or compute_workspace_revision(source) != revision
        ):
            continue
        destination = campaign_root / "research_seeds" / str(seed["seed_id"])
        copy_job_bundle(source, destination)
        if compute_workspace_revision(destination) != revision:
            raise ValueError("campaign research-seed copy revision mismatch")
        frozen.append(
            {
                **seed,
                "bundle": f"research_seeds/{seed['seed_id']}",
                "evidence_reset": True,
            }
        )
    return frozen


def _freeze_research_context(store: JobStore, job_id: str) -> dict[str, Any]:
    """Two load-bearing lists, distilled from existing mechanical records."""
    archive = load_archive(store, job_id).get("candidates") or []
    refuted_by_family: dict[str, dict[str, Any]] = {}
    for entry in archive:
        if entry.get("status") != "refuted":
            continue
        family = str(entry.get("family") or "").strip()
        if family and family not in refuted_by_family:
            refuted_by_family[family] = {
                "family": family,
                "candidate_id": entry.get("candidate_id"),
                "evidence": str(entry.get("evidence") or "")[:240],
            }

    positives: list[dict[str, Any]] = []
    probation = store.read_json(job_id, "probation.json", default={}) or {}
    for leg in probation.get("legs") or []:
        if leg.get("status") == "graduated":
            positives.append(
                {
                    "source": "probation_graduate",
                    "id": leg.get("name"),
                    "symbol": leg.get("symbol"),
                    "evidence": str(
                        (leg.get("graduate") or {}).get("progress")
                        or leg.get("notes")
                        or (leg.get("graduate") or {}).get("criterion")
                        or ""
                    )[:240],
                    "recorded_at": leg.get("closed_at") or leg.get("updated_at"),
                }
            )
    for trial in probation.get("trials") or []:
        if trial.get("status") != "graduated":
            continue
        metrics = (trial.get("forward") or {}).get("metrics") or {}
        positives.append(
            {
                "source": "evolution_probation_graduate",
                "id": trial.get("candidate_id") or trial.get("trial_id"),
                "family": trial.get("family"),
                "evidence": (
                    f"paired_days={metrics.get('paired_days')}; "
                    f"lcb={metrics.get('lcb')}; "
                    f"net_delta="
                    f"{float(metrics.get('candidate_net_pnl') or 0.0) - float(metrics.get('reference_net_pnl') or 0.0):.4f}"
                )[:240],
                "recorded_at": trial.get("closed_at") or trial.get("updated_at"),
            }
        )
    verdicts = (
        store.read_json(job_id, "state/promotion_verdicts.json", default={}) or {}
    )
    for proposal_id, verdict in verdicts.items():
        try:
            strategy_effect = float(verdict.get("strategy_effect"))
        except (TypeError, ValueError):
            continue
        if verdict.get("verdict") == "beat" and strategy_effect > 0:
            positives.append(
                {
                    "source": "counterfactual_confirmed_treatment",
                    "id": proposal_id,
                    "strategy_effect": strategy_effect,
                    "recorded_at": verdict.get("recorded_at"),
                }
            )
    experiment = (
        store.read_json(job_id, "state/evolution_experiment.json", default={}) or {}
    )
    if (
        experiment.get("status") == "complete"
        and (experiment.get("verdict") or {}).get("verdict") == "accrete"
    ):
        champion = ((experiment.get("arms") or {}).get("evolution") or {}).get(
            "champion"
        ) or {}
        positives.append(
            {
                "source": "evolution_experiment_accrete",
                "id": champion.get("candidate_id"),
                "revision": champion.get("revision"),
                "recorded_at": experiment.get("completed_at"),
            }
        )
    positives.sort(key=lambda item: str(item.get("recorded_at") or ""), reverse=True)
    return {
        "refuted_families": list(refuted_by_family.values())[-20:],
        "validated_positives": positives[:20],
        "matching": "exact_free_form_family_v1",
        "accepted_limitation": (
            "near-duplicate free-form family names are not normalized; a validated "
            "positive is named new evidence, not deletion of a refutation"
        ),
        "frozen_at_campaign_start": True,
    }


def _select_parent_plan(
    manifest: dict[str, Any],
    *,
    requested_source: str,
    requested_starter_id: str | None = None,
    requested_research_seed_id: str | None = None,
    slot: int,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve a requested source to material that actually exists.

    Cold-start QD/crossover slots become distinct audited starter seeds; they
    are never incumbent copies carrying a misleading lineage label.
    """
    pool = (manifest.get("parent_pool") or {}).get("candidates") or []
    if requested_source == "research_seed":
        used = {
            str(item.get("research_seed_id") or "")
            for item in candidates
            if item.get("research_seed_id")
        }
        seeds = list(manifest.get("research_seeds") or [])
        seed = (
            next(
                (
                    item
                    for item in seeds
                    if str(item.get("seed_id") or "") == requested_research_seed_id
                ),
                None,
            )
            if requested_research_seed_id
            else next(
                (item for item in seeds if str(item.get("seed_id") or "") not in used),
                None,
            )
        )
        if seed is not None:
            return {
                "source": "research_seed",
                "parents": [],
                "research_seed": seed,
            }
        requested_source = "research_context"
    if requested_source == "starter_seed":
        used = {
            str(item.get("starter_seed_id") or "")
            for item in candidates
            if item.get("starter_seed_id")
        }
        starters = list(manifest.get("starter_seeds") or [])
        starter = (
            next(
                (
                    item
                    for item in starters
                    if str(item.get("starter_id") or "") == requested_starter_id
                ),
                None,
            )
            if requested_starter_id
            else next(
                (
                    item
                    for item in starters
                    if str(item.get("starter_id") or "") not in used
                ),
                None,
            )
        )
        if starter is not None:
            return {"source": "starter_seed", "parents": [], "starter": starter}
        return {"source": "de_novo", "parents": [], "fallback_from": "starter_seed"}
    if requested_source == "research_context":
        return {"source": "research_context", "parents": []}
    qd_ids = set((manifest.get("parent_pool") or {}).get("qd_elite_ids") or [])
    qd = [item for item in pool if item.get("candidate_id") in qd_ids]
    crossover_pool = [item for item in pool if item.get("status") != "incumbent"]
    if requested_source == "incumbent":
        return {"source": "incumbent", "parents": []}
    if requested_source == "qd_elite" and qd:
        parent = qd[(slot - 1) % len(qd)]
        return {"source": "qd_elite", "parents": [parent], "primary": parent}
    if requested_source == "crossover" and len(crossover_pool) >= 2:
        first = (slot - 1) % len(crossover_pool)
        pair = [
            crossover_pool[first],
            crossover_pool[(first + 1) % len(crossover_pool)],
        ]
        pair.sort(key=participation_score, reverse=True)
        return {
            "source": "crossover",
            "parents": pair,
            "primary": pair[0],
            "secondary": pair[1],
        }
    if requested_source in {"qd_elite", "crossover"}:
        used = {
            str(item.get("starter_seed_id") or "")
            for item in candidates
            if item.get("starter_seed_id")
        }
        starter = next(
            (
                item
                for item in manifest.get("starter_seeds") or []
                if str(item.get("starter_id") or "") not in used
            ),
            None,
        )
        if starter is not None:
            return {"source": "starter_seed", "parents": [], "starter": starter}
    return {"source": "de_novo", "parents": []}


def _materialize_candidate_seed(
    store: JobStore,
    job_id: str,
    *,
    campaign_id: str,
    candidate_root: Path,
    plan: dict[str, Any],
) -> int:
    campaign_root = store.job_dir(job_id) / CAMPAIGN_ROOT / campaign_id
    frozen_source = campaign_root / "source"
    source = str(plan["source"])
    if source == "incumbent":
        copy_job_bundle(frozen_source, candidate_root)
    elif source in {"qd_elite", "crossover"}:
        primary = plan.get("primary") or {}
        parent = _resolve_frozen_parent_bundle(
            store, job_id, campaign_id, str(primary.get("bundle") or "")
        )
        if compute_workspace_revision(parent) != str(primary.get("revision") or ""):
            raise ValueError("frozen executable parent revision mismatch")
        copy_job_bundle(parent, candidate_root)
        job_data = _load_job_yaml(candidate_root)
        params = dict(job_data.get("execution_params") or {})
        for key in _TARGET_EXECUTION_PARAM_KEYS:
            params.pop(key, None)
        params.update(_target_execution_params(frozen_source))
        job_data["execution_params"] = params
        atomic_write_text(
            candidate_root / "job.yaml", yaml.safe_dump(job_data, sort_keys=False)
        )
    elif source == "research_seed":
        seed = dict(plan.get("research_seed") or {})
        parent = _resolve_research_seed_source(
            store, job_id, campaign_id, str(seed.get("bundle") or "")
        )
        if compute_workspace_revision(parent) != str(seed.get("revision") or ""):
            raise ValueError("frozen research seed revision mismatch")
        copy_job_bundle(parent, candidate_root)
        job_data = _load_job_yaml(candidate_root)
        params = dict(job_data.get("execution_params") or {})
        for key in _TARGET_EXECUTION_PARAM_KEYS:
            params.pop(key, None)
        params.update(_target_execution_params(frozen_source))
        job_data["execution_params"] = params
        atomic_write_text(
            candidate_root / "job.yaml", yaml.safe_dump(job_data, sort_keys=False)
        )
        _consume_research_seed(
            store, job_id, str(seed.get("seed_id") or ""), campaign_id
        )
    elif source == "starter_seed":
        _copy_clean_scaffold(store, job_id, frozen_source, candidate_root)
        _install_starter_seed(
            store,
            job_id,
            campaign_id=campaign_id,
            candidate_root=candidate_root,
            starter=dict(plan.get("starter") or {}),
        )
    else:
        _copy_clean_scaffold(store, job_id, frozen_source, candidate_root)
    return _seed_bundle_window(store, job_id, candidate_root)


def _copy_clean_scaffold(
    store: JobStore,
    job_id: str,
    source_root: Path,
    destination: Path,
) -> None:
    """Target job shell without incumbent alpha code or research memory."""
    copy_job_bundle(source_root, destination)
    risk_path = destination / "workspace" / "risk_limits.json"
    risk_limits = risk_path.read_bytes() if risk_path.exists() else None
    shutil.rmtree(destination / "workspace")
    (destination / "workspace").mkdir()
    if risk_limits is not None:
        (destination / "workspace" / "risk_limits.json").write_bytes(risk_limits)
    job_data = _load_job_yaml(destination)
    job_data["execution_params"] = _target_execution_params(source_root)
    atomic_write_text(
        destination / "job.yaml", yaml.safe_dump(job_data, sort_keys=False)
    )
    script = store.resolve_script_entrypoint(
        job_id, job_data, candidate_dir=destination
    )
    if script is None or not script.resolve().is_relative_to(
        (destination / "workspace").resolve()
    ):
        raise ValueError("evolution scaffold entrypoint must live inside workspace")
    script.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        script,
        '"""Clean de-novo evolution scaffold."""\n\n'
        "from __future__ import annotations\n\n"
        "from typing import Any\n\n\n"
        "def decide(ctx: Any) -> list[Any]:\n"
        "    return []\n",
    )


def _install_starter_seed(
    store: JobStore,
    job_id: str,
    *,
    campaign_id: str,
    candidate_root: Path,
    starter: dict[str, Any],
) -> None:
    source = _resolve_starter_source(
        store, job_id, campaign_id, str(starter.get("source") or "")
    )
    job_data = _load_job_yaml(candidate_root)
    script = store.resolve_script_entrypoint(
        job_id, job_data, candidate_dir=candidate_root
    )
    if script is None:
        raise ValueError("starter seed candidate has no execution entrypoint")
    atomic_write_text(script, source.read_text(encoding="utf-8"))
    params = dict(starter.get("params") or {})
    params.update(_target_execution_params(candidate_root))
    params["warmup_bars"] = int(starter["warmup_bars"])
    params["lookback_bars"] = int(starter["lookback_bars"])
    params.pop("full_history", None)
    job_data["execution_params"] = params
    atomic_write_text(
        candidate_root / "job.yaml", yaml.safe_dump(job_data, sort_keys=False)
    )


def _target_execution_params(source_root: Path) -> dict[str, Any]:
    params = dict(_load_job_yaml(source_root).get("execution_params") or {})
    return {key: params[key] for key in _TARGET_EXECUTION_PARAM_KEYS if key in params}


def _resolve_frozen_parent_bundle(
    store: JobStore, job_id: str, campaign_id: str, relative: str
) -> Path:
    if not relative or Path(relative).is_absolute():
        raise ValueError("frozen parent bundle must be campaign-relative")
    campaign_root = (store.job_dir(job_id) / CAMPAIGN_ROOT / campaign_id).resolve()
    allowed = (campaign_root / "parents").resolve()
    resolved = (campaign_root / relative).resolve()
    if not resolved.is_relative_to(allowed) or resolved.parent != allowed:
        raise ValueError("frozen parent bundle escapes the campaign parent root")
    return resolved


def _resolve_starter_source(
    store: JobStore, job_id: str, campaign_id: str, relative: str
) -> Path:
    if not relative or Path(relative).is_absolute():
        raise ValueError("starter source must be campaign-relative")
    campaign_root = (store.job_dir(job_id) / CAMPAIGN_ROOT / campaign_id).resolve()
    allowed = (campaign_root / "starters").resolve()
    resolved = (campaign_root / relative).resolve()
    if not resolved.is_relative_to(allowed) or resolved.name != "strategy.py":
        raise ValueError("starter source escapes the campaign starter root")
    return resolved


def _resolve_research_seed_source(
    store: JobStore, job_id: str, campaign_id: str, relative: str
) -> Path:
    if not relative or Path(relative).is_absolute():
        raise ValueError("research seed source must be campaign-relative")
    campaign_root = (store.job_dir(job_id) / CAMPAIGN_ROOT / campaign_id).resolve()
    allowed = (campaign_root / "research_seeds").resolve()
    resolved = (campaign_root / relative).resolve()
    if not resolved.is_relative_to(allowed) or resolved.parent != allowed:
        raise ValueError("research seed source escapes its campaign root")
    return resolved


def _consume_research_seed(
    store: JobStore, job_id: str, seed_id: str, campaign_id: str
) -> None:
    with job_state_lock(store.repo_root, job_id, name="evolution_research_seeds"):
        state = store.read_json(job_id, RESEARCH_SEED_STATE_PATH, default={}) or {}
        for seed in state.get("seeds") or []:
            if seed.get("seed_id") == seed_id and seed.get("status") == "pending":
                seed.update(
                    {
                        "status": "consumed",
                        "consumed_by_campaign": campaign_id,
                        "consumed_at": utc_now_iso(),
                    }
                )
                store.write_json(job_id, RESEARCH_SEED_STATE_PATH, state)
                return


def _snapshot_campaign_inputs(
    active_root: Path,
    campaign_root: Path,
    *,
    dataset_path: Path,
    experience: dict[str, Any],
    development_fraction: float,
) -> dict[str, Any]:
    """Freeze every input known when candidate generation begins."""
    campaign_root.mkdir(parents=True, exist_ok=False)
    source_bundle = campaign_root / "source"
    copy_job_bundle(active_root, source_bundle)
    data_root = campaign_root / CAMPAIGN_DATA_ROOT
    bars_path = data_root / "results" / "backtest" / "input_bars.json"
    bars_path.parent.mkdir(parents=True, exist_ok=True)
    cutoff = _write_development_prefix(
        dataset_path,
        bars_path,
        fraction=development_fraction,
    )

    features: list[dict[str, Any]] = []
    job_data = _load_job_yaml(active_root)
    spec_data, _ = resolve_execution_spec(active_root, job_data)
    if spec_data:
        copied: set[Path] = set()
        for feature in parse_feature_specs(ExecutionSpec.from_dict(spec_data)):
            relative = Path(feature.path)
            if relative.is_absolute():
                raise ValueError("evolution features must live inside the job bundle")
            source = (active_root / relative).resolve()
            destination = (data_root / relative).resolve()
            if not source.is_relative_to(
                active_root.resolve()
            ) or not destination.is_relative_to(data_root.resolve()):
                raise ValueError("evolution feature path escapes the job bundle")
            if not source.exists():
                features.append(
                    {
                        "path": f"{CAMPAIGN_DATA_ROOT}/{relative.as_posix()}",
                        "missing": True,
                    }
                )
                continue
            if source in copied:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not _write_timeseries_prefix(source, destination, cutoff=cutoff):
                shutil.copy2(source, destination)
            copied.add(source)
            features.append(
                {
                    "path": f"{CAMPAIGN_DATA_ROOT}/{relative.as_posix()}",
                    "sha256": _file_hash(destination),
                    "bytes": destination.stat().st_size,
                }
            )

    forward_path = campaign_root / FORWARD_SNAPSHOT
    atomic_write_json(forward_path, experience)
    return {
        "dataset": {
            "path": f"{CAMPAIGN_DATA_ROOT}/results/backtest/input_bars.json",
            "sha256": _file_hash(bars_path),
            "bytes": bars_path.stat().st_size,
            "development_cutoff": cutoff.isoformat() if cutoff is not None else None,
        },
        "features": features,
        "forward_experience": {
            "path": FORWARD_SNAPSHOT,
            "sha256": _file_hash(forward_path),
            "bytes": forward_path.stat().st_size,
        },
        "source_bundle": {
            "path": "source",
            "revision": compute_workspace_revision(source_bundle),
        },
    }


def _write_development_prefix(
    source: Path, destination: Path, *, fraction: float
) -> pd.Timestamp | None:
    payload = json.loads(source.read_text(encoding="utf-8"))
    bars = payload.get("bars") if isinstance(payload, dict) else None
    if not isinstance(bars, list) or not bars:
        atomic_write_json(destination, payload)
        return None
    timestamp_values = {_row_timestamp(row) for row in bars if isinstance(row, dict)}
    timestamps = sorted(stamp for stamp in timestamp_values if stamp is not None)
    if not timestamps:
        atomic_write_json(destination, payload)
        return None
    count = max(1, min(len(timestamps), int(len(timestamps) * fraction)))
    cutoff = timestamps[count - 1]
    development = {
        **payload,
        "bars": [
            row
            for row in bars
            if isinstance(row, dict)
            and (stamp := _row_timestamp(row)) is not None
            and stamp <= cutoff
        ],
    }
    metadata = dict(development.get("metadata") or {})
    metadata["evolution_development_cutoff"] = cutoff.isoformat()
    development["metadata"] = metadata
    atomic_write_json(destination, development)
    return cutoff


def _write_timeseries_prefix(
    source: Path, destination: Path, *, cutoff: pd.Timestamp | None
) -> bool:
    if cutoff is None or source.suffix.lower() not in {".json", ".jsonl"}:
        return False
    if source.suffix.lower() == ".jsonl":
        jsonl_rows: list[dict[str, Any]] = []
        try:
            for line in source.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                row = json.loads(line)
                if (
                    isinstance(row, dict)
                    and (stamp := _row_timestamp(row)) is not None
                    and stamp <= cutoff
                ):
                    jsonl_rows.append(row)
        except (OSError, ValueError):
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            destination,
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in jsonl_rows),
        )
        return True
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    key = next(
        (
            name
            for name in ("bars", "rows", "data", "features")
            if isinstance(payload, dict) and isinstance(payload.get(name), list)
        ),
        None,
    )
    rows = payload.get(key) if key else payload if isinstance(payload, list) else None
    if not isinstance(rows, list):
        return False
    filtered = [
        row
        for row in rows
        if isinstance(row, dict)
        and (stamp := _row_timestamp(row)) is not None
        and stamp <= cutoff
    ]
    atomic_write_json(destination, {**payload, key: filtered} if key else filtered)
    return True


def _row_timestamp(row: dict[str, Any]) -> pd.Timestamp | None:
    raw = next(
        (row[key] for key in ("timestamp", "time", "t") if row.get(key) is not None),
        None,
    )
    if raw is None:
        return None
    try:
        stamp = (
            pd.Timestamp(raw, unit="ms")
            if isinstance(raw, (int, float))
            else pd.Timestamp(raw)
        )
    except (TypeError, ValueError):
        return None
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _campaign_manifest(
    store: JobStore, job_id: str, campaign_id: str
) -> dict[str, Any]:
    manifest = (
        store.read_json(
            job_id, f"{CAMPAIGN_ROOT}/{campaign_id}/manifest.json", default={}
        )
        or {}
    )
    if not isinstance(manifest, dict):
        raise ValueError(f"evolution campaign {campaign_id!r} has no manifest")
    return manifest


def _campaign_policy(store: JobStore, job_id: str, campaign_id: str) -> dict[str, Any]:
    manifest = _campaign_manifest(store, job_id, campaign_id)
    policy = manifest.get("policy")
    if not isinstance(policy, dict):
        raise ValueError(f"evolution campaign {campaign_id!r} has no policy")
    return policy


def _split_bounds(
    store: JobStore,
    job_id: str,
    *,
    campaign_id: str,
) -> tuple[float, float]:
    split = _campaign_policy(store, job_id, campaign_id).get("split") or {}
    train = float(split.get("train") or 0.0)
    validation = float(split.get("validation") or 0.0)
    total = train + validation
    if min(train, validation) <= 0 or not math.isclose(total, 1.0, abs_tol=1e-9):
        raise ValueError(
            "evolution train/validation split must be positive and sum to 1"
        )
    return train, 1.0


def _same_family_nonwins(state: dict[str, Any], family: str, streak: int) -> bool:
    outcomes = [
        item
        for item in reversed(state.get("candidates") or [])
        if item.get("family") == family
        and item.get("status")
        in {
            "invalid",
            "low_fidelity_rejected",
            "proposal_rejected",
            "proposal_deferred",
            "probation_deferred",
            "dev_frontier",
            "probation",
            "paper_proposal",
            "paper_experiment",
        }
    ]
    failures = {
        "invalid",
        "low_fidelity_rejected",
        "proposal_rejected",
        "proposal_deferred",
        "probation_deferred",
    }
    return len(outcomes) >= streak and all(
        item.get("status") in failures for item in outcomes[:streak]
    )


def _parent_source(slot: int, raw_mix: dict[str, Any]) -> str:
    """Weighted-deficit allocation makes the 30/30/20/20 mix auditable."""
    weights = {source: float(raw_mix.get(source) or 0.0) for source in _PARENT_SOURCES}
    scale = sum(weights.values()) or 1.0
    weights = {source: value / scale for source, value in weights.items()}
    assigned = dict.fromkeys(_PARENT_SOURCES, 0)
    selected = _PARENT_SOURCES[0]
    for turn in range(1, slot + 1):
        selected = max(
            _PARENT_SOURCES,
            key=lambda source: (
                turn * weights[source] - assigned[source],
                -_PARENT_SOURCES.index(source),
            ),
        )
        assigned[selected] += 1
    return selected


def _candidate(state: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    match = next(
        (
            item
            for item in state["candidates"]
            if item.get("candidate_id") == candidate_id
        ),
        None,
    )
    if match is None:
        raise ValueError(f"unknown campaign candidate {candidate_id!r}")
    return match


def resolve_candidate_bundle(
    store: JobStore,
    job_id: str,
    candidate: dict[str, Any],
    *,
    campaign_id: str | None = None,
) -> Path:
    """Resolve one campaign candidate without trusting mutable state paths."""
    relative = str(candidate.get("bundle") or "").strip()
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    resolved_campaign = str(campaign_id or candidate.get("campaign_id") or "").strip()
    if not relative or not candidate_id or not resolved_campaign:
        raise ValueError("candidate bundle, id, and campaign id are required")
    if Path(relative).is_absolute():
        raise ValueError("candidate bundle must be relative to the job root")
    root = store.job_dir(job_id).resolve()
    allowed = (root / CAMPAIGN_ROOT / resolved_campaign / "candidates").resolve()
    candidate_root = (root / relative).resolve()
    if (
        not candidate_root.is_relative_to(allowed)
        or candidate_root.parent != allowed
        or candidate_root.name != candidate_id
    ):
        raise ValueError("candidate bundle escapes its active campaign root")
    return candidate_root


def _active_campaign(store: JobStore, job_id: str) -> dict[str, Any]:
    state = campaign_status(store, job_id)
    if state.get("status") != "active":
        raise ValueError(f"job {job_id!r} has no active evolution campaign")
    return state


def _save_campaign(store: JobStore, job_id: str, state: dict[str, Any]) -> None:
    store.write_json(job_id, CAMPAIGN_STATE_PATH, state)


def _job_tags(store: JobStore, job_id: str) -> set[str]:
    job = store.load(job_id)
    text = f"{job.name} {job.goal} {job_id}".lower()
    return {
        tag
        for tag in ("momentum", "factor", "mean-reversion", "live", "execution")
        if tag in text
    } | {"validity", "costs"}


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse(value: Any) -> datetime:
    return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _campaign_now(value: datetime | None = None) -> datetime:
    if value is not None:
        return _aware(value)
    if os.getenv("WAYFINDER_BENCHMARK") == "1":
        frozen = str(os.getenv("WAYFINDER_BENCHMARK_NOW") or "").strip()
        if frozen:
            return _parse(frozen)
    return datetime.now(UTC)
