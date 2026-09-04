"""Isolated, budgeted open-ended strategy evolution campaigns.

The model remains the code mutation operator; this module owns everything
that must not be left to model discretion: cadence, immutable context,
lineage, stage budgets, causal paper proposals, archive accounting, and the
paper-only terminal state.  Candidate bundles never replace the active
workspace and no function in this module can authorize live trading.
"""

from __future__ import annotations

import ast
import gc
import hashlib
import importlib
import json
import math
import os
import re
import shutil
import statistics
import tempfile
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
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
from wayfinder_paths.jobs.bench.leaders import (
    LEADER_CLOSES_RELATIVE,
    load_leader_closes,
)
from wayfinder_paths.jobs.bundles import copy_job_bundle
from wayfinder_paths.jobs.compute_lock import (
    ComputeLockBusy,
    experiment_compute_lock,
    job_state_lock,
    machine_state_lock,
)
from wayfinder_paths.jobs.constitution import load_constitution
from wayfinder_paths.jobs.economics import (
    _chain_fold_equity,
    block_bootstrap_lcb,
    daily_log_returns,
    objective_vector,
    paired_daily_deltas,
)
from wayfinder_paths.jobs.evidence import verify_job_evidence_refs
from wayfinder_paths.jobs.evolution_diagnostics import (
    COST_HURDLE_MULTIPLE,
    attempt_made_progress,
    build_diagnostic_pack,
    build_postmortem,
    build_repair_work_order,
    compact_postmortem,
    fit_diagnostic_pack,
    leader_attribution_sentence,
    maker_round_trip_bps,
    preview_progress,
    receipt_economics,
    receipt_exits,
    resolve_json_pointer,
    result_receipt,
    valid_evidence_pointers,
)
from wayfinder_paths.jobs.evolution_funnel import summarize_evolution_funnel
from wayfinder_paths.jobs.execution.features import (
    DEFAULT_FEATURES_PATH,
    parse_feature_specs,
)
from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
from wayfinder_paths.jobs.execution.optimize import (
    is_search_space,
    normalize_search_space,
    run_optuna_search,
    search_space_probe_variants,
    untyped_search_keys,
)
from wayfinder_paths.jobs.execution.primitives import (
    DEFAULT_INITIAL_CAPITAL,
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
    sequence_preview,
    validate_execution_job,
    window_invariance_probe,
)
from wayfinder_paths.jobs.execution.walk_forward import _slice, _test_window_stats
from wayfinder_paths.jobs.execution_grid import GridCosts, passive_entry_grid
from wayfinder_paths.jobs.failures import TransientInfrastructureError, classify_failure
from wayfinder_paths.jobs.forward_experience import (
    CALIBRATION_PATH,
    build_forward_experience,
    execution_cost_assumptions,
)
from wayfinder_paths.jobs.gating import (
    clamp_size_scale,
    compute_workspace_revision,
    evaluate_economic_gate,
)
from wayfinder_paths.jobs.governance import record_evidence_access
from wayfinder_paths.jobs.improver.spec import ImproverSpec, revision_stamp
from wayfinder_paths.jobs.indicators import REGIME_LABELS, wilder_rsi
from wayfinder_paths.jobs.isolated_phase import run_isolated_phase
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.multiple_testing import haircut
from wayfinder_paths.jobs.policy_scan import policy_scan
from wayfinder_paths.jobs.regime import (
    LEADER_CODES,
    LEADER_FEATURE_NAME,
    LEADER_RALLY_RETURN,
    LEADER_RETURN_FEATURE_NAMES,
    LEADER_SELLOFF_RETURN,
    LEADER_SYMBOLS,
    MACRO_CODES,
    MACRO_FEATURE_NAME,
    MACRO_RETURN_FEATURE_NAMES,
    MIXED_REGIME,
    PORTFOLIO_REGIME_CLASSIFIER,
    classify_portfolio_regimes,
    declared_regimes,
    leader_feature_names,
    macro_label,
    opposite_regime,
    regime_universe,
)
from wayfinder_paths.jobs.research import (
    apply_bh_verdicts,
    library_signal_warmup_bars,
    rank_ic,
    resample_ohlcv,
    scan_signals,
)
from wayfinder_paths.jobs.resource_envelope import (
    evolution_resource_phase,
    require_evolution_headroom,
    require_evolution_launch_headroom,
)
from wayfinder_paths.jobs.robustness import _strategy_warmup_bars
from wayfinder_paths.jobs.signal_library import (
    SIGNAL_DSL,
    build_signal_frame,
    compile_signal_expression,
    signal_defs,
)
from wayfinder_paths.jobs.signal_population import population_defs
from wayfinder_paths.jobs.starter_casebook import select_starter_cases
from wayfinder_paths.jobs.starters import (
    STARTER_DEFINITIONS,
    STARTER_LOOKBACK_MARGIN_BARS,
    StarterDefinition,
    starter_lookback_bars,
    starter_warmup_bars,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.trade_forensics import (
    aggregate_trade_forensics,
    forensics_for_closed_trades,
)
from wayfinder_paths.jobs.workspace_signals import (
    WORKSPACE_SIGNAL_CAP,
    validate_workspace_signals,
)
from wayfinder_paths.runner.monitor_state import atomic_write_json, atomic_write_text

CAMPAIGN_STATE_PATH = "state/evolution_campaign.json"
CAMPAIGN_ROOT = "research/evolution/campaigns"
PARENT_BUNDLE_ROOT = "research/evolution/parents"
RESEARCH_SEED_ROOT = "research/evolution/research_seeds"
RESEARCH_SEED_STATE_PATH = "state/evolution_research_seeds.json"
CAMPAIGN_DATA_ROOT = "dataset"
PROTECTED_CAMPAIGN_ROOT = "evolution/campaigns"
FORWARD_SNAPSHOT = "forward_experience.json"
DIAGNOSTIC_PACK = "diagnostic_pack.json"
CAMPAIGN_DESIGN = "campaign_design.json"
SCHEMA_VERSION = "2.0"
COMPOSE_STAGE = "compose"
REDESIGN_STAGE = "redesign"
MECHANISM_GRID_MAX = 6
_DEFAULT_EXTRA_HORIZONS: dict[str, list[int]] = {"1h": [72, 168], "4h": [42, 84]}
_CROSS_SECTION_SECONDS = 3600
_CROSS_SECTION_HORIZON_HOURS = (24, 168)
_MECHANISM_REF_RE = re.compile(r"^/mechanism_grids/(\d+)/top/(\d+)(?:/|$)")
_PROPOSAL_NAME_RE = re.compile(r"^[a-z0-9_]{1,48}$")
_COMPOSE_DSL_NAMES = tuple(name for name in SIGNAL_DSL if name not in {"pd", "np"})
# Failure codes that are evidence against a hypothesis family.  Infrastructure
# errors, activity floors, and windows without the declared regime are not.
_REFUTING_FAILURE_CODES = frozenset(
    {
        "negative_after_costs",
        "negative_in_target_regime",
        "out_of_regime_loss_budget",
        "screen_inversion",
    }
)
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
    "search with one hand-picked value. Sweep knobs that change decisions "
    "inside the screen window (entry thresholds, offsets, sizing); a holding "
    "period or TTL longer than the window changes nothing and is rejected "
    "before simulation."
)
_STRUCTURAL_SEARCH_GUIDANCE = (
    "Make the named causal code change. If it introduces meaningful numeric "
    "behavior knobs, also create search_space.json with at most three bounded "
    "typed dimensions covering only those new knobs. Otherwise omit it; do not "
    "invent tuning axes for a boolean or parameterless change. Any age, "
    "cooldown or expiry must be measured with ctx.bar_ordinal / "
    "ctx.bars_since(stamp) or timestamps, never ctx.bar_index (the bounded "
    "view length, constant once warm; such a candidate is rejected before "
    "simulation)."
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
    certification_policy = _protected_fold_policy(campaign_policy)
    # Two campaigns' worth: a weekly loop must still see the week before last.
    historical_lessons = evolution_lessons_block(store, job_id, limit=16)
    cases = select_starter_cases(_job_tags(store, job_id))
    relative_root = f"{CAMPAIGN_ROOT}/{campaign_id}"
    campaign_root = root / relative_root
    protected_root = (
        _protected_campaign_dataset_root(store, job_id, campaign_id)
        if certification_policy["enabled"]
        else None
    )
    with experiment_compute_lock(store, job_id, label=f"evolution-snapshot:{job_id}"):
        snapshots = _snapshot_campaign_inputs(
            root,
            campaign_root,
            dataset_path=dataset_path,
            experience=experience,
            development_fraction=float(certification_policy["discovery_fraction"]),
            protected_data_root=protected_root,
            audit_days=int(
                load_constitution(root).get("evaluation", {}).get("audit_days") or 7
            ),
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
    starter_seeds = _snapshot_starter_seeds(
        store,
        job_id,
        campaign_root,
        dataset_symbols=tuple(snapshots["dataset"].get("symbols") or ()),
    )
    research_context = _freeze_research_context(store, job_id)
    research_ideation = _freeze_research_ideation(store, job_id)
    regime_context = _campaign_regime_context(
        campaign_root / str(snapshots["dataset"]["path"]),
        campaign_root / "source",
        enabled=bool(campaign_policy.get("regime_specialist_enabled")),
    )
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
        "research_ideation": research_ideation,
        "regime_context": regime_context,
        "evaluation_plan": snapshots["evaluation_plan"],
        "policy": campaign_policy,
        **revision_stamp(root),
    }
    manifest_path = campaign_root / "manifest.json"
    atomic_write_json(manifest_path, manifest)
    # The design pack aggregates checked-in diagnostics only. Candidate
    # evaluation owns fresh simulations; campaign start must stay a cheap
    # control-plane operation rather than adding another incumbent backtest.
    baseline = _existing_baseline_receipt(
        root,
        window=snapshots["dataset"],
        params=_target_execution_params(campaign_root / "source"),
    )
    failure_modes = _incumbent_failure_modes(
        store, job_id, campaign_root, policy=campaign_policy
    )
    if failure_modes is not None:
        baseline["failure_modes"] = failure_modes
    baseline["complexity"] = _bundle_complexity(store, job_id, campaign_root / "source")
    try:
        with experiment_compute_lock(
            store, job_id, label=f"evolution-signal-scan:{job_id}"
        ):
            validated_signals = _validated_signals(
                store, job_id, campaign_root, policy=campaign_policy
            )
            policy_scan_block = _policy_scan_block(
                store, job_id, campaign_root, policy=campaign_policy
            )
    except ComputeLockBusy as exc:
        # Seeding never blocks a start; the designer reads why the feed is
        # empty and the next campaign scans again.
        busy = {"available": False, "reason": f"compute budget busy: {exc}"}
        validated_signals = (
            dict(busy)
            if bool(campaign_policy.get("signal_first_seeding", False))
            else None
        )
        policy_scan_block = (
            dict(busy)
            if bool(campaign_policy.get("policy_scan_enabled", True))
            else None
        )
    diagnostic_pack = build_diagnostic_pack(
        root,
        campaign_id=campaign_id,
        created_at=current.isoformat(),
        baseline=baseline,
        historical_lessons=historical_lessons,
        research_context=research_context,
        regime_context=regime_context,
        research_ideation=research_ideation,
        validated_signals=validated_signals,
        policy_scan=policy_scan_block,
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
        "stage": _initial_stage(campaign_schema, campaign_policy, validated_signals),
        "composition": _composition_state(campaign_policy),
        "started_at": current.isoformat(),
        "deadline_at": deadline.isoformat(),
        "manifest": f"{relative_root}/manifest.json",
        "diagnostic_pack": manifest["diagnostic_pack"]["path"],
        "campaign_design": f"{relative_root}/{CAMPAIGN_DESIGN}",
        "forward_context_cutoff": manifest["forward_context_cutoff"],
        "evaluation_plan": manifest["evaluation_plan"],
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
            "evaluation_plan": manifest["evaluation_plan"],
        },
    )
    return state


def _campaign_regime_context(
    dataset_path: Path, source_root: Path, *, enabled: bool
) -> dict[str, Any]:
    """Freeze the causal market cell that campaign design must diversify from,
    and the macro regime (the scale a designer means) whether or not the
    specialist cells are enabled."""
    macro: dict[str, Any] | None = None
    try:
        payload = json.loads(dataset_path.read_text(encoding="utf-8"))
        rows = payload.get("bars") if isinstance(payload, dict) else payload
        if isinstance(rows, list) and rows:
            macro = _macro_regime_context(pd.DataFrame(rows))
            latest = _store_latest_values(
                dataset_path.parents[2] / DEFAULT_FEATURES_PATH,
                {MACRO_FEATURE_NAME, *leader_feature_names()},
            )
            macro["runtime_feature"] = _macro_runtime_feature(
                available=MACRO_FEATURE_NAME in latest
            )
            macro["leaders"] = _leader_context(latest)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        macro = {"available": False, "reason": str(exc)[:240]}
    if not enabled:
        return {"enabled": False, "available": False, "macro": macro}
    try:
        payload = json.loads(dataset_path.read_text(encoding="utf-8"))
        rows = payload.get("bars") if isinstance(payload, dict) else None
        params = dict(_load_job_yaml(source_root).get("execution_params") or {})
        if not isinstance(rows, list) or not rows:
            raise ValueError("campaign dataset has no bars")
        frame = pd.DataFrame(rows)
        available_symbols = tuple(str(value) for value in frame["symbol"].unique())
        universe = regime_universe(params, available_symbols)
        labels = classify_portfolio_regimes(frame, universe=universe)
        usable = labels[labels != MIXED_REGIME]
        if usable.empty:
            raise ValueError("campaign dataset has no classifiable regime bars")
    except (KeyError, OSError, TypeError, ValueError) as exc:
        return {
            "enabled": True,
            "available": False,
            "reason": str(exc)[:240],
            "macro": macro,
        }
    recent_cutoff = usable.index[-1] - pd.Timedelta(days=7)
    recent = usable[usable.index >= recent_cutoff]
    counts = recent.value_counts().to_dict()
    primary = str(
        max(
            counts,
            key=lambda label: (int(counts[label]), label == str(recent.iloc[-1])),
        )
    )
    return {
        "enabled": True,
        "available": True,
        "classifier": PORTFOLIO_REGIME_CLASSIFIER,
        "universe": list(universe),
        "primary_regime": primary,
        "counter_regime": opposite_regime(primary),
        "recent_window_days": 7,
        "recent_counts": {str(key): int(value) for key, value in counts.items()},
        "as_of": pd.Timestamp(usable.index[-1]).isoformat(),
        "macro": macro,
    }


_COMPLEXITY_FLOOR_COMPARISONS = 24
_RISK_NORMALIZATION_FLOOR = 0.25
_RISK_CEILING_REASON_PREFIXES = ("OOS max drawdown ", "OOS tail loss ")
_SIZING_DIMENSIONS = frozenset(
    {
        "notional_fraction",
        "position_fraction",
        "risk_fraction",
        "leverage",
        "size_scale",
        "size",
    }
)
_COMPLEXITY_MULTIPLE = 1.5


def strategy_complexity(source: str) -> dict[str, int]:
    """Static size of a strategy: comparisons (gates), numeric literals, lines.

    Not a quality score — a budget.  Fewer degrees of freedom on a 35-day
    screen slice is the most reliable generalization lever there is.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {
            "comparisons": 0,
            "numeric_literals": 0,
            "lines": len(source.splitlines()),
        }
    comparisons = sum(isinstance(node, ast.Compare) for node in ast.walk(tree))
    literals = sum(
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
        for node in ast.walk(tree)
    )
    return {
        "comparisons": comparisons,
        "numeric_literals": literals,
        "lines": len(source.splitlines()),
    }


def _bundle_complexity(store: JobStore, job_id: str, root: Path) -> dict[str, int]:
    job_data = _load_job_yaml(root)
    script = store.resolve_script_entrypoint(job_id, job_data, candidate_dir=root)
    if script is None or not script.exists():
        return {"comparisons": 0, "numeric_literals": 0, "lines": 0}
    return strategy_complexity(script.read_text(encoding="utf-8", errors="replace"))


def _complexity_budget(
    policy: Mapping[str, Any],
    incumbent: Mapping[str, Any] | None,
    *,
    regime_branches: int = 1,
) -> int:
    """Comparisons a candidate may spend: a multiple of the incumbent's above
    a floor, per regime branch — a book that runs one mechanism in one macro
    regime and another elsewhere is two books, and the budget says so."""
    floor = int(
        policy.get("complexity_floor_comparisons") or _COMPLEXITY_FLOOR_COMPARISONS
    )
    multiple = float(policy.get("complexity_multiple") or _COMPLEXITY_MULTIPLE)
    base = int((incumbent or {}).get("comparisons") or 0)
    return max(floor, math.ceil(multiple * base)) * max(1, int(regime_branches))


def _regime_branches(store: JobStore, job_id: str, root: Path) -> int:
    """Two when the bundle declares the macro regime column and reads it in
    its script (a regime-conditioned book), else one."""
    job_data = _load_job_yaml(root)
    spec_data, _ = resolve_execution_spec(root, job_data)
    if not spec_data:
        return 1
    try:
        declared = {
            item.column_name
            for item in parse_feature_specs(ExecutionSpec.from_dict(spec_data))
        }
    except ValueError:
        return 1
    if MACRO_FEATURE_NAME not in declared:
        return 1
    script = store.resolve_script_entrypoint(job_id, job_data, candidate_dir=root)
    if script is None or not script.exists():
        return 1
    text = script.read_text(encoding="utf-8", errors="replace")
    reads = (f"feature('{MACRO_FEATURE_NAME}'", f'feature("{MACRO_FEATURE_NAME}"')
    return 2 if any(read in text for read in reads) else 1


def _signal_timeframes(bar_seconds: int) -> list[str]:
    """Base timeframe plus the coarser intraday frames it divides evenly."""
    out = [f"{int(bar_seconds)}s"]
    for candidate in ("15m", "1h", "4h"):
        seconds = bar_interval_seconds(candidate) or 0
        if seconds > bar_seconds and seconds % bar_seconds == 0:
            out.append(candidate)
    return out


_CONDITION_LABELS: dict[str, dict[float, str]] = {
    MACRO_FEATURE_NAME: {code: label for label, code in MACRO_CODES.items()},
    LEADER_FEATURE_NAME: {code: label for label, code in LEADER_CODES.items()},
}
_SCAN_BAR_COLUMNS = ["timestamp", "symbol", "open", "high", "low", "close", "volume"]
_NEAR_MISS_KEYS = (
    "symbol",
    "signal",
    "timeframe",
    "horizon",
    "direction",
    "scope",
    "regime",
    "t_stat",
    "t_net",
    "q_value",
    "gross_edge_bps",
    "execution_hint",
    "events",
    "t_stat_by_slice",
    "shortfall",
)


def _condition_features(
    policy: Mapping[str, Any], columns: Sequence[str]
) -> dict[str, dict[float, str]]:
    """The store columns the scan conditions on: policy names with a known
    code map that are present on the frame."""
    wanted = policy.get("signal_first_condition_features")
    names = (
        [str(name) for name in wanted]
        if isinstance(wanted, (list, tuple))
        else list(_CONDITION_LABELS)
    )
    return {
        name: _CONDITION_LABELS[name]
        for name in names
        if name in _CONDITION_LABELS and name in columns
    }


def _extra_horizons(policy: Mapping[str, Any]) -> dict[str, list[int]]:
    raw = policy.get("signal_first_extra_horizons")
    table = raw if isinstance(raw, Mapping) else _DEFAULT_EXTRA_HORIZONS
    out: dict[str, list[int]] = {}
    for timeframe, horizons in table.items():
        if not bar_interval_seconds(str(timeframe)):
            continue
        values = sorted({int(h) for h in (horizons or []) if int(h) > 0})
        if values:
            out[str(timeframe)] = values
    return out


def _ranking_columns(close: pd.Series, *, bars_per_day: int) -> dict[str, pd.Series]:
    """Cross-sectional rankings a rotation slot could trade: trailing
    strength, oversold-ness and stretch from the mean."""
    return {
        "ret_7d": close / close.shift(7 * bars_per_day) - 1.0,
        "rsi14": wilder_rsi(close),
        "dist_sma20": close / close.rolling(20).mean() - 1.0,
    }


def _cross_sectional_block(
    frames: Mapping[str, pd.DataFrame],
    *,
    bar_seconds: int,
    leaders: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Rank-IC of a few ranking columns across the panel (plus the frozen
    leaders when present) on the hour frame: does the ranking order relative
    forward returns? Material for a rotation slot, reported beside the
    per-symbol signals."""
    rule = max(int(bar_seconds), _CROSS_SECTION_SECONDS)
    bars_per_day = max(1, 86_400 // rule)
    panel: dict[str, pd.Series] = {}
    for symbol, frame in frames.items():
        bars = (
            resample_ohlcv(frame, rule, bar_seconds=int(bar_seconds))
            if rule != int(bar_seconds)
            else frame
        )
        panel[str(symbol)] = pd.Series(
            bars["close"].astype(float).to_numpy(),
            index=pd.to_datetime(bars["timestamp"], utc=True),
        )
    leader_names: list[str] = []
    if leaders is not None and not leaders.empty and panel:
        start = min(series.index.min() for series in panel.values())
        end = max(series.index.max() for series in panel.values())
        for name in leaders.columns:
            series = leaders[name].dropna()
            series = series[(series.index >= start) & (series.index <= end)]
            if len(series) and str(name) not in panel:
                panel[str(name)] = series.astype(float)
                leader_names.append(str(name))
    if len(panel) < 4:
        return {
            "available": False,
            "reason": f"rank IC needs at least 4 symbols; the panel has {len(panel)}",
        }
    ranked: dict[str, dict[str, pd.DataFrame]] = {}
    for symbol, close in panel.items():
        columns = _ranking_columns(close, bars_per_day=bars_per_day)
        for column, values in columns.items():
            ranked.setdefault(column, {})[symbol] = pd.DataFrame(
                {
                    "timestamp": close.index,
                    "close": close.to_numpy(),
                    column: values.to_numpy(),
                }
            )
    horizons = [max(1, hours * 3600 // rule) for hours in _CROSS_SECTION_HORIZON_HOURS]
    # Three rankings over two horizons is one family of six tests: the
    # per-horizon |t| >= 2 bar becomes the Bonferroni bar for six.
    tests = len(ranked) * len(horizons)
    threshold = statistics.NormalDist().inv_cdf(1.0 - 0.025 / max(1, tests))
    columns_out: list[dict[str, Any]] = []
    for column, by_symbol in ranked.items():
        result = rank_ic(by_symbol, column, horizons=horizons)
        rows = []
        for row in result.get("horizons") or []:
            edge = (
                bool(row.get("edge"))
                and abs(float(row.get("t_stat") or 0.0)) >= threshold
            )
            rows.append(
                {
                    **{
                        key: row.get(key)
                        for key in ("horizon", "n", "mean_ic", "t_stat")
                    },
                    "edge": edge,
                }
            )
        columns_out.append(
            {
                "column": column,
                "has_edge": any(row["edge"] for row in rows),
                "horizons": rows,
            }
        )
    return {
        "available": True,
        "timeframe": f"{rule}s",
        "symbols": sorted(panel),
        "leaders": leader_names,
        "horizons": horizons,
        "multiple_testing": {
            "tests": tests,
            "t_threshold": round(threshold, 3),
            "method": "bonferroni",
            "observations": "horizon-spaced (non-overlapping forward windows)",
        },
        "columns": columns_out,
    }


def _campaign_scan_frames(
    store: JobStore, job_id: str, campaign_root: Path, *, policy: Mapping[str, Any]
) -> dict[str, Any]:
    """Per-symbol bar frames for the campaign's signal scans: the full train
    split and each screen slice, OHLCV plus every store column (macro and
    leader labels, funding when present) so scans can condition on them and
    proposals can read them, beside the scan's cost and floor settings."""
    subject = _load_subject(
        store,
        job_id,
        campaign_root / "source",
        dataset_root=campaign_root / CAMPAIGN_DATA_ROOT,
        include_store_features=True,
    )
    train = _discovery_dataset(subject["dataset"], policy)
    params, _, _ = _calibrated_params(store, job_id, subject)
    bar_seconds = bar_interval_seconds(
        subject["spec"].data_contract.get("bar_interval")
    )
    if not bar_seconds:
        raise ValueError("execution spec requires a positive bar_interval")
    bar_seconds = int(bar_seconds)
    universe = regime_universe(params, subject["dataset"].bars.symbols)
    train_frame = train.bars.to_frame()
    # resample_ohlcv labels the coarser bars by symbol, so keep the column;
    # store columns ride along with last-value aggregation.
    extras = [c for c in train_frame.columns if c not in _SCAN_BAR_COLUMNS]
    columns = [*_SCAN_BAR_COLUMNS, *extras]

    def per_symbol(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for symbol in universe:
            rows = frame[frame["symbol"] == symbol][columns].reset_index(drop=True)
            if len(rows) >= 200:
                out[symbol] = rows
        return out

    stamps = train.bars.timestamps
    train_days = max((stamps[-1] - stamps[0]).total_seconds() / 86_400.0, 1e-9)
    slices = _policy_screen_slices(train, policy)
    fee_bps = float(params.get("fee_bps") or 5.0)
    slippage_bps = float(params.get("slippage_bps") or 3.5)
    timeframes = _signal_timeframes(bar_seconds)
    return {
        "symbols": list(universe),
        "train": per_symbol(train_frame),
        "slices": {
            label: per_symbol(dataset.bars.to_frame()) for label, dataset in slices
        },
        "train_days": train_days,
        "bar_seconds": bar_seconds,
        "timeframes": timeframes,
        "feature_columns": extras,
        "condition_features": _condition_features(policy, extras),
        "taker_round_trip_bps": round(2.0 * (fee_bps + slippage_bps), 2),
        "maker_round_trip_bps": maker_round_trip_bps(params),
        "scan_kwargs": {
            "bar_seconds": bar_seconds,
            "timeframes": timeframes,
            "holdout_fraction": 0.0,
            "min_events": int(policy.get("signal_scan_min_events") or 30),
            "fee_bps": fee_bps,
            "slippage_bps": slippage_bps,
            # Slow signals act past the default 24-bar ceiling: 3 and 7 days
            # on the hour frame, 7 and 14 days on the 4-hour frame.
            "required_horizons": _extra_horizons(policy),
        },
        "source_root": campaign_root / "source",
    }


SIGNAL_RECIPE_NOTES = (
    "Per entry, how_to_use is the one-call precompute "
    "(library_signal_on_bars on the entry's timeframe), the side, the "
    "fixed-horizon exit and the warmup to declare. Always inside the risk "
    "envelope: one position per symbol, a stop via add_stop_atr or an "
    "explicit invalidation level, size so a 3x typical adverse move costs "
    "< 5% of equity (the gate sizes to the ceiling; needing < 0.5x is a weak "
    "design). A 'gate:' clause means the signal fires only while "
    "ctx.view.feature(<name>, default=0.0) equals that code; declare "
    '{"name": <name>} under execution_spec.data_contract.features. An '
    "'entry: passive' clause means the move is smaller than the taker round "
    "trip: enter with a post-only resting limit (limit_price at an ATR offset "
    'beyond the signal close, time_in_force="ALO", expires_after_bars=1) and '
    "exit with a passive reduce-only take-profit, never at the close. A "
    "library: population entry is a composed def: its expression is DSL "
    "source over f and wayfinder_paths.jobs.signal_library.SIGNAL_DSL; build "
    "it in precompute() with compile_signal_expression (from "
    "wayfinder_paths.jobs.signal_library) and pass the def object to "
    "library_signal_on_bars."
)


# What the pack carries per offered signal: the citation, the statistics the
# designer reads, the cost decomposition and the recipe. Per-slice t's,
# fold counts, density and ranking scores stay in the campaign's memory, not
# in the pack (each pack entry is read by a model turn and costs budget).
_PACK_SIGNAL_KEYS = (
    "signal_id",
    "symbol",
    "signal",
    "family",
    "library",
    "timeframe",
    "horizon",
    "direction",
    "scope",
    "regime",
    "regime_source",
    "tier",
    "shortfall",
    "t_stat",
    "t_net",
    "t_net_maker",
    "q_value",
    "gross_edge_bps",
    "edge_net_maker_bps",
    "execution_hint",
    "events",
    "expression",
    "min_bars",
    "source",
    "warmup_bars_required",
    "how_to_use",
)


def _signal_row_key(row: Mapping[str, Any]) -> str:
    """The stable identity of an offered row: what it measures, where."""
    return "|".join(
        str(row.get(key) if row.get(key) is not None else "")
        for key in (
            "signal",
            "symbol",
            "timeframe",
            "horizon",
            "regime",
            "direction",
            "expression",
            "min_bars",
        )
    )


def _signal_id(row: Mapping[str, Any]) -> str:
    return hashlib.sha1(_signal_row_key(row).encode()).hexdigest()[:12]


def _definition_fingerprint(definition: Any) -> tuple[str, int | None]:
    """What a proposal name is bound to for the campaign: its expression and
    warmup. Accepts a SignalDef, a registry record or a bare expression."""
    if isinstance(definition, Mapping):
        min_bars = definition.get("min_bars")
        return (
            str(definition.get("expression") or ""),
            int(min_bars) if min_bars is not None else None,
        )
    if isinstance(definition, str):
        return (definition, None)
    return (str(getattr(definition, "expression", "") or ""), int(definition.min_bars))


def _locate_signal_pointer(block: Mapping[str, Any], signal_id: str) -> str | None:
    """The current positional pointer of an offered row, by identity; None
    when the budget has trimmed it."""
    for tier_key in ("signals", "replicated"):
        for position, row in enumerate(block.get(tier_key) or []):
            if str(row.get("signal_id") or _signal_id(row)) == str(signal_id):
                return f"/validated_signals/{tier_key}/{position}"
    return None


def _pack_signal_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    out = {key: entry[key] for key in _PACK_SIGNAL_KEYS if key in entry}
    # Positional pointers move when a later round prepends rows; the id does
    # not, and every citation and grid carries it.
    out["signal_id"] = str(entry.get("signal_id") or _signal_id(entry))
    return out


def _diverse_signal_rows(
    rows: Sequence[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """The strongest row per (symbol, signal, timeframe): on the bench's year
    the top ten replicated rows were four horizon and regime variants of one
    HYPE signal, and the designer should see ten signals, not one."""
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row["symbol"]), str(row["signal"]), str(row["timeframe"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _signal_recipe(
    row: Mapping[str, Any],
    *,
    bar_seconds: int,
    condition: Mapping[str, Mapping[float, str]],
) -> str:
    """One compact line per entry; the shared rules are SIGNAL_RECIPE_NOTES
    on the block (the pack has a byte budget and ten entries of prose
    tipped it into its fail-closed shape)."""
    signal = (
        f"compile_signal_expression(name={row['signal']!r}, "
        f"family={str(row.get('family') or 'workspace')!r}, description='', "
        f"min_bars={row.get('min_bars')}, expression=<expression>)"
        if row.get("library") in {"population", "workspace"}
        else repr(row["signal"])
    )
    text = (
        f"library_signal_on_bars(frame, {signal}, {row['timeframe']!r}, "
        f"bar_seconds={bar_seconds}); {row['direction']} on True; exit after "
        f"{row['horizon']} {row['timeframe']} bars; warmup_bars >= "
        f"{row['warmup_bars_required']}"
    )
    source = row.get("regime_source")
    if source:
        label = str(row.get("regime") or "").split("=", 1)[-1]
        code = next(
            (
                code
                for code, name in (condition.get(str(source)) or {}).items()
                if name == label
            ),
            None,
        )
        gate = f"== {code:.1f}" if code is not None else f"is {label!r}"
        text += f"; gate: {source} {gate} ({label})"
    if row.get("execution_hint") in {"passive_only", "mechanism_required"}:
        text += "; entry: passive (resting limit at an offset, passive take-profit)"
    return text


def _policy_scan_block(
    store: JobStore, job_id: str, campaign_root: Path, *, policy: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Portfolio policies swept on the train panel: the material a rotation,
    sleeve, rank or relay slot builds on, beside the families this panel
    falsified. Survivors carry the kernel and params that instantiate them."""
    if not bool(policy.get("policy_scan_enabled", True)):
        return None
    try:
        frames = _campaign_scan_frames(store, job_id, campaign_root, policy=policy)
        block = policy_scan(
            frames["train"],
            bar_seconds=int(frames["bar_seconds"]),
            cost_bps_per_side=float(frames["taker_round_trip_bps"]) / 2.0,
            limit=int(policy.get("policy_scan_limit") or 6),
        )
    except Exception as exc:  # noqa: BLE001 - a side panel never blocks a start
        return {"available": False, "reason": str(exc)[:240]}
    block["source"] = "train split of the campaign dataset, common history"
    return block


def _policy_scan_instruction(block: Mapping[str, Any]) -> str:
    if not block or not block.get("available"):
        return ""
    survivors = list(block.get("survivors") or [])
    falsified = list(block.get("falsified") or [])
    text = (
        f"Policy scan ({int(block.get('configs') or 0)} portfolio configurations "
        f"across {len(block.get('families') or [])} families on "
        f"{', '.join(block.get('symbols') or [])}, taker cost charged, ranked on "
        "the first part of the train panel and reported on the rest): "
    )
    if survivors:
        text += (
            "survivors "
            + "; ".join(
                f"[{index}] {row['family']} via "
                f"{str(row.get('kernel') or 'no kernel').rsplit('.', 1)[-1]} "
                f"(rank Sharpe {float(row['rank']['sharpe']):+.1f}, report Sharpe "
                f"{float(row['report']['sharpe']):+.1f} / "
                f"{float(row['report']['return']):+.1%}, "
                f"{int(row['full']['rebalances'])} rebalances"
                + (
                    "; "
                    + ", ".join(f"{k} {v:+.1%}" for k, v in row["by_regime"].items())
                    if row.get("by_regime")
                    else ""
                )
                + f"; cite /policy_scan/survivors/{index})"
                for index, row in enumerate(survivors)
            )
            + ". A survivor with a kernel is instantiated by re-exporting the "
            "kernel with its recipe params (/policy_scan/survivors/<i>/recipe; no "
            "new code) and counts as the evidence a grounded de_novo slot must "
            "cite. "
        )
    else:
        text += "no configuration was consistent on both windows. "
    if falsified:
        text += (
            "Falsified on this panel (the family's best in-sample row lost more "
            "than the slice bound out of sample): "
            + ", ".join(falsified)
            + " — do not spend attempts there. "
        )
    return text


def _validated_signals(
    store: JobStore, job_id: str, campaign_root: Path, *, policy: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Library signals with replicated edge on this dataset, in two tiers.

    The scan runs on the FULL train split (events decimated to horizon
    spacing, |t|>=2 against drift, sign agreement in 3 of 4 chronological
    folds), unconditionally and conditioned on the store's macro and leader
    labels. ``signals`` (validated) also clear the Benjamini-Hochberg q over
    the whole family, the t_net floor and a positive edge net of the taker
    round trip; ``replicated`` rows are fed with their cost decomposition
    because existence and monetization are different questions: the HYPE
    maker starters came from a 4 bps ten-minute move a taker book could not
    carry. Both tiers need the minimum event count and non-inferiority on
    the screen slices (base rows; a conditioned row gates itself).
    """
    if not bool(policy.get("signal_first_seeding", False)):
        return None
    try:
        frames = _campaign_scan_frames(store, job_id, campaign_root, policy=policy)
        # The mechanical population (windows, thresholds, cross-family pairs)
        # joins the same family as the library: the honest denominator.
        population = (
            population_defs(limit=int(policy.get("signal_population_limit") or 300))
            if bool(policy.get("signal_population_search", True))
            else ()
        )
        population_by_name = {spec.name: spec for spec in population}
        scan_kwargs = {
            **frames["scan_kwargs"],
            "condition_features": frames["condition_features"],
            "extra_signals": population,
        }
        full_rows: list[dict[str, Any]] = []
        for symbol, rows in frames["train"].items():
            result = scan_signals(rows, **scan_kwargs)
            for row in result.get("_all_rows") or []:
                if row["signal"] in population_by_name:
                    row["library"] = "population"
                full_rows.append({**row, "symbol": symbol})
        max_q = float(policy.get("signal_first_max_q") or 0.20)
        apply_bh_verdicts(full_rows, q_threshold=max_q, min_folds_agree=3)
        per_slice: dict[str, dict[tuple[str, str, str, int], dict[str, Any]]] = {}
        for label, by_symbol in frames["slices"].items():
            table: dict[tuple[str, str, str, int], dict[str, Any]] = {}
            for symbol, rows in by_symbol.items():
                result = scan_signals(
                    rows,
                    **{
                        **frames["scan_kwargs"],
                        "min_events": 10,
                        "extra_signals": population,
                    },
                )
                for row in result.get("_all_rows") or []:
                    key = (
                        symbol,
                        str(row["signal"]),
                        str(row["timeframe"]),
                        int(row["horizon"]),
                    )
                    table[key] = row
            per_slice[label] = table
        selected, replicated, near, funnel = _select_validated_rows(
            full_rows,
            per_slice,
            days=frames["train_days"],
            min_events=int(policy.get("signal_first_min_events") or 40),
            max_q=max_q,
            slice_min_t=float(policy.get("signal_first_slice_min_t") or 1.0),
            min_t_net=float(policy.get("signal_first_min_t_net") or 2.0),
            taker_round_trip_bps=frames["taker_round_trip_bps"],
            maker_round_trip_bps=frames["maker_round_trip_bps"],
        )
        funnel["population_tests"] = sum(
            row.get("library") == "population" for row in full_rows
        )
        breakdown = _signal_breakdown([*selected, *replicated])
        leader_source = next(
            (
                root
                for root in (frames["source_root"], store.job_dir(job_id))
                if (root / LEADER_CLOSES_RELATIVE).exists()
            ),
            None,
        )
        loaded = load_leader_closes(leader_source) if leader_source else None
        try:
            cross_sectional = _cross_sectional_block(
                frames["train"],
                bar_seconds=int(frames["bar_seconds"]),
                leaders=loaded[0] if loaded else None,
            )
        except Exception as exc:  # noqa: BLE001 - a side panel never blocks seeding
            cross_sectional = {"available": False, "reason": str(exc)[:200]}
        funnel["population_survivors"] = sum(
            row.get("library") == "population" for row in [*selected, *replicated]
        )
        bar_seconds = int(frames["bar_seconds"])
        for row in [*selected, *replicated]:
            spec = population_by_name.get(str(row["signal"]))
            if spec is not None:
                row["expression"] = spec.expression
                row["min_bars"] = spec.min_bars
            row["warmup_bars_required"] = library_signal_warmup_bars(
                spec or row["signal"], row["timeframe"], bar_seconds=bar_seconds
            )
            row["how_to_use"] = _signal_recipe(
                row, bar_seconds=bar_seconds, condition=frames["condition_features"]
            )
        # Near misses are direction, not evidence: the compact view only.
        near = [
            {key: row[key] for key in _NEAR_MISS_KEYS if key in row} for row in near
        ]
    except Exception as exc:  # noqa: BLE001 - seeding never blocks a start
        return {"available": False, "reason": str(exc)[:240]}
    limit = int(policy.get("signal_first_limit") or 10)
    return {
        "available": True,
        "method": (
            "library event study on the full train split (decimated events, "
            "|t|>=2 vs drift, 3/4 fold sign agreement), unconditionally and "
            "conditioned on the store's macro and leader labels; validated = "
            "Benjamini-Hochberg q over the whole family at or under the "
            "threshold, t_net at the floor and positive edge net of the taker "
            "round trip; replicated = the same replication with the cost "
            "decomposition (gross move vs taker and maker round trips) instead "
            "of a cost gate; both powered and non-inferior on the screen slices"
        ),
        "timeframes": frames["timeframes"],
        "condition_features": sorted(frames["condition_features"]),
        "feature_columns": list(frames["feature_columns"]),
        "breakdown": breakdown,
        "horizons_required": frames["scan_kwargs"].get("required_horizons") or {},
        "cross_sectional": cross_sectional,
        "tests": len(full_rows),
        # The haircut the designer should see: this many tests would clear a
        # 5% bar by luck alone, which is why q gates the validated tier.
        "expected_lucky_passes": round(0.05 * len(full_rows), 1),
        "q_threshold": max_q,
        "taker_round_trip_bps": frames["taker_round_trip_bps"],
        "maker_round_trip_bps": frames["maker_round_trip_bps"],
        "funnel": funnel,
        "train_days": round(frames["train_days"], 2),
        "how_to_use_notes": SIGNAL_RECIPE_NOTES,
        "signals": [_pack_signal_entry(row) for row in selected[:limit]],
        "replicated": [
            _pack_signal_entry(row) for row in _diverse_signal_rows(replicated, limit)
        ],
        # Under power or against a slice: direction for the designer, not
        # evidence.
        "near_misses": near[:10],
    }


def _select_validated_rows(
    full_rows: Sequence[Mapping[str, Any]],
    per_slice: Mapping[str, Mapping[tuple[str, str, str, int], Mapping[str, Any]]],
    *,
    days: float,
    min_events: int,
    max_q: float,
    slice_min_t: float,
    min_t_net: float,
    taker_round_trip_bps: float,
    maker_round_trip_bps: float,
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]
]:
    """Two tiers from a full-train scan, both powered and non-inferior on the
    screen slices. ``validated`` clears the family-corrected q, the t_net
    floor and a positive edge net of the taker round trip. ``replicated`` is
    every directional, fold-stable row regardless of cost, carrying the cost
    decomposition (gross move, edge net of the taker and of the maker round
    trip, an execution hint). The scan answers existence; monetization is the
    mechanism's job, and a cost filter here hid the very rows a passive
    design needs (2026-09-04 replay: 66 of 74 strong rows, the HYPE 5m seed
    among them). Direction and strength come from the gross t-stat
    (``t_net``'s sign is not the side); q is reported and tiers, it does not
    empty the list — the full-dev haircut corrects for trials at strategy
    level."""
    validated: list[dict[str, Any]] = []
    replicated: list[dict[str, Any]] = []
    near: list[dict[str, Any]] = []
    # Stage counts so the designer reads why nothing survived, not silence.
    funnel = {
        "tests": len(full_rows),
        "directional_fold_stable": 0,
        "cost_positive": 0,
        "t_net_at_floor": 0,
        "q_at_threshold": 0,
        "powered": 0,
        "non_inferior": 0,
        "validated": 0,
        "replicated": 0,
        "passive_only": 0,
        "mechanism_required": 0,
        "regime_tests": 0,
        "regime_survivors": 0,
    }
    # A conditioned row whose cell holds every event of its base row carries
    # no information about the condition (a label constant over the sample).
    base_events: dict[tuple[str, str, str, int], int] = {
        (
            str(r["symbol"]),
            str(r["signal"]),
            str(r["timeframe"]),
            int(r["horizon"]),
        ): int(r.get("n") or 0)
        for r in full_rows
        if not r.get("regime")
    }
    for row in full_rows:
        conditioned = bool(row.get("regime"))
        funnel["regime_tests"] += int(conditioned)
        direction = row.get("direction")
        t_gross = float(row.get("t_stat_vs_drift") or 0.0)
        t_net = float(row.get("t_net") or 0.0)
        if direction not in ("long", "short") or not bool(row.get("fold_stable")):
            continue
        if conditioned and int(row.get("n") or 0) == base_events.get(
            (
                str(row["symbol"]),
                str(row["signal"]),
                str(row["timeframe"]),
                int(row["horizon"]),
            )
        ):
            continue
        funnel["directional_fold_stable"] += 1
        taker = float(row.get("round_trip_cost_bps") or taker_round_trip_bps)
        maker = float(maker_round_trip_bps)
        edge_net_taker = float(row.get("edge_net_bps") or 0.0)
        gross_edge = edge_net_taker + taker
        edge_net_maker = gross_edge - maker
        # The standard error in bps falls out of the two t's: |t| = excess /
        # sem and t_net = (excess - taker) / sem, so the maker-cost t is the
        # taker one plus the cost difference in sem units.
        spread = abs(t_gross) - t_net
        t_net_maker = (
            round(t_net + (taker - maker) * spread / taker, 3)
            if taker > 0 and spread > 0
            else None
        )
        hint = (
            "taker_ok"
            if edge_net_taker > 0
            else "passive_only"
            if edge_net_maker > 0
            else "mechanism_required"
        )
        funnel["cost_positive"] += int(edge_net_taker > 0)
        t_ok = t_net >= min_t_net and edge_net_taker > 0
        funnel["t_net_at_floor"] += int(t_ok)
        key = (
            str(row["symbol"]),
            str(row["signal"]),
            str(row["timeframe"]),
            int(row["horizon"]),
        )
        events = int(row.get("n") or 0)
        density = events / days
        side = 1.0 if t_gross > 0 else -1.0
        if conditioned:
            # A conditioned signal gates itself; the base slices do not judge
            # it.
            slice_t: dict[str, float] = {}
            non_inferior = True
        else:
            slice_t = {
                label: float((table.get(key) or {}).get("t_stat_vs_drift") or 0.0)
                for label, table in per_slice.items()
            }
            # Non-inferiority, not confirmation: a 35-day slice may be flat
            # for the signal, it may not be significantly against its side.
            non_inferior = all(
                value * side > -slice_min_t for value in slice_t.values()
            )
        q_value = row.get("q_value")
        q_ok = q_value is not None and float(q_value) <= max_q
        powered = events >= min_events
        funnel["q_at_threshold"] += int(t_ok and q_ok)
        funnel["powered"] += int(t_ok and q_ok and powered)
        funnel["non_inferior"] += int(t_ok and q_ok and powered and non_inferior)
        entry: dict[str, Any] = {
            "symbol": key[0],
            "signal": key[1],
            "family": row.get("family"),
            "library": row.get("library"),
            "timeframe": key[2],
            "horizon": key[3],
            "direction": str(direction),
            "scope": "regime" if conditioned else "base",
            "regime": row.get("regime"),
            "regime_source": row.get("regime_source"),
            "t_stat": round(t_gross, 3),
            "t_net": round(t_net, 3),
            "t_net_maker": t_net_maker,
            "q_value": row.get("q_value"),
            "bh_verdict": row.get("verdict"),
            "gross_edge_bps": round(gross_edge, 2),
            "edge_net_bps": round(edge_net_taker, 2),
            "edge_net_maker_bps": round(edge_net_maker, 2),
            "taker_round_trip_bps": round(taker, 2),
            "maker_round_trip_bps": round(maker, 2),
            "execution_hint": hint,
            "events": events,
            "events_per_day": round(density, 3),
            "folds_agreeing": row.get("folds_agreeing"),
            "t_stat_by_slice": {
                label: round(value, 3) for label, value in slice_t.items()
            },
            "score": round(t_net, 3),
        }
        if not (powered and non_inferior):
            entry["shortfall"] = "underpowered" if not powered else "slice_against"
            near.append(entry)
            continue
        if t_ok and q_ok:
            entry["tier"] = "validated"
            validated.append(entry)
        else:
            entry["tier"] = "replicated"
            entry["shortfall"] = (
                "t_net_below_floor" if not t_ok else "q_above_threshold"
            )
            replicated.append(entry)
        if hint != "taker_ok":
            funnel[hint] += 1
        funnel["regime_survivors"] += int(conditioned)
    funnel["validated"] = len(validated)
    funnel["replicated"] = len(replicated)
    validated.sort(key=lambda item: -float(item["score"]))
    replicated.sort(key=lambda item: -abs(float(item["t_stat"])))
    near.sort(key=lambda item: -abs(float(item["t_stat"])))
    return validated, replicated, near, funnel


def _numeric_tunables(
    strategy_params: Mapping[str, Any], params: Mapping[str, Any], *, limit: int = 8
) -> dict[str, float]:
    excluded = set(_TARGET_EXECUTION_PARAM_KEYS) | {
        "warmup_bars",
        "lookback_bars",
        "full_history",
        "target_regimes",
        "defense_overlay",
        "size_scale",
    }
    merged = {**dict(strategy_params), **dict(params)}
    out: dict[str, float] = {}
    for name in sorted(merged):
        value = merged[name]
        if name in excluded or isinstance(value, bool):
            continue
        if isinstance(value, int | float) and value != 0 and math.isfinite(value):
            out[name] = float(value)
        if len(out) >= limit:
            break
    return out


def _neighborhood_dimension(value: float, span: float) -> dict[str, Any]:
    low, high = value * (1 - span), value * (1 + span)
    if low > high:
        low, high = high, low
    if float(value).is_integer() and abs(value) >= 2:
        low_int = int(math.floor(low))
        high_int = max(low_int + 1, int(math.ceil(high)))
        return {"type": "int", "low": low_int, "high": high_int}
    return {"type": "float", "low": round(low, 10), "high": round(high, 10)}


def _plateau_select(
    grid: ExecutionGridResult,
    dimensions: Sequence[str],
    rank_by: str,
    *,
    radius: float = 0.2,
    min_ratio: float = 0.5,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Prefer a plateau over a peak: the trial whose neighborhood scores best.

    A lone peak whose neighbors score under ``min_ratio`` of it is a fit to
    noise; the best-neighborhood trial generalizes.
    """
    rows = [
        row
        for row in (getattr(grid, "runs", None) or [])
        if isinstance(row.get("params"), dict) and row.get(rank_by) is not None
    ]
    if not rows:
        # No per-trial rows (older grids, test doubles): keep the ranked winner.
        ranked = list(getattr(grid, "ranked", None) or [])
        return (
            (dict(ranked[0]) if ranked else None),
            {"neighbors_of_best": 0, "ratio": None, "selected_by": "ranked"},
        )

    def score(row: Mapping[str, Any]) -> float:
        return float(row[rank_by])

    def near(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
        for name in dimensions:
            left, right = a.get(name), b.get(name)
            if isinstance(left, int | float) and isinstance(right, int | float):
                if abs(float(left) - float(right)) > radius * max(
                    abs(float(left)), 1e-9
                ):
                    return False
            elif left != right:
                return False
        return True

    def neighborhood(row: Mapping[str, Any]) -> tuple[float, int]:
        values = [
            score(other)
            for other in rows
            if other is not row and near(row["params"], other["params"])
        ]
        return (sum([score(row), *values]) / (1 + len(values)), len(values))

    best = max(rows, key=score)
    best_mean, best_neighbors = neighborhood(best)
    ratio = best_mean / score(best) if score(best) > 0 and best_neighbors else None
    supported = [row for row in rows if neighborhood(row)[1] >= 1]
    peaky = best_neighbors >= 2 and ratio is not None and ratio < min_ratio
    # A best trial nobody sits next to is unverifiable noise when the search
    # did find neighborhoods elsewhere; take the best-supported one instead.
    isolated_peak_rejected = best_neighbors == 0 and bool(supported)
    if isolated_peak_rejected:
        selected = max(supported, key=lambda row: neighborhood(row)[0])
    elif peaky:
        selected = max(rows, key=lambda row: neighborhood(row)[0])
    else:
        selected = best
    return dict(selected), {
        "neighbors_of_best": best_neighbors,
        "ratio": None if ratio is None else round(ratio, 4),
        "selected_by": "plateau" if selected is not best else "peak",
        "selected_trial": selected.get("trial"),
        "isolated_peak_rejected": isolated_peak_rejected,
        "isolated": best_neighbors == 0 and not supported,
    }


def _fill_signatures(result: Any) -> set[tuple[str, str, str]]:
    return {
        (
            str(row.get("timestamp") or row.get("filled_at")),
            str(row.get("symbol")),
            str(row.get("side") or row.get("action")),
        )
        for row in result.trades
    }


def _alive_tunables(
    subject: Mapping[str, Any],
    probe_slice: PreparedExecutionDataset,
    params: Mapping[str, Any],
    tunables: Mapping[str, float],
    *,
    span: float,
) -> list[str]:
    """Knobs whose endpoints change fills on a short slice.

    A sparse strategy trades on ~1% of bars, so an 8-bar intent probe reads
    every knob as dead; a short full simulation compared by fill signature
    is the honest cheap test.
    """
    baseline = _fill_signatures(
        simulate_execution(
            subject["script"], probe_slice, subject["spec"], dict(params)
        )
    )
    alive: list[str] = []
    for name, value in tunables.items():
        dimension = _neighborhood_dimension(value, span)
        for endpoint in (dimension["low"], dimension["high"]):
            variant = {**params, name: endpoint}
            fills = _fill_signatures(
                simulate_execution(
                    subject["script"], probe_slice, subject["spec"], variant
                )
            )
            if fills != baseline:
                alive.append(name)
                break
        if len(alive) >= _MAX_SEARCH_DIMENSIONS:
            break
    return alive


def _incumbent_neighborhood(
    store: JobStore,
    job_id: str,
    state: Mapping[str, Any],
    candidate_root: Path,
    *,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Deterministic local search around the incumbent before the model edits.

    Probes which numeric knobs move decisions, tunes the live ones on the
    recent screen slice, verifies the winner on the earlier slice, and
    applies it only when it beats the incumbent on both.  Always leaves a
    typed search_space.json so the parameter slot starts from a valid space.
    """
    campaign_id = str(state["campaign_id"])
    try:
        subject = _load_subject(store, job_id, candidate_root, campaign_id=campaign_id)
        train = _discovery_dataset(subject["dataset"], policy)
        params, _, _ = _calibrated_params(store, job_id, subject)
        strategy = _load_strategy(subject["script"], dict(params))
        tunables = _numeric_tunables(getattr(strategy, "params", {}) or {}, params)
        if not tunables:
            return {"available": False, "reason": "no numeric tunables"}
        slices = _policy_screen_slices(train, policy)
        recent = slices[0][1]
        span = float(policy.get("incumbent_neighborhood_span") or 0.3)
        alive = _alive_tunables(
            subject,
            _tail(
                recent, int(policy.get("incumbent_neighborhood_probe_bars") or 2_000)
            ),
            params,
            tunables,
            span=span,
        )
        search_space = {
            name: _neighborhood_dimension(tunables[name], span) for name in alive
        }
        atomic_write_text(
            candidate_root / "search_space.json",
            json.dumps(search_space or {}, indent=2, sort_keys=True) + "\n",
        )
        if not alive:
            return {
                "available": True,
                "searched": [],
                "applied": False,
                "reason": "no decision-sensitive numeric params on the screen slice",
            }
        baseline = {
            label: float(
                simulate_execution(
                    subject["script"], dataset, subject["spec"], params
                ).stats.get("net_return")
                or 0.0
            )
            for label, dataset in slices
        }
        grid = run_optuna_search(
            subject["script"],
            recent,
            subject["spec"],
            {**params, **search_space},
            rank_by="net_return",
            n_trials=int(policy.get("incumbent_neighborhood_trials") or 6),
            seed=_OPTUNA_SEED,
            timeout=float(policy.get("incumbent_neighborhood_timeout_seconds") or 180),
        )
        selected, plateau = _plateau_select(grid, alive, "net_return")
        if selected is None:
            return {
                "available": True,
                "searched": alive,
                "applied": False,
                "reason": "no valid neighborhood trial",
            }
        best_params = {
            name: selected["params"][name]
            for name in alive
            if name in selected["params"]
        }
        tuned = {**params, **best_params}
        best = {"recent": float(selected.get("net_return") or 0.0)}
        for label, dataset in slices[1:]:
            best[label] = float(
                simulate_execution(
                    subject["script"], dataset, subject["spec"], tuned
                ).stats.get("net_return")
                or 0.0
            )
        applied = all(best[label] > baseline[label] for label in best)
        if applied:
            job_data = _load_job_yaml(candidate_root)
            execution_params = dict(job_data.get("execution_params") or {})
            execution_params.update(best_params)
            job_data["execution_params"] = execution_params
            atomic_write_text(
                candidate_root / "job.yaml", yaml.safe_dump(job_data, sort_keys=False)
            )
            atomic_write_text(
                candidate_root / "search_space.json",
                json.dumps(
                    {
                        name: _neighborhood_dimension(float(value), span / 2)
                        for name, value in best_params.items()
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
    except Exception as exc:  # noqa: BLE001 - local search never blocks prepare
        return {"available": False, "reason": str(exc)[:240]}
    return {
        "available": True,
        "searched": alive,
        "trials": len(grid.runs),
        "incumbent": {label: round(value, 6) for label, value in baseline.items()},
        "best": {
            "params": best_params,
            **{label: round(value, 6) for label, value in best.items()},
        },
        "plateau": plateau,
        "applied": applied,
    }


def _incumbent_failure_modes(
    store: JobStore, job_id: str, campaign_root: Path, *, policy: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Where and when the incumbent loses on the screen slices.

    Two bounded simulations at campaign start; the design phase targets the
    incumbent's losing days and regimes instead of trying to beat it where
    it already earns.
    """
    if not bool(policy.get("incumbent_failure_modes", True)):
        return None
    try:
        subject = _load_subject(
            store,
            job_id,
            campaign_root / "source",
            dataset_root=campaign_root / CAMPAIGN_DATA_ROOT,
        )
        train = _discovery_dataset(subject["dataset"], policy)
        params, _, _ = _calibrated_params(store, job_id, subject)
        labels = classify_portfolio_regimes(
            train.bars.to_frame(),
            universe=regime_universe(params, subject["dataset"].bars.symbols),
        )
        day_labels = _majority_day_labels(labels)
        leader_days = _leader_day_states(
            campaign_root / CAMPAIGN_DATA_ROOT / DEFAULT_FEATURES_PATH
        )
        slices: dict[str, Any] = {}
        for label, dataset in _policy_screen_slices(train, policy):
            result = simulate_execution(
                subject["script"], dataset, subject["spec"], params
            )
            daily = daily_log_returns(result.equity_curve)
            slices[label] = _failure_mode_summary(daily, day_labels)
            attribution = _leader_attribution(daily, leader_days)
            if attribution:
                slices[label]["leader_attribution"] = attribution
    except Exception as exc:  # noqa: BLE001 - diagnostics never block a start
        return {"available": False, "reason": str(exc)[:240]}
    return {
        "available": True,
        "classifier": PORTFOLIO_REGIME_CLASSIFIER,
        "slices": slices,
    }


def _leader_day_states(store_path: Path) -> dict[str, int]:
    """Day -> leader state code (+1 broad rally, -1 broad selloff, 0) from the
    campaign's frozen feature store; majority per day of the hourly rows."""
    if not store_path.exists():
        return {}
    needle = f'"name": "{LEADER_FEATURE_NAME}"'
    values: dict[str, float] = {}
    with store_path.open(encoding="utf-8") as handle:
        for line in handle:
            if needle not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            stamp = str(row.get("timestamp") or "")
            if stamp and stamp not in values:
                values[stamp] = float(row.get("value") or 0.0)
    if not values:
        return {}
    series = pd.Series(values)
    series.index = pd.to_datetime(series.index, utc=True)
    return {
        day: int(float(label))
        for day, label in _majority_day_labels(series.astype(str)).items()
    }


def _leader_attribution(
    daily: Sequence[tuple[str, float]], day_states: Mapping[str, int]
) -> dict[str, Any] | None:
    """How much of a slice's losses fell on broad-rally and broad-selloff
    days, beside those days' share of the slice. None when no day is labelled."""
    rows = [(str(day), float(value)) for day, value in daily]
    labelled = [(day, value) for day, value in rows if day in day_states]
    if not rows or not labelled:
        return None
    total_loss = sum(value for _, value in rows if value < 0)
    out: dict[str, Any] = {"days": len(rows), "labelled_days": len(labelled)}
    for state, code in (("rally", 1), ("selloff", -1)):
        days = [(day, value) for day, value in rows if day_states.get(day) == code]
        loss = sum(value for _, value in days if value < 0)
        out[state] = {
            "days": len(days),
            "day_share": round(len(days) / len(rows), 4),
            "loss_share": abs(round(loss / total_loss, 4)) if total_loss else 0.0,
            "net_log_growth": round(sum(value for _, value in days), 8),
        }
    return out


def _majority_day_labels(labels: pd.Series) -> dict[str, str]:
    if labels.empty:
        return {}
    frame = pd.DataFrame(
        {"day": pd.to_datetime(labels.index, utc=True).date.astype(str), "cell": labels}
    )
    return {
        str(day): str(group["cell"].mode().iloc[0])
        for day, group in frame.groupby("day")
        if not group["cell"].mode().empty
    }


def _failure_mode_summary(
    daily: Sequence[tuple[str, float]], day_labels: Mapping[str, str]
) -> dict[str, Any]:
    """Losing days, worst days, and per-regime P&L of one daily return series."""
    by_regime: dict[str, dict[str, Any]] = {}
    losing: list[tuple[str, float]] = []
    for day, value in daily:
        cell = str(day_labels.get(str(day), MIXED_REGIME))
        bucket = by_regime.setdefault(
            cell, {"days": 0, "losing_days": 0, "net_log_growth": 0.0}
        )
        bucket["days"] += 1
        bucket["net_log_growth"] += float(value)
        if float(value) < 0:
            bucket["losing_days"] += 1
            losing.append((str(day), float(value)))
    for bucket in by_regime.values():
        bucket["net_log_growth"] = round(bucket["net_log_growth"], 8)
        bucket["net_return"] = round(math.exp(bucket["net_log_growth"]) - 1.0, 6)
    worst_regime = (
        min(by_regime, key=lambda cell: by_regime[cell]["net_log_growth"])
        if by_regime
        else None
    )
    total = sum(float(value) for _, value in daily)
    losing_total = sum(value for _, value in losing)
    return {
        "days": len(daily),
        "net_return": round(math.exp(total) - 1.0, 6),
        "losing_days": len(losing),
        "losing_log_growth": round(losing_total, 8),
        "losing_return": round(math.exp(losing_total) - 1.0, 6),
        "worst_days": [
            {
                "day": day,
                "return": round(value, 6),
                "regime": day_labels.get(day, MIXED_REGIME),
            }
            for day, value in sorted(losing, key=lambda item: item[1])[:5]
        ],
        "by_regime": by_regime,
        "worst_regime": worst_regime,
    }


def _existing_baseline_receipt(
    root: Path,
    *,
    window: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    path = root / "results/backtest/baseline.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False, "reason": "baseline backtest unavailable"}
    if not isinstance(document, dict):
        return {"available": False, "reason": "baseline backtest is not an object"}
    nested_result = document.get("result")
    result = nested_result if isinstance(nested_result, dict) else document
    stats = result.get("stats") or {}
    receipt: dict[str, Any] = {
        "available": True,
        "run_id": result.get("run_id"),
        "stats": stats,
        "validation": result.get("validation") or {},
        "source": {
            "path": "results/backtest/baseline.json",
            "sha256": _file_hash(path),
        },
    }
    raw_params = result.get("params")
    result_params: dict[str, Any] = (
        dict(raw_params) if isinstance(raw_params, dict) else {}
    )
    capital = float(
        result_params.get("initial_capital")
        or (params or {}).get("initial_capital")
        or DEFAULT_INITIAL_CAPITAL
    )
    baseline_window = _baseline_window(document.get("scope"), window, capital)
    if baseline_window:
        receipt["window"] = baseline_window
        economics = receipt_economics({"stats": stats, "window": baseline_window})
        if economics:
            receipt["economics"] = economics
    fee_bps = (params or {}).get("fee_bps", result_params.get("fee_bps"))
    slippage_bps = (params or {}).get("slippage_bps", result_params.get("slippage_bps"))
    if fee_bps is not None and slippage_bps is not None:
        receipt["round_trip_cost_bps"] = round(
            2.0 * (float(fee_bps) + float(slippage_bps)), 2
        )
    receipt["maker_round_trip_bps"] = maker_round_trip_bps(
        {**result_params, **(params or {})}
    )
    if stats.get("net_return") is not None:
        # Doing nothing is the bar the incumbent is measured against first:
        # a book that loses to cash is retired, not repaired.
        net_return = float(stats["net_return"])
        receipt["vs_cash"] = {
            "net_return": round(net_return, 6),
            "buy_hold_return": stats.get("buy_hold_return"),
            "fee_pct_of_capital": (receipt.get("economics") or {}).get(
                "fee_pct_of_capital"
            ),
            "window_days": (receipt.get("window") or {}).get("days"),
            "beats_cash": net_return > 0,
        }
    return receipt


def _baseline_window(
    scope: Any, dataset_window: Mapping[str, Any] | None, capital: float
) -> dict[str, Any]:
    """Baseline span: its own recorded scope, else the full frozen dataset span."""
    if isinstance(scope, dict) and scope.get("start") and scope.get("end"):
        try:
            days = (
                pd.Timestamp(scope["end"]) - pd.Timestamp(scope["start"])
            ).total_seconds() / 86_400.0
        except (TypeError, ValueError):
            days = 0.0
        if days > 0:
            return {
                "days": round(days, 4),
                "bars": int(scope.get("bars") or 0),
                "starting_equity": capital,
                "source": "baseline_scope",
            }
    days = float((dataset_window or {}).get("full_days") or 0.0)
    if days <= 0:
        return {}
    return {
        "days": round(days, 4),
        "bars": int((dataset_window or {}).get("full_bars") or 0),
        "starting_equity": capital,
        "source": "dataset_span",
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


def _initial_stage(
    schema: str, policy: Mapping[str, Any], validated_signals: Mapping[str, Any] | None
) -> str:
    """Compose before design when the scan is available and rounds are
    budgeted; the legacy schema goes straight to generation."""
    if schema != SCHEMA_VERSION:
        return "generate"
    rounds = int(policy.get("signal_composition_rounds", 2) or 0)
    if (
        rounds > 0
        and isinstance(validated_signals, Mapping)
        and validated_signals.get("available")
    ):
        return COMPOSE_STAGE
    return "design"


def _composition_state(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "rounds_max": int(policy.get("signal_composition_rounds", 2) or 0),
        "rounds_used": 0,
        "history": [],
        "problems": None,
        # name -> expression: a proposal name means one definition for the
        # whole campaign, so a citation never changes meaning under it.
        "names": {},
    }


def _signal_breakdown(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Where the survivors are: counts by family and timeframe, and by regime
    cell. The compose prompt reads this instead of the whole row list."""
    family_timeframe: dict[str, int] = {}
    regime: dict[str, int] = {}
    for row in rows:
        key = f"{row.get('family')}@{row.get('timeframe')}"
        family_timeframe[key] = family_timeframe.get(key, 0) + 1
        cell = str(row.get("regime") or "base")
        regime[cell] = regime.get(cell, 0) + 1
    return {
        "family_timeframe": dict(
            sorted(family_timeframe.items(), key=lambda item: -item[1])[:16]
        ),
        "regime": dict(sorted(regime.items(), key=lambda item: -item[1])),
    }


def _compose_prompt_block(
    store: JobStore, job_id: str, state: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any]:
    """The composition turn: the designer proposes signal definitions in the
    DSL, the harness scans them under one family and reports; survivors
    become citable. AlphaAgent's hypothesis-to-factor loop on our scanner."""
    root = store.job_dir(job_id)
    pack_path = (root / str(state["diagnostic_pack"])).resolve()
    manifest_path = (root / str(state["manifest"])).resolve()
    pack = store.read_json(job_id, str(state["diagnostic_pack"]), default={}) or {}
    validated = pack.get("validated_signals") or {}
    composition = state.get("composition") or {}
    rounds_used = int(composition.get("rounds_used") or 0)
    rounds_max = int(composition.get("rounds_max") or 0)
    history = list(composition.get("history") or [])
    last = history[-1] if history else None
    problems = list(composition.get("problems") or [])
    funnel = validated.get("funnel") or {}
    breakdown = validated.get("breakdown") or {}
    columns = ["open", "high", "low", "close", "volume"] + [
        str(name) for name in validated.get("feature_columns") or []
    ]
    funnel_text = (
        f"Funnel so far: {funnel.get('tests')} tests, {funnel.get('validated') or 0} "
        f"validated, {funnel.get('replicated') or 0} replicated "
        f"({funnel.get('passive_only') or 0} passive-only, "
        f"{funnel.get('mechanism_required') or 0} needing a mechanism), "
        f"{funnel.get('regime_survivors') or 0} regime-conditioned and "
        f"{funnel.get('population_survivors') or 0} population survivors; by "
        f"family@timeframe {breakdown.get('family_timeframe')}; by regime "
        f"{breakdown.get('regime')}. "
        if funnel
        else ""
    )
    last_text = (
        f"Round {last.get('round')}: {len(last.get('proposals') or [])} proposals "
        f"scanned in a family of {last.get('family_size')}, "
        f"{len(last.get('survivors') or [])} survived ("
        + "; ".join(
            f"{item.get('name')}: {item.get('tier')}"
            + (
                f" best t {float(item['best'].get('t_stat') or 0):+.1f} q "
                f"{float(item['best'].get('q_value') or 0):.2f} n "
                f"{item['best'].get('events')} gross "
                f"{float(item['best'].get('gross_edge_bps') or 0):+.1f} bps "
                f"{item['best'].get('execution_hint')}"
                + (
                    f" in {item['best']['regime']}"
                    if item["best"].get("regime")
                    else ""
                )
                if item.get("best")
                else " (no measurable row)"
            )
            for item in (last.get("proposals") or [])[:12]
        )
        + "). "
        if last and not last.get("ended")
        else ""
    )
    problems_text = (
        "The previous submission was rejected and the round was not consumed; "
        "fix these: " + "; ".join(problems) + ". "
        if problems
        else ""
    )
    next_action = (
        f"Composition round {rounds_used + 1} of {rounds_max}. Read `{pack_path}` "
        "(validated_signals: the funnel, the validated and replicated tiers with "
        "their cost decomposition, the near misses) and propose up to "
        f"{WORKSPACE_SIGNAL_CAP} new signal definitions. The harness scans them "
        "with the library under one Benjamini-Hochberg family across "
        f"{validated.get('timeframes')} and every horizon in seconds and reports "
        "each proposal's best row (t, q, events, gross move vs the taker and "
        "maker round trips, by regime); survivors join the pack as citable "
        'signals for the design stage. A proposal is {"name": <matches '
        '^[a-z0-9_]{1,48}$, not a library name, no pop_ prefix>, "family": '
        '<str>, "description": <str>, "min_bars": <int warmup>, '
        '"expression": <one Python expression over f>} where f is the '
        f"resampled bar frame with columns {', '.join(columns)} and the helpers "
        f"{', '.join(_COMPOSE_DSL_NAMES)} (signatures: new_extreme(f, period, "
        "+1|-1), momentum(f, period, +1|-1), rsi_extreme(f, level, +1|-1, "
        "period=14), rsi_cross(f, level, +1|-1, period=14), bb_extreme(f, z, "
        "period=20), spike_vs_sma(f, pct, +1|-1, period=20), wide_range(f, "
        "+1|-1, multiple=2.0, period=14), vol_surge(f, +1|-1, multiple=2.0, "
        "window=20), ema_cross(f, +1|-1, fast=9, slow=50), sma_cross(f, +1|-1, "
        "period=20), macd_cross(f, +1|-1, fast=12, slow=26, signal=9), "
        "extended(f, +1|-1, short=24, long=72, extreme=12), compression_break(f, "
        "+1|-1, period=20, lookback=100), trend_gated_extreme(f, +1|-1, "
        "period=5, span=50, lag=10), session_window(f, start_minute, "
        "end_minute) in New York wall-clock, weekend(f); close(f), sma(series, "
        "n), ema(series, n), atr(f, n), wilder_rsi(series, n), bb_z(series, n), "
        "cross(fast, slow, +1|-1), fresh(event)); combine with &, |, ~, "
        "comparisons, f[<column>] for a store column (e.g. f['macro_regime'] "
        "== -1.0) and .shift(k, fill_value=False) for k >= 1; nothing else "
        "resolves (no other names, modules, attributes or imports; Series "
        "methods are limited to shift, rolling, ewm, mean, std, max, min, "
        "sum, quantile, abs, diff, pct_change, fillna, astype, clip, rank, "
        "where and the comparisons). The expression "
        "must be boolean, row-aligned and causal (current and past rows only: "
        "no shift(-k), no centered windows, no full-frame statistics); a "
        "malformed or non-causal list is rejected with the problems quoted "
        "back and the round is not consumed. Every proposal widens the family, "
        "so propose the mechanisms the funnel points at (a regime or session "
        "gate, a composition, a different window), not variants of one idea. "
        "A round's report names each survivor's pointer at that moment; a "
        "later round prepends rows, so cite from the pack you read at design "
        "time (every entry carries a stable signal_id). "
        f"{funnel_text}{last_text}{problems_text}"
        + _mechanism_instruction(
            job_id,
            pack,
            [*(validated.get("signals") or []), *(validated.get("replicated") or [])],
        )
        + f'Call wayfinder_core_jobs(action="evolution_compose", job_id="{job_id}", '
        "signal_proposals=[...]) exactly once with the list, or with an empty "
        "list to end composition and move to design, then end this stage. A "
        "status of busy means the machine's compute budget is spent: end the "
        "turn and the next one repeats this round."
    )
    return {
        "job_id": job_id,
        "campaign_id": state["campaign_id"],
        "stage": COMPOSE_STAGE,
        "session_stage": COMPOSE_STAGE,
        "artifact_key": f"compose-{rounds_used + 1:02d}",
        "agent_name": "wayfinder-evolution-designer",
        "deadline_at": state["deadline_at"],
        "counts": state["counts"],
        "diagnostic_pack": str(pack_path),
        "manifest_path": str(manifest_path),
        "next_action": next_action,
        "constraints": {
            "composition": {
                "round": rounds_used + 1,
                "rounds_max": rounds_max,
                "cap": WORKSPACE_SIGNAL_CAP,
                "columns": columns,
                "timeframes": validated.get("timeframes"),
            },
            "validated_signals": [
                f"{row['symbol']}:{row['signal']}:{row['timeframe']}:{row['horizon']}"
                for row in validated.get("signals") or []
            ],
            "replicated_signals": [
                f"{row['symbol']}:{row['signal']}:{row['timeframe']}:{row['horizon']}"
                for row in validated.get("replicated") or []
            ],
        },
        "valid_evidence_pointers": valid_evidence_pointers(pack),
        "deadline_elapsed": False,
    }


def _compile_signal_proposals(
    proposals: Sequence[Any],
) -> tuple[list[Any], list[str]]:
    """Proposals to defs, collecting every problem instead of stopping at the
    first: the designer fixes the whole list in one turn."""
    problems: list[str] = []
    defs: list[Any] = []
    if len(proposals) > WORKSPACE_SIGNAL_CAP:
        problems.append(
            f"{len(proposals)} proposals exceed the cap of {WORKSPACE_SIGNAL_CAP}"
        )
        return defs, problems
    canonical = set(signal_defs())
    seen: set[str] = set()
    for index, raw in enumerate(proposals):
        label = f"proposal {index}"
        if not isinstance(raw, Mapping):
            problems.append(f"{label}: must be an object")
            continue
        name = str(raw.get("name") or "").strip()
        label = f"{name!r}" if name else label
        if not _PROPOSAL_NAME_RE.match(name):
            problems.append(f"{label}: name must match {_PROPOSAL_NAME_RE.pattern}")
            continue
        if name in canonical or name.startswith("pop_"):
            problems.append(f"{label}: collides with a library or population name")
            continue
        if name in seen:
            problems.append(f"{label}: duplicate name")
            continue
        seen.add(name)
        expression = str(raw.get("expression") or "").strip()
        if not expression:
            problems.append(f"{label}: expression is required")
            continue
        try:
            min_bars = int(raw.get("min_bars") or 0)
        except (TypeError, ValueError):
            problems.append(f"{label}: min_bars must be an integer")
            continue
        if not 1 <= min_bars <= 5_000:
            problems.append(f"{label}: min_bars must be between 1 and 5000")
            continue
        try:
            defs.append(
                compile_signal_expression(
                    name=name,
                    family=str(raw.get("family") or "workspace").strip()[:40],
                    description=str(raw.get("description") or "").strip()[:160],
                    min_bars=min_bars,
                    expression=expression,
                )
            )
        except (SyntaxError, ValueError, NameError, TypeError) as exc:
            problems.append(f"{label}: expression does not compile ({exc})")
    return defs, problems


def _scan_signal_proposals(
    frames: Mapping[str, Any], defs: Sequence[Any], *, policy: Mapping[str, Any]
) -> dict[str, Any]:
    """Scan the proposals beside the library (one family, conditioned on the
    store's labels) and report each proposal's best row and tier."""
    by_name = {spec.name: spec for spec in defs}
    scan_kwargs = {
        **frames["scan_kwargs"],
        "condition_features": frames["condition_features"],
        "extra_signals": tuple(defs),
    }
    full_rows: list[dict[str, Any]] = []
    for symbol, rows in frames["train"].items():
        result = scan_signals(rows, **scan_kwargs)
        for row in result.get("_all_rows") or []:
            full_rows.append({**row, "symbol": symbol})
    max_q = float(policy.get("signal_first_max_q") or 0.20)
    apply_bh_verdicts(full_rows, q_threshold=max_q, min_folds_agree=3)
    per_slice: dict[str, dict[tuple[str, str, str, int], dict[str, Any]]] = {}
    for label, by_symbol in frames["slices"].items():
        table: dict[tuple[str, str, str, int], dict[str, Any]] = {}
        for symbol, rows in by_symbol.items():
            result = scan_signals(
                rows,
                **{
                    **frames["scan_kwargs"],
                    "min_events": 10,
                    "extra_signals": tuple(defs),
                },
            )
            for row in result.get("_all_rows") or []:
                table[
                    (
                        symbol,
                        str(row["signal"]),
                        str(row["timeframe"]),
                        int(row["horizon"]),
                    )
                ] = row
        per_slice[label] = table
    selected, replicated, near, _ = _select_validated_rows(
        full_rows,
        per_slice,
        days=frames["train_days"],
        min_events=int(policy.get("signal_first_min_events") or 40),
        max_q=max_q,
        slice_min_t=float(policy.get("signal_first_slice_min_t") or 1.0),
        min_t_net=float(policy.get("signal_first_min_t_net") or 2.0),
        taker_round_trip_bps=frames["taker_round_trip_bps"],
        maker_round_trip_bps=frames["maker_round_trip_bps"],
    )
    bar_seconds = int(frames["bar_seconds"])
    survivors: list[dict[str, Any]] = []
    tiers: dict[str, str] = {}
    for tier, rows in (
        ("validated", selected),
        ("replicated", replicated),
        ("near", near),
    ):
        for entry in rows:
            name = str(entry["signal"])
            if name not in by_name:
                continue
            tiers.setdefault(name, tier)
            if tier == "near":
                continue
            spec = by_name[name]
            entry["library"] = "workspace"
            entry["expression"] = spec.expression
            entry["min_bars"] = spec.min_bars
            entry["warmup_bars_required"] = library_signal_warmup_bars(
                spec, entry["timeframe"], bar_seconds=bar_seconds
            )
            entry["how_to_use"] = _signal_recipe(
                entry, bar_seconds=bar_seconds, condition=frames["condition_features"]
            )
            survivors.append(_pack_signal_entry(entry))
    taker = float(frames["taker_round_trip_bps"])
    maker = float(frames["maker_round_trip_bps"])
    report: list[dict[str, Any]] = []
    for spec in defs:
        rows = [row for row in full_rows if row["signal"] == spec.name]
        best = max(
            rows,
            key=lambda row: abs(float(row.get("t_stat_vs_drift") or 0.0)),
            default=None,
        )
        best_view = None
        if best is not None:
            row_taker = float(best.get("round_trip_cost_bps") or taker)
            gross = float(best.get("edge_net_bps") or 0.0) + row_taker
            best_view = {
                "symbol": best["symbol"],
                "timeframe": best["timeframe"],
                "horizon": best["horizon"],
                "direction": best.get("direction"),
                "regime": best.get("regime"),
                "t_stat": round(float(best.get("t_stat_vs_drift") or 0.0), 3),
                "q_value": best.get("q_value"),
                "events": best.get("n"),
                "fold_stable": bool(best.get("fold_stable")),
                "gross_edge_bps": round(gross, 2),
                "execution_hint": (
                    "taker_ok"
                    if gross > row_taker
                    else "passive_only"
                    if gross > maker
                    else "mechanism_required"
                ),
            }
        report.append(
            {
                "name": spec.name,
                "tests": len(rows),
                "tier": tiers.get(
                    spec.name, "unmeasured" if not rows else "not_replicated"
                ),
                "best": best_view,
            }
        )
    return {
        "proposals": report,
        "survivors": survivors,
        "family_size": len(full_rows),
        "max_q": max_q,
    }


def _merge_compose_survivors(
    store: JobStore,
    job_id: str,
    state: Mapping[str, Any],
    manifest: dict[str, Any],
    survivors: Sequence[dict[str, Any]],
    round_number: int,
    *,
    limit: int = 10,
) -> list[str | None]:
    """Merge a round's survivors into the frozen pack: the strongest row per
    (symbol, signal, timeframe) up to ``limit`` per tier goes to the front of
    its tier, each tier is capped at twice the limit from the tail, and the
    pack is refitted to its byte budget. Pointers and the merged count come
    from the FITTED pack, located by signal id, so a row the budget trimmed
    is reported as not merged rather than pointed at. A refit that would
    still fall to the fail-closed shape leaves the pack untouched (losing the
    whole feed to a byte budget is worse than merging nothing)."""
    root = store.job_dir(job_id)
    pack_path = root / str(state["diagnostic_pack"])
    pack = store.read_json(job_id, str(state["diagnostic_pack"]), default={}) or {}
    block = pack.setdefault(
        "validated_signals",
        {"available": True, "signals": [], "replicated": [], "near_misses": []},
    )
    by_tier: dict[str, list[dict[str, Any]]] = {"signals": [], "replicated": []}
    for entry in survivors:
        entry["source"] = f"compose:round{round_number}"
        entry["signal_id"] = str(entry.get("signal_id") or _signal_id(entry))
        by_tier["signals" if entry.get("tier") == "validated" else "replicated"].append(
            entry
        )
    kept_ids: set[str] = set()
    for tier_key, rows in by_tier.items():
        if not rows:
            continue
        ordered = sorted(rows, key=lambda item: -abs(float(item.get("t_stat") or 0.0)))
        kept = _diverse_signal_rows(ordered, max(1, int(limit)))
        kept_ids.update(str(entry["signal_id"]) for entry in kept)
        existing = [
            row
            for row in list(block.get(tier_key) or [])
            if str(row.get("signal_id") or _signal_id(row)) not in kept_ids
        ]
        block[tier_key] = [*kept, *existing][: 2 * max(1, int(limit))]
    composed = dict(block.get("composition") or {})
    # The metadata is in the pack BEFORE the fit, at its largest possible
    # values, so the size the fitter enforces is the size that is written.
    block["composition"] = {
        "rounds": round_number,
        "survivors": int(composed.get("survivors") or 0) + len(kept_ids),
        "scanned_survivors": int(composed.get("scanned_survivors") or 0)
        + len(survivors),
    }
    fitted = fit_diagnostic_pack(json.loads(json.dumps(pack, default=str)))
    if fitted.get("pack_truncated"):
        raise ValueError(
            "merging the round's survivors would push the diagnostic pack past "
            "its byte budget even after trimming; nothing was merged"
        )
    fitted_block = fitted.get("validated_signals") or {}
    located: dict[str, str] = {}
    for tier_key in ("signals", "replicated"):
        for position, row in enumerate(fitted_block.get(tier_key) or []):
            located.setdefault(
                str(row.get("signal_id") or _signal_id(row)),
                f"/validated_signals/{tier_key}/{position}",
            )
    # One pointer per persisted row: a second survivor with the same
    # identity (same signal, symbol, timeframe, horizon and regime) is the
    # same row and is not counted twice.
    pointers: list[str | None] = []
    assigned: set[str] = set()
    for entry in survivors:
        signal_id = str(entry["signal_id"])
        pointer = located.get(signal_id) if signal_id in kept_ids else None
        if pointer is not None and signal_id in assigned:
            pointer = None
        if pointer is not None:
            assigned.add(signal_id)
        pointers.append(pointer)
    merged = sum(pointer is not None for pointer in pointers)
    # Never larger than the placeholder the fitter measured.
    fitted_block["composition"] = {
        **dict(fitted_block.get("composition") or {}),
        "survivors": int(composed.get("survivors") or 0) + merged,
    }
    atomic_write_json(pack_path, fitted)
    manifest["diagnostic_pack"] = {
        **dict(manifest.get("diagnostic_pack") or {}),
        "sha256": _file_hash(pack_path),
    }
    atomic_write_json(root / str(state["manifest"]), manifest)
    return pointers


def _composition_claim(
    state: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """What a composition round must find unchanged when it commits: the
    campaign, its stage, the rounds used and the pack it will merge into."""
    composition = state.get("composition") or {}
    return {
        "campaign_id": str(state.get("campaign_id") or ""),
        "stage": str(state.get("stage") or ""),
        "rounds_used": int(composition.get("rounds_used") or 0),
        "pack_sha256": str((manifest.get("diagnostic_pack") or {}).get("sha256") or ""),
    }


def submit_signal_proposals(
    store: JobStore,
    job_id: str,
    *,
    signal_proposals: Sequence[Any] | None,
) -> dict[str, Any]:
    """One composition round: claim under the state lock, compile and
    validate, scan under the machine-wide compute lock, then commit only if
    the campaign, its stage, its round count and its pack are unchanged.
    Problems are returned, not raised, and do not consume a round; an empty
    list ends composition; a busy compute budget returns ``busy``."""
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = _active_campaign(store, job_id)
        if state.get("stage") != COMPOSE_STAGE:
            raise ValueError("evolution campaign is not in the compose stage")
        campaign_id = str(state["campaign_id"])
        manifest = _campaign_manifest(store, job_id, campaign_id)
        policy = manifest.get("policy") or {}
        composition = state.setdefault("composition", _composition_state(policy))
        round_number = int(composition.get("rounds_used") or 0) + 1
        proposals = list(signal_proposals or [])
        if not proposals:
            composition["history"] = [
                *list(composition.get("history") or []),
                {
                    "round": round_number,
                    "ended": True,
                    "proposals": [],
                    "survivors": [],
                },
            ]
            composition["problems"] = None
            state["stage"] = "design"
            _save_campaign(store, job_id, state)
            store.append_journal(
                job_id,
                {
                    "type": "evolution_campaign_composed",
                    "campaign_id": campaign_id,
                    "round": round_number,
                    "ended": True,
                    "stage": "design",
                },
            )
            return {"status": "ended", "round": round_number, "stage": "design"}
        defs, problems = _compile_signal_proposals(proposals)
        registry = dict(composition.get("names") or {})
        for spec in defs:
            previous = registry.get(spec.name)
            if previous is not None and _definition_fingerprint(previous) != (
                _definition_fingerprint(spec)
            ):
                problems.append(
                    f"{spec.name!r} was proposed in an earlier round with a different "
                    "definition (expression or min_bars); names are immutable within "
                    "a campaign, choose a new name"
                )
        if problems:
            composition["problems"] = problems[:12]
            _save_campaign(store, job_id, state)
            return {
                "status": "rejected",
                "round": round_number,
                "problems": problems[:12],
                "stage": COMPOSE_STAGE,
            }
        claim = _composition_claim(state, manifest)
    campaign_root = store.job_dir(job_id) / CAMPAIGN_ROOT / campaign_id
    scanned: dict[str, Any] | None = None
    try:
        with experiment_compute_lock(
            store, job_id, label=f"evolution-compose:{job_id}"
        ):
            frames = _campaign_scan_frames(store, job_id, campaign_root, policy=policy)
            probe = next(iter(frames["train"].values()))
            try:
                validate_workspace_signals(defs, probe)
            except ValueError as exc:
                problems.append(str(exc))
            if not problems:
                scanned = _scan_signal_proposals(frames, defs, policy=policy)
    except ComputeLockBusy as exc:
        return {
            "status": "busy",
            "round": round_number,
            "reason": str(exc),
            "stage": COMPOSE_STAGE,
        }
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = _active_campaign(store, job_id)
        manifest = _campaign_manifest(store, job_id, campaign_id)
        if _composition_claim(state, manifest) != claim:
            return {
                "status": "stale",
                "round": round_number,
                "reason": "campaign state changed during the scan; submit again",
                "stage": str(state.get("stage") or ""),
            }
        composition = state["composition"]
        if problems or scanned is None:
            composition["problems"] = problems[:12]
            _save_campaign(store, job_id, state)
            return {
                "status": "rejected",
                "round": round_number,
                "problems": problems[:12],
                "stage": COMPOSE_STAGE,
            }
        try:
            pointers = _merge_compose_survivors(
                store,
                job_id,
                state,
                manifest,
                scanned["survivors"],
                round_number,
                limit=int(policy.get("signal_first_limit") or 10),
            )
        except ValueError as exc:
            # The scan was fine; the pack could not take the survivors. The
            # round is not consumed and the next prompt quotes why.
            composition["problems"] = [str(exc)]
            _save_campaign(store, job_id, state)
            return {
                "status": "rejected",
                "round": round_number,
                "problems": [str(exc)],
                "stage": COMPOSE_STAGE,
                "family_size": scanned["family_size"],
                "proposals": scanned["proposals"],
            }
        composition["rounds_used"] = round_number
        composition["problems"] = None
        composition["names"] = {
            **dict(composition.get("names") or {}),
            **{
                spec.name: {
                    "expression": str(spec.expression or ""),
                    "min_bars": int(spec.min_bars),
                }
                for spec in defs
            },
        }
        composition["history"] = [
            *list(composition.get("history") or []),
            {
                "round": round_number,
                "proposals": scanned["proposals"],
                "survivors": [entry["signal"] for entry in scanned["survivors"]],
                "merged": sum(pointer is not None for pointer in pointers),
                "family_size": scanned["family_size"],
            },
        ]
        if round_number >= int(composition.get("rounds_max") or 0):
            state["stage"] = "design"
        _save_campaign(store, job_id, state)
    store.append_journal(
        job_id,
        {
            "type": "evolution_campaign_composed",
            "campaign_id": campaign_id,
            "round": round_number,
            "proposals": len(proposals),
            "survivors": len(scanned["survivors"]),
            "merged": sum(pointer is not None for pointer in pointers),
            "family_size": scanned["family_size"],
            "stage": state["stage"],
        },
    )
    return {
        "status": "scanned",
        "round": round_number,
        "rounds_max": int(composition.get("rounds_max") or 0),
        "stage": state["stage"],
        "family_size": scanned["family_size"],
        "q_threshold": scanned["max_q"],
        "proposals": scanned["proposals"],
        "merged": sum(pointer is not None for pointer in pointers),
        "survivors": [
            {
                "name": entry["signal"],
                "signal_id": entry.get("signal_id"),
                "tier": entry["tier"],
                "pointer": pointer,
                "merged": pointer is not None,
                "symbol": entry["symbol"],
                "timeframe": entry["timeframe"],
                "horizon": entry["horizon"],
                "direction": entry["direction"],
                "regime": entry.get("regime"),
                "t_stat": entry["t_stat"],
                "q_value": entry["q_value"],
                "events": entry["events"],
                "gross_edge_bps": entry["gross_edge_bps"],
                "execution_hint": entry["execution_hint"],
            }
            for entry, pointer in zip(scanned["survivors"], pointers, strict=True)
        ],
    }


def _cross_sectional_instruction(block: Mapping[str, Any]) -> str:
    edges = [
        (index, column)
        for index, column in enumerate(block.get("columns") or [])
        if column.get("has_edge")
    ]
    if not block.get("available") or not edges:
        return ""
    parts = []
    for index, column in edges:
        best = max(
            (row for row in column.get("horizons") or [] if row.get("edge")),
            key=lambda row: abs(float(row.get("t_stat") or 0.0)),
            default=None,
        )
        if best is None:
            continue
        parts.append(
            f"{column['column']} (rank IC {float(best.get('mean_ic') or 0):+.3f}, t "
            f"{float(best.get('t_stat') or 0):+.1f} over {best.get('horizon')} bars; "
            f"/validated_signals/cross_sectional/columns/{index})"
        )
    if not parts:
        return ""
    return (
        "Cross-sectional: on the hour frame across "
        f"{', '.join(block.get('symbols') or [])} the ranking "
        + "; ".join(parts)
        + " orders relative forward returns — material for a rotation slot "
        "(long the top, short or flat the bottom), not a per-symbol trigger. "
    )


def _mechanism_instruction(
    job_id: str, pack: Mapping[str, Any], offered: Sequence[Mapping[str, Any]]
) -> str:
    """How to monetize a signal whose move is real but small: sweep resting
    entries with the grid and cite the chosen row."""
    grids = [g for g in (pack.get("mechanism_grids") or []) if isinstance(g, Mapping)]
    passive = [
        row
        for row in offered
        if row.get("execution_hint") in {"passive_only", "mechanism_required"}
    ]
    if not passive and not grids:
        return ""
    existing = "; ".join(
        f"/mechanism_grids/{index} {g.get('signal')} {g.get('side')} "
        f"{g.get('symbol')} {g.get('timeframe')}: "
        + (
            f"best offset {top[0]['entry_offset_atr']} ATR, ttl {top[0]['entry_ttl_bars']}, "
            f"target {top[0]['target_atr']} ATR, hold {top[0]['hold_bars']}, stop "
            f"{top[0]['stop_atr']} ATR, score {top[0]['score']:+.2f} "
            f"({top[0]['train']['trades']} train trades) at /mechanism_grids/{index}/top/0"
            if (top := list(g.get("top") or []))
            else f"no viable row of {g.get('evaluated')}"
        )
        for index, g in enumerate(grids)
    )
    return (
        "Mechanism grids: for a passive_only or mechanism_required signal, call "
        f'wayfinder_core_jobs(action="evolution_mechanism_grid", job_id="{job_id}", '
        'signal_ref="/validated_signals/<tier>/<i>", side="long"|"short") to sweep '
        "resting-entry mechanisms on it (entry offset in ATR, order life in bars, "
        "passive target, hold, stop; maker fee on passive legs, taker on stop and "
        "time exits; ranked by min(train, validation) Sharpe; seconds; a screen, "
        "never evidence). Cite the chosen row /mechanism_grids/<k>/top/<j> in the "
        "slot's evidence_refs beside the signal; the worker implements exactly that "
        "row. A status of busy means the compute budget is spent: end the turn "
        f"and call again next turn. {'Existing grids: ' + existing + '. ' if existing else ''}"
    )


def _resolve_signal_pointer(
    pack: Mapping[str, Any], signal_ref: str
) -> tuple[str, int, dict[str, Any]]:
    match = re.match(
        r"^/validated_signals/(signals|replicated)/(\d+)(?:/|$)", str(signal_ref)
    )
    if not match:
        raise ValueError(
            "signal_ref must point at /validated_signals/signals/<i> or "
            "/validated_signals/replicated/<i>"
        )
    rows = list((pack.get("validated_signals") or {}).get(match.group(1)) or [])
    index = int(match.group(2))
    if not 0 <= index < len(rows):
        raise ValueError(f"signal_ref {signal_ref!r} does not resolve in the pack")
    return match.group(1), index, dict(rows[index])


def _signal_def_for_entry(entry: Mapping[str, Any]) -> Any:
    if entry.get("library") in {"population", "workspace"} and entry.get("expression"):
        return compile_signal_expression(
            name=str(entry["signal"]),
            family=str(entry.get("family") or "workspace"),
            description="",
            min_bars=int(entry.get("min_bars") or 1),
            expression=str(entry["expression"]),
        )
    spec = signal_defs().get(str(entry["signal"]))
    if spec is None:
        raise ValueError(f"unknown library signal {entry['signal']!r}")
    return spec


def mechanism_grid(
    store: JobStore,
    job_id: str,
    *,
    signal_ref: str,
    side: str | None = None,
) -> dict[str, Any]:
    """Sweep passive-entry mechanisms on one offered signal and persist the
    ranked rows in the pack (finalist and alternatives on record). Claims
    the signal under the state lock, sweeps under the machine-wide compute
    lock, and commits only if the pack still holds that signal at that
    pointer. Runs in the compose and design stages; a repeat call for the
    same signal and side returns the stored grid; a busy compute budget
    returns ``busy``."""
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = _active_campaign(store, job_id)
        if state.get("stage") not in {COMPOSE_STAGE, "design"}:
            raise ValueError("mechanism grids run in the compose or design stage")
        campaign_id = str(state["campaign_id"])
        manifest = _campaign_manifest(store, job_id, campaign_id)
        policy = manifest.get("policy") or {}
        pack = store.read_json(job_id, str(state["diagnostic_pack"]), default={}) or {}
        grids = list(pack.get("mechanism_grids") or [])
        tier, index, entry = _resolve_signal_pointer(pack, signal_ref)
        pointer = f"/validated_signals/{tier}/{index}"
        chosen_side = str(side or entry.get("direction") or "long")
        wanted_id = str(entry.get("signal_id") or _signal_id(entry))
        # Cached by what the signal IS, not where it sits: a later round
        # prepends rows and moves every positional pointer.
        for position, record in enumerate(grids):
            if (
                record.get("signal_id") == wanted_id
                and record.get("side") == chosen_side
            ):
                return {
                    **record,
                    "signal_ref": pointer,
                    "status": "ok",
                    "pointer": f"/mechanism_grids/{position}",
                    "cached": True,
                }
        if len(grids) >= MECHANISM_GRID_MAX:
            raise ValueError(
                f"at most {MECHANISM_GRID_MAX} mechanism grids per campaign"
            )
        identity = _signal_identity(entry)
    campaign_root = store.job_dir(job_id) / CAMPAIGN_ROOT / campaign_id
    try:
        with experiment_compute_lock(
            store, job_id, label=f"evolution-mechanism-grid:{job_id}"
        ):
            result = _sweep_signal_mechanism(
                store,
                job_id,
                campaign_root,
                entry,
                side=chosen_side,
                policy=policy,
            )
    except ComputeLockBusy as exc:
        return {"status": "busy", "signal_ref": pointer, "reason": str(exc)}
    record = {
        "signal_ref": pointer,
        "signal_id": str(entry.get("signal_id") or _signal_id(entry)),
        "symbol": entry["symbol"],
        "signal": entry["signal"],
        "timeframe": entry["timeframe"],
        "regime": entry.get("regime"),
        "library": entry.get("library"),
        "created_at": utc_now_iso(),
        **result,
    }
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = _active_campaign(store, job_id)
        if str(state.get("campaign_id") or "") != campaign_id:
            return {
                "status": "stale",
                "signal_ref": pointer,
                "reason": "campaign changed",
            }
        manifest = _campaign_manifest(store, job_id, campaign_id)
        root = store.job_dir(job_id)
        pack_path = root / str(state["diagnostic_pack"])
        pack = store.read_json(job_id, str(state["diagnostic_pack"]), default={}) or {}
        try:
            _, _, current = _resolve_signal_pointer(pack, pointer)
        except ValueError:
            current = {}
        if _signal_identity(current) != identity:
            return {
                "status": "stale",
                "signal_ref": pointer,
                "reason": "the pack changed during the sweep; the pointer no longer "
                "names that signal, submit again",
            }
        grids = list(pack.get("mechanism_grids") or [])
        for position, existing in enumerate(grids):
            if (
                existing.get("signal_id") == wanted_id
                and existing.get("side") == chosen_side
            ):
                return {
                    **existing,
                    "signal_ref": pointer,
                    "status": "ok",
                    "pointer": f"/mechanism_grids/{position}",
                    "cached": True,
                }
        grids.append(record)
        pack["mechanism_grids"] = grids
        pack = fit_diagnostic_pack(pack)
        try:
            _, _, after_fit = _resolve_signal_pointer(pack, pointer)
        except ValueError:
            after_fit = {}
        if _signal_identity(after_fit) != identity:
            return {
                "status": "rejected",
                "signal_ref": pointer,
                "reason": "persisting the grid would push the pack past its budget "
                "and drop the signal it cites; nothing was persisted",
            }
        atomic_write_json(pack_path, pack)
        manifest["diagnostic_pack"] = {
            **dict(manifest.get("diagnostic_pack") or {}),
            "sha256": _file_hash(pack_path),
        }
        atomic_write_json(root / str(state["manifest"]), manifest)
    store.append_journal(
        job_id,
        {
            "type": "evolution_campaign_mechanism_grid",
            "campaign_id": campaign_id,
            "signal_ref": pointer,
            "side": chosen_side,
            "evaluated": result["evaluated"],
            "viable": result["viable"],
            "top_score": (result["top"][0]["score"] if result["top"] else None),
        },
    )
    return {
        **record,
        "status": "ok",
        "pointer": f"/mechanism_grids/{len(grids) - 1}",
        "cached": False,
    }


def _signal_identity(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        entry.get("signal"),
        entry.get("symbol"),
        entry.get("timeframe"),
        entry.get("horizon"),
        entry.get("regime"),
    )


def _sweep_signal_mechanism(
    store: JobStore,
    job_id: str,
    campaign_root: Path,
    entry: Mapping[str, Any],
    *,
    side: str,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    """The grid itself: the entry's symbol resampled to its timeframe, the
    def rebuilt (library, population or proposal), the regime gate applied,
    the job's costs."""
    frames = _campaign_scan_frames(store, job_id, campaign_root, policy=policy)
    symbol = str(entry["symbol"])
    base = frames["train"].get(symbol)
    if base is None:
        raise ValueError(f"symbol {symbol!r} has no scan frame")
    timeframe = str(entry["timeframe"])
    bar_seconds = int(frames["bar_seconds"])
    rule_seconds = int(bar_interval_seconds(timeframe) or bar_seconds)
    bars = resample_ohlcv(base, rule_seconds, bar_seconds=bar_seconds)
    spec = _signal_def_for_entry(entry)
    column = build_signal_frame(bars, [spec], include_canonical=False)[spec.name]
    source = entry.get("regime_source")
    if source and str(source) in bars.columns:
        label = str(entry.get("regime") or "").split("=", 1)[-1]
        codes = frames["condition_features"].get(str(source)) or {}
        code = next((c for c, name in codes.items() if name == label), None)
        if code is not None:
            column = column & (
                pd.to_numeric(bars[str(source)], errors="coerce") == code
            )
    costs = GridCosts(
        maker_fee_bps=float(frames["maker_round_trip_bps"]) / 2.0,
        taker_fee_bps=float(frames["scan_kwargs"]["fee_bps"]),
        slippage_bps=float(frames["scan_kwargs"]["slippage_bps"]),
    )
    return passive_entry_grid(
        bars,
        column,
        side=side,
        bar_seconds=rule_seconds,
        costs=costs,
        min_trades=int(policy.get("mechanism_grid_min_trades") or 30),
        top=int(policy.get("mechanism_grid_top") or 10),
    )


def _cited_mechanisms(
    store: JobStore, job_id: str, manifest: Mapping[str, Any], refs: Sequence[str]
) -> list[dict[str, Any]]:
    """The grid rows a hypothesis cites, resolved from the frozen pack so the
    worker's candidate.json carries the exact mechanism to implement."""
    wanted: list[tuple[int, int]] = []
    for ref in refs:
        match = _MECHANISM_REF_RE.match(str(ref))
        if match and (int(match.group(1)), int(match.group(2))) not in wanted:
            wanted.append((int(match.group(1)), int(match.group(2))))
    if not wanted:
        return []
    pack_path = str((manifest.get("diagnostic_pack") or {}).get("path") or "")
    pack = store.read_json(job_id, pack_path, default={}) if pack_path else {}
    grids = list((pack or {}).get("mechanism_grids") or [])
    cited: list[dict[str, Any]] = []
    for grid_index, row_index in wanted:
        if not 0 <= grid_index < len(grids):
            continue
        record = grids[grid_index]
        rows = list(record.get("top") or [])
        if not 0 <= row_index < len(rows):
            continue
        cited.append(
            {
                "pointer": f"/mechanism_grids/{grid_index}/top/{row_index}",
                # The stored pointer is where the signal sat when the grid ran;
                # a later round may have moved it. Export where it is now.
                "signal_ref": _locate_signal_pointer(
                    (pack or {}).get("validated_signals") or {},
                    str(record.get("signal_id") or ""),
                ),
                "signal_id": record.get("signal_id"),
                "symbol": record.get("symbol"),
                "signal": record.get("signal"),
                "timeframe": record.get("timeframe"),
                "side": record.get("side"),
                "regime": record.get("regime"),
                "costs": record.get("costs"),
                **dict(rows[row_index]),
            }
        )
    return cited


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
    raw: dict[str, Any],
    *,
    manifest: dict[str, Any],
    diagnostic_pack: dict[str, Any],
    extension: bool = False,
    reserved_ids: Collection[str] = (),
) -> dict[str, Any]:
    """The design's shape rules. ``extension`` validates the redesign
    checkpoint's replacement slots: the same per-hypothesis, per-slot and
    citation rules, no wildcards, and none of the whole-design allocation
    rules (those were met by the accepted design)."""
    if not isinstance(raw, dict):
        raise ValueError("campaign_design must be an object")
    hypotheses = raw.get("hypotheses")
    slots = raw.get("slots")
    if extension:
        if not isinstance(hypotheses, list) or not 1 <= len(hypotheses) <= 3:
            raise ValueError("a redesign carries 1-3 replacement hypotheses")
        limit = int(manifest["policy"].get("redesign_slots") or 3)
        if not isinstance(slots, list) or not 1 <= len(slots) <= limit:
            raise ValueError(f"a redesign carries 1-{limit} replacement slots")
    else:
        if not isinstance(hypotheses, list) or not 3 <= len(hypotheses) <= 5:
            raise ValueError("campaign design requires 3-5 grounded hypotheses")
        expected_slots = int(manifest["policy"]["generated_programs"])
        if not isinstance(slots, list) or len(slots) != expected_slots:
            raise ValueError(f"campaign design requires exactly {expected_slots} slots")
    reserved = {str(item) for item in reserved_ids}
    refuted_families = {
        str(item.get("family") or "").strip()
        for item in (
            (diagnostic_pack.get("research_context") or {}).get("refuted_families")
            or []
        )
        if item.get("family")
    }
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
        addresses_refutation = str(hypothesis.get("addresses_refutation") or "").strip()
        raw_new_refs = hypothesis.get("new_evidence_refs") or []
        if family in refuted_families:
            if not addresses_refutation:
                raise ValueError(
                    f"hypothesis {hypothesis_id} re-proposes refuted family "
                    "without addresses_refutation"
                )
            if (
                not isinstance(raw_new_refs, list)
                or not raw_new_refs
                or not all(
                    isinstance(ref, str) and ref.strip() in refs for ref in raw_new_refs
                )
            ):
                raise ValueError(
                    f"hypothesis {hypothesis_id} must name new_evidence_refs "
                    "already present in evidence_refs"
                )
        normalized = {
            "id": hypothesis_id,
            "family": family[:120],
            "causal_mechanism": mechanism[:800],
            "falsifier": falsifier[:500],
            "evidence_refs": refs[:12],
            **(
                {
                    "addresses_refutation": addresses_refutation[:500],
                    "new_evidence_refs": [str(ref).strip() for ref in raw_new_refs[:6]],
                }
                if addresses_refutation
                else {}
            ),
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
        "policy_kernel",
    }
    available_policy_refs = [
        str(row.get("pointer") or "")
        for row in (diagnostic_pack.get("policy_scan") or {}).get("survivors") or []
        if (row.get("recipe") or {}).get("module")
    ]
    starters_by_id = {
        str(item.get("starter_id") or ""): item
        for item in manifest.get("starter_seeds") or []
        if item.get("starter_id")
    }
    available_starter_ids = set(starters_by_id)
    available_research_seed_ids = {
        str(item.get("seed_id") or "")
        for item in manifest.get("research_seeds") or []
        if item.get("seed_id")
    }
    normalized_slots: list[dict[str, Any]] = []
    regime_context = manifest.get("regime_context") or {}
    specialist_design = bool(
        manifest["policy"].get("regime_specialist_enabled")
        and regime_context.get("available")
    )
    seen_slot_ids: set[str] = set()
    for index, slot in enumerate(slots, start=1):
        if not isinstance(slot, dict):
            raise ValueError("every design slot must be an object")
        slot_id = str(slot.get("slot_id") or f"s{index:02d}").strip()
        if slot_id in seen_slot_ids or slot_id in reserved:
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
            starter_snapshot = starters_by_id[starter_seed_id]
            if starter_snapshot.get("compatible") is False:
                raise ValueError(
                    f"slot {slot_id} starter_seed_id {starter_seed_id!r} "
                    f"{starter_snapshot.get('incompatibility_reason')}"
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
        policy_ref = str(slot.get("policy_ref") or "").strip()
        policy_survivor: dict[str, Any] | None = None
        if policy_ref or source == "policy_kernel":
            if source != "policy_kernel":
                raise ValueError(
                    f"slot {slot_id} policy_ref requires parent_source policy_kernel"
                )
            policy_survivor = _policy_survivor_from_pack(diagnostic_pack, policy_ref)
            if policy_survivor is None:
                raise ValueError(
                    f"slot {slot_id} policy_ref must name a policy-scan survivor "
                    f"with a kernel recipe; available: {available_policy_refs}"
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
        if policy_survivor is not None:
            normalized_slot["policy_ref"] = policy_ref
            normalized_slot["policy_id"] = str(policy_survivor.get("policy_id") or "")
        raw_regimes = slot.get("target_regimes")
        if specialist_design:
            if (
                not isinstance(raw_regimes, list)
                or isinstance(raw_regimes, str)
                or not 1 <= len(raw_regimes) <= 2
            ):
                raise ValueError(f"slot {slot_id} requires one or two target_regimes")
            target_regimes = list(
                dict.fromkeys(str(value).strip() for value in raw_regimes)
            )
            invalid_regimes = sorted(set(target_regimes) - set(REGIME_LABELS))
            if invalid_regimes:
                raise ValueError(
                    f"slot {slot_id} has unknown target_regimes: "
                    + ", ".join(invalid_regimes)
                )
            normalized_slot["target_regimes"] = target_regimes
        if starter_seed_id:
            normalized_slot["starter_seed_id"] = starter_seed_id
        if research_seed_id:
            normalized_slot["research_seed_id"] = research_seed_id
        normalized_slots.append(normalized_slot)
    wildcard_count = sum(bool(slot["wildcard"]) for slot in normalized_slots)
    if extension:
        if wildcard_count:
            raise ValueError("a redesign cannot add wildcard slots")
    elif wildcard_count != int(manifest["policy"].get("wildcard_slots") or 0):
        raise ValueError("campaign design has the wrong number of wildcard slots")
    grounded = [slot for slot in normalized_slots if not slot["wildcard"]]
    signal_block = diagnostic_pack.get("validated_signals") or {}
    offered = list(signal_block.get("signals") or [])
    # Signals that survived the family-corrected scan are the evidence a
    # grounded free-form slot must build on; with none, the replicated tier
    # (real, fold-stable, not cost-gated) is what it must build on instead.
    # Narratives are not evidence either way.
    tier, prefix = (
        ("validated", "/validated_signals/signals/")
        if offered
        else ("replicated", "/validated_signals/replicated/")
    )
    pool = offered or list(signal_block.get("replicated") or [])
    # A policy-scan survivor is evidence of the same standing: a portfolio
    # policy consistent on both scan windows, instantiable from a kernel.
    policy_pool = list(
        (diagnostic_pack.get("policy_scan") or {}).get("survivors") or []
    )
    accepted_prefixes = (prefix, *(("/policy_scan/survivors/",) if policy_pool else ()))
    if pool or policy_pool:
        for slot in grounded:
            if slot["parent_source"] not in {"de_novo", "research_context"}:
                continue
            refs = list(
                (hypothesis_by_id.get(str(slot.get("hypothesis_id") or "")) or {}).get(
                    "evidence_refs"
                )
                or []
            )
            if not any(
                str(ref).startswith(accepted)
                for ref in refs
                for accepted in accepted_prefixes
            ):
                names = ", ".join(
                    f"{row.get('signal')} {row.get('symbol')} {row.get('timeframe')}"
                    for row in pool[:6]
                )
                raise ValueError(
                    f"slot {slot['slot_id']} must build on a {tier} signal"
                    + (" or a policy-scan survivor" if policy_pool else "")
                    + ": its hypothesis cites none of "
                    + ", ".join(f"{accepted}<i>" for accepted in accepted_prefixes)
                    + f" while the pack offers {len(pool) + len(policy_pool)} "
                    f"({names})"
                )
    sources = {str(slot["parent_source"]) for slot in grounded}
    if extension:
        if sources & {"incumbent"}:
            raise ValueError("a redesign cannot add an incumbent slot")
    else:
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
    if specialist_design:
        represented = {
            regime
            for slot in normalized_slots
            for regime in slot.get("target_regimes") or []
        }
        if len(represented) < 2:
            raise ValueError("campaign design must span at least two regime cells")
        counter = str(regime_context["counter_regime"])
        if counter not in represented:
            raise ValueError(
                f"campaign design requires a counter-regime slot for {counter}"
            )
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
    limit = _program_budget(state, policy)
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
        requested_policy=_policy_survivor(
            store, job_id, manifest, str(design_slot.get("policy_ref") or "")
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
    target_regimes = list(design_slot.get("target_regimes") or [])
    if target_regimes:
        job_data = _load_job_yaml(candidate_root)
        params = dict(job_data.get("execution_params") or {})
        params["target_regimes"] = target_regimes
        job_data["execution_params"] = params
        atomic_write_text(
            candidate_root / "job.yaml", yaml.safe_dump(job_data, sort_keys=False)
        )
    neighborhood: dict[str, Any] | None = None
    if (
        source == "incumbent"
        and chosen_mutation == "parameter"
        and bool((manifest.get("policy") or {}).get("incumbent_neighborhood_search"))
    ):
        neighborhood = _incumbent_neighborhood(
            store, job_id, state, candidate_root, policy=manifest.get("policy") or {}
        )
    seed_revision = compute_workspace_revision(candidate_root)
    reference_relative = (
        f"{CAMPAIGN_ROOT}/{state['campaign_id']}/references/{candidate_id}"
    )
    # A seed's screen reference is the incumbent, never the seed itself: a
    # starter or research slot exists to ask whether an audited mechanism
    # beats the book, and paired against itself an unchanged winner scores a
    # zero delta on every day and can never pass.
    reference_source = (
        f"{CAMPAIGN_ROOT}/{state['campaign_id']}/source"
        if source in {"starter_seed", "research_seed"}
        else relative
    )
    copy_job_bundle(
        store.job_dir(job_id) / reference_source,
        store.job_dir(job_id) / reference_relative,
    )
    reference_revision = compute_workspace_revision(
        store.job_dir(job_id) / reference_relative
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
        "policy_ref": (parent_plan.get("policy") or {}).get("pointer"),
        "policy_id": (parent_plan.get("policy") or {}).get("policy_id"),
        "secondary_parent_bundle": (parent_plan.get("secondary") or {}).get("bundle"),
        "mutation_kind": chosen_mutation,
        "neighborhood": neighborhood,
        "forced_jump": forced_jump,
        "bundle": relative,
        "warmup_bars": seeded_window,
        "seed_revision": seed_revision,
        "reference_bundle": reference_relative,
        "reference_revision": reference_revision,
        "evidence_reset": source in {"starter_seed", "research_seed", "policy_kernel"},
        "design_slot_id": design_slot.get("slot_id"),
        "hypothesis_id": design_slot.get("hypothesis_id"),
        "wildcard": bool(design_slot.get("wildcard")),
        "target_regimes": target_regimes,
        "evidence_refs": list(hypothesis.get("evidence_refs") or []),
        "signal_refs": _cited_signals(
            store, job_id, manifest, list(hypothesis.get("evidence_refs") or [])
        ),
        "mechanism_refs": _cited_mechanisms(
            store, job_id, manifest, list(hypothesis.get("evidence_refs") or [])
        ),
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
            "target_regimes": candidate.get("target_regimes"),
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
        status_before_claim = str(candidate["status"])
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
        if outcome.get("status") == "rejected_submission":
            candidate["status"] = status_before_claim
            candidate["submission_rejection"] = {
                "error": str((outcome.get("evidence") or {}).get("error") or ""),
                "at": utc_now_iso(),
                "attempt_charged": False,
            }
            candidate.pop("evaluation_claim_id", None)
            candidate.pop("evaluation_claimed_at", None)
            _save_campaign(store, job_id, state)
            return dict(candidate)
        candidate.pop("submission_rejection", None)
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


def _rejected_submission(error: str) -> dict[str, Any]:
    """A deterministic authoring mistake: no simulation ran, so no attempt is
    charged; the worker fixes the bundle and resubmits."""
    return {"status": "rejected_submission", "evidence": {"error": error[:500]}}


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
        return _rejected_submission(str(exc))
    sizing = sorted(_SIZING_DIMENSIONS.intersection(search_space or {}))
    if sizing:
        return _rejected_submission(
            f"sizing dimensions are not search dimensions: {sizing}; the finalist "
            "gate sizes a strategy to the risk ceiling mechanically"
        )
    report = validate_execution_job(job_id, candidate_dir=candidate_root, store=store)
    contract = next(
        (
            check
            for check in report.get("checks") or []
            if check.get("name") in _CONTRACT_CHECKS and not check.get("passed")
        ),
        None,
    )
    if contract is not None:
        # A deterministic authoring mistake with a mechanical fix: no
        # simulation ran, so no attempt is charged.
        return _rejected_submission(
            str(contract.get("hint") or contract.get("error") or contract.get("name"))
        )
    if not _candidate_validation_passed(report):
        return {"status": "invalid", "evidence": {"validation": report}}
    revision = compute_workspace_revision(candidate_root)
    manifest = (
        store.read_json(
            job_id, f"{CAMPAIGN_ROOT}/{campaign_id}/manifest.json", default={}
        )
        or {}
    )
    complexity = _bundle_complexity(store, job_id, candidate_root)
    regime_branches = _regime_branches(store, job_id, candidate_root)
    complexity_budget = _complexity_budget(
        manifest.get("policy") or {},
        _incumbent_complexity(store, job_id, campaign_id),
        regime_branches=regime_branches,
    )
    if int(complexity.get("comparisons") or 0) > complexity_budget:
        error = (
            f"strategy has {complexity['comparisons']} comparisons against a "
            f"budget of {complexity_budget}"
            + (f" ({regime_branches} regime branches)" if regime_branches > 1 else "")
            + "; simplify before simulation"
        )
        return {
            "status": "low_fidelity_rejected",
            "evidence": error,
            "quick_simulation_ran": False,
            "complexity": complexity,
            "postmortem": {
                "viable": False,
                "primary_failure": "complexity_over_budget",
                "failure_codes": ["complexity_over_budget"],
                "behavior_diff": {"material_change": False},
                "repair_context": {
                    "error": error,
                    "complexity": complexity,
                    "complexity_budget": complexity_budget,
                    "regime_branches": regime_branches,
                },
            },
        }
    if (
        revision == _source_baseline_revision(manifest, candidate)
        and search_space is None
    ):
        return _rejected_submission(
            "candidate bundle is identical to its source revision"
        )
    policy = manifest.get("policy") or {}
    tuning_preview: dict[str, Any] | None = None
    behavior_preview: dict[str, Any] | None = None
    sequence_report: dict[str, Any] | None = None
    previous_preview: dict[str, Any] | None = None
    repair_gate = False
    try:
        subject = _load_subject(store, job_id, candidate_root, campaign_id=campaign_id)
        train = _discovery_dataset(subject["dataset"], policy)
        # The candidate's recent window is the reference's: one slice set,
        # sized by policy, so paired deltas, trade counts and the macro label
        # describe the same bars.
        screen_slices = _policy_screen_slices(train, policy)
        _, quick = screen_slices[0]
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
        # A stuck state machine (armed once, never fires) is invisible to
        # isolated-bar probes; replay consecutive bars with persistent state
        # before the 10k-bar screen. A first attempt is never rejected on it
        # (a sparse, alive mechanism may not fire in the replayed tail); a
        # no-trade repair must show the replay moved.
        attempts = list(candidate.get("attempts") or [])
        previous_codes = set(
            ((attempts[-1].get("postmortem") or {}).get("failure_codes") or [])
            if attempts
            else []
        )
        repair_gate = bool(previous_codes & {"no_trades", "no_progress_preview"})
        previous_preview = (
            ((attempts[-1].get("outcome") or {}).get("sequence_preview"))
            if attempts
            else None
        )
        if repair_gate or (
            candidate.get("mutation_kind") != "parameter"
            and bool(policy.get("sequence_preview_enabled", True))
        ):
            sequence_report = sequence_preview(
                subject["script"],
                quick,
                subject["spec"],
                params,
                bars=int(policy.get("sequence_preview_bars") or 2_000),
            )
            if repair_gate and not preview_progress(previous_preview, sequence_report):
                reason = (
                    "sequence preview shows no new intent or state transition "
                    "since the previous no-trade attempt"
                )
                return {
                    "status": "low_fidelity_rejected",
                    "evidence": reason,
                    "quick_simulation_ran": False,
                    "sequence_preview": sequence_report,
                    "postmortem": {
                        "viable": False,
                        "primary_failure": "no_progress_preview",
                        "failure_codes": ["no_progress_preview", "no_trades"],
                        "behavior_diff": {"material_change": False},
                        "repair_context": {
                            "error": reason,
                            "sequence_preview": sequence_report,
                            "previous_preview": previous_preview,
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
                        "repair_context": {
                            "error": error,
                            "dead_params": _typed_search_dimensions(search_space),
                        },
                    },
                }
        result = simulate_execution(subject["script"], quick, subject["spec"], params)
        screen_results: dict[str, Any] = {"recent": result}
        screen_macros = {
            label: _slice_macro_regime(dataset) for label, dataset in screen_slices
        }
        if result.validation.get("execution_valid"):
            for label, dataset in screen_slices[1:]:
                screen_results[label] = simulate_execution(
                    subject["script"], dataset, subject["spec"], params
                )
        if (
            candidate.get("mutation_kind") == "parameter"
            and search_space is not None
            and result.validation.get("execution_valid")
            and _decision_trade_count(result.stats) > 0
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
        "complexity": complexity,
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
        receipt["round_trip_cost_bps"] = _round_trip_cost_bps(params)
        receipt["maker_round_trip_bps"] = maker_round_trip_bps(params)
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
            max_outside_loss_pct=float(
                (
                    load_constitution(candidate_root)
                    .get("evaluation", {})
                    .get("regime", {})
                ).get("max_out_of_regime_loss_pct", 0.02)
            ),
            incumbent_economics=_incumbent_economics(store, job_id, campaign_id),
            cost_bleed_fee_multiple=float(policy.get("cost_bleed_fee_multiple") or 3.0),
            cost_bleed_fee_pct_of_capital_30d=float(
                policy.get("cost_bleed_fee_pct_of_capital_30d") or 0.10
            ),
        )
        _apply_screen_verdict(
            common["postmortem"],
            screen_results,
            reference,
            attempt_index=int(candidate.get("attempt_count") or 0) + 1,
            policy=policy,
            manifest=manifest,
            screen_macros=screen_macros,
            leader_days=_leader_day_states(
                store.job_dir(job_id)
                / CAMPAIGN_ROOT
                / campaign_id
                / CAMPAIGN_DATA_ROOT
                / DEFAULT_FEATURES_PATH
            ),
        )
        _stamp_screen_progress(
            common["postmortem"], attempts[-1].get("postmortem") if attempts else None
        )
    if search_space is None:
        common["tuning_skip_reason"] = "no_typed_search_space"
    if tuning_preview is not None:
        common["tuning_preview"] = tuning_preview
    if behavior_preview is not None:
        common["behavior_preview"] = behavior_preview
    if sequence_report is not None:
        common["sequence_preview"] = sequence_report
    if _decision_trade_count(result.stats) <= 0:
        postmortem = common.get("postmortem")
        if isinstance(postmortem, dict) and sequence_report is not None:
            context = postmortem.setdefault("repair_context", {})
            context["sequence_preview"] = sequence_report
            if previous_preview:
                context["previous_preview"] = previous_preview
            if repair_gate:
                postmortem.setdefault("progress_from_previous", {})[
                    "preview_progress"
                ] = True
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


def _stamp_screen_progress(
    postmortem: dict[str, Any], previous_postmortem: Mapping[str, Any] | None
) -> None:
    """A repair earns more budget on the screen's own metric: the lower
    bound of the slice that was worst last attempt must rise. Fill-count
    differences and hairline net-return deltas are not progress."""
    if not previous_postmortem:
        return
    previous = (previous_postmortem.get("screen") or {}).get("slices") or {}
    current = (postmortem.get("screen") or {}).get("slices") or {}
    scored = {
        str(label): float(row["lcb"])
        for label, row in previous.items()
        if isinstance(row, Mapping) and row.get("lcb") is not None
    }
    if not scored:
        return
    label = min(scored, key=lambda item: scored[item])
    latest = (current.get(label) or {}).get("lcb")
    if latest is None:
        return
    progress = postmortem.setdefault("progress_from_previous", {})
    progress["screen_lcb_slice"] = label
    progress["screen_lcb_delta"] = round(float(latest) - scored[label], 6)


def _apply_screen_verdict(
    postmortem: dict[str, Any],
    screen_results: Mapping[str, Any],
    reference: Mapping[str, Any],
    *,
    attempt_index: int,
    policy: Mapping[str, Any],
    manifest: Mapping[str, Any],
    screen_macros: Mapping[str, Mapping[str, Any]] | None = None,
    leader_days: Mapping[str, int] | None = None,
) -> None:
    """Overlay the multi-slice, significance-aware screen on the postmortem."""
    step = policy.get("screen_confidence_step")
    confidence = _screen_confidence(
        attempt_index,
        base=float(policy.get("screen_confidence_base") or 0.70),
        step=0.0 if step is None else float(step),
    )
    reference_slices = reference.get("slices") or {}
    reports = {
        label: _screen_slice_report(
            result,
            (reference_slices.get(label) or {}).get("daily_returns"),
            confidence=confidence,
            leader_days=leader_days,
        )
        for label, result in screen_results.items()
    }
    for label, macro in (screen_macros or {}).items():
        if label in reports:
            reports[label]["macro_regime"] = macro.get("label")
            reports[label]["macro_median_return"] = macro.get("median_return")
            reports[label]["window"] = [macro.get("start"), macro.get("end")]
    min_trades = _screen_min_trades(policy, manifest, screen_results["recent"])
    pooled = [
        value for report in reports.values() for value in report.pop("deltas", [])
    ]
    candidate_economics = (postmortem.get("economics") or {}).get("candidate") or {}
    verdict = _screen_verdict(
        reports,
        min_trades=min_trades,
        pooled_lcb=_screen_lcb(pooled, confidence),
        max_slice_loss=float(
            policy.get("screen_slice_max_loss") or SCREEN_SLICE_MAX_LOSS
        ),
        cost_coverage=candidate_economics.get("cost_coverage"),
        cost_hurdle=float(policy.get("cost_hurdle_multiple") or COST_HURDLE_MULTIPLE),
        cost_basis=candidate_economics.get("cost_basis"),
    )
    postmortem["screen"] = {
        "confidence": confidence,
        "min_trades": min_trades,
        **verdict,
    }
    code = verdict["code"]
    if code and code not in postmortem["failure_codes"]:
        postmortem["failure_codes"].insert(0, code)
        postmortem["primary_failure"] = code
    if not verdict["passed"]:
        postmortem["viable"] = False


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
    # Records from before seeds were paired against the incumbent carry no
    # reference_revision; their reference was the seed.
    expected_revision = candidate.get("reference_revision") or candidate.get(
        "seed_revision"
    )
    if cached.get("revision") == expected_revision and "slices" in cached:
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
    if compute_workspace_revision(reference) != expected_revision:
        raise ValueError("candidate reference bundle revision mismatch")
    subject = _load_subject(store, job_id, reference, campaign_id=campaign_id)
    policy = _campaign_policy(store, job_id, campaign_id)
    train = _discovery_dataset(subject["dataset"], policy)
    params, _, _ = _calibrated_params(store, job_id, subject)
    slices = _policy_screen_slices(train, policy)
    _, quick = slices[0]
    result = simulate_execution(subject["script"], quick, subject["spec"], params)
    receipt = result_receipt(
        result,
        revision=compute_workspace_revision(reference),
        objective=_objective(result.stats, params),
        behavior=_behavior(result, quick, subject["spec"]),
    )
    receipt["round_trip_cost_bps"] = _round_trip_cost_bps(params)
    receipt["maker_round_trip_bps"] = maker_round_trip_bps(params)
    receipt["slices"] = {
        "recent": {"daily_returns": daily_log_returns(result.equity_curve)}
    }
    for label, dataset in slices[1:]:
        extra = simulate_execution(subject["script"], dataset, subject["spec"], params)
        receipt["slices"][label] = {
            "daily_returns": daily_log_returns(extra.equity_curve),
            "stats": {
                key: extra.stats.get(key)
                for key in ("net_return", "trade_count")
                if extra.stats.get(key) is not None
            },
        }
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
    if not bool(postmortem.get("viable")):
        manifest = store.read_json(job_id, str(state.get("manifest") or ""), default={})
        postmortem["repair_work_order"] = build_repair_work_order(
            postmortem,
            policy,
            params=dict(_load_job_yaml(candidate_root).get("execution_params") or {}),
            min_fills_per_day=_min_fills_per_day(
                policy, (manifest or {}).get("dataset")
            ),
        )
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
    if attempt_index > 1:
        counts["repairs"] = int(counts.get("repairs") or 0) + 1
    max_attempts = int(policy.get("max_attempts_per_idea") or 3)
    global_cap = int(
        policy.get("max_quick_attempts")
        or int(policy["generated_programs"]) * max_attempts
    )
    before_drain = _campaign_now() < _parse(state["deadline_at"]) - CAMPAIGN_DRAIN
    global_room = counts["quick_attempts"] < global_cap
    viable = bool(postmortem.get("viable"))
    if _screen_before_repair(policy) and attempt_index == 1:
        # A candidate's first attempt is its screen.  Non-viable candidates
        # park until the focus phase ranks them; nothing is rejected on a
        # single screen, and no repair budget is spent before every slot has
        # been screened.
        keep_open = before_drain and global_room and not viable
    else:
        cap = _attempt_cap(state, candidate, policy)
        keep_open = bool(
            before_drain
            and attempt_index < cap
            and global_room
            and not viable
            and (attempt_index == 1 or attempt_made_progress(postmortem))
        )
    if keep_open:
        candidate["status"] = "repair_pending"
        candidate["evidence"] = postmortem
        atomic_write_json(candidate_root / "candidate.json", candidate)
        _stamp_focus(state, policy)
        return
    _close_designed_candidate(store, job_id, state=state, candidate=candidate)
    _stamp_focus(state, policy)


def _screen_before_repair(policy: Mapping[str, Any]) -> bool:
    return bool(policy.get("screen_before_repair", True))


def _program_budget(state: Mapping[str, Any], policy: Mapping[str, Any]) -> int:
    """Initial slots plus the replacement slots a redesign added."""
    extra = int((state.get("redesign") or {}).get("extra_slots") or 0)
    return int(policy.get("generated_programs") or 0) + extra


def _screen_complete(state: Mapping[str, Any], policy: Mapping[str, Any]) -> bool:
    return len(state.get("candidates") or []) >= _program_budget(state, policy)


def _redesign_due(state: Mapping[str, Any], policy: Mapping[str, Any]) -> bool:
    """One bounded global learning turn: after every initial slot has had
    its screen attempt and before the focus repairs, the designer reads the
    whole table once."""
    if not bool(policy.get("redesign_checkpoint", True)):
        return False
    if state.get("stage") != "generate" or state.get("redesign") is not None:
        return False
    # Only a designed campaign has a design to extend; the legacy flow and
    # the depth-first control arm keep their mechanical allocation.
    if not state.get("design") or not _screen_before_repair(policy):
        return False
    candidates = list(state.get("candidates") or [])
    if len(candidates) < int(policy.get("generated_programs") or 0):
        return False
    if any(
        item.get("status") in {"prepared", "quick_running", "quick_failed"}
        for item in candidates
    ):
        return False
    # Nothing open means nothing to abandon or keep: the campaign finalizes.
    if not any(item.get("status") == "repair_pending" for item in candidates):
        return False
    counts = state.get("counts") or {}
    used = int(counts.get("quick_attempts") or 0)
    return used < int(policy.get("max_quick_attempts") or 0)


def _attempt_cap(
    state: Mapping[str, Any], candidate: Mapping[str, Any], policy: Mapping[str, Any]
) -> int:
    """Per-candidate attempt ceiling for the current allocation phase."""
    if not _screen_before_repair(policy):
        return int(policy.get("max_attempts_per_idea") or 3)
    if not _screen_complete(state, policy):
        return 1
    focus_ids = {item["candidate_id"] for item in _focus_candidates(state, policy)}
    if candidate.get("candidate_id") in focus_ids:
        return int(policy.get("focus_attempts_per_candidate") or 6)
    return 1


_FIXABILITY = {
    "cost_bleed": 2,
    "fees_erased_edge": 2,
    "screen_regime_dependent": 2,
    "screen_slice_loss_bound": 2,
    "cost_not_covered": 2,
    "screen_edge_not_significant": 1,
    "complexity_over_budget": 1,
    "negative_after_costs": 1,
    "negative_in_target_regime": 1,
    "out_of_regime_loss_budget": 1,
    "no_trades": 1,
    "activity_below_floor": 1,
    "activity_collapse": 1,
}


def _focus_rank(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    attempts = candidate.get("attempts") or []
    latest = attempts[-1] if attempts else {}
    postmortem = latest.get("postmortem") or {}
    codes = list(postmortem.get("failure_codes") or [])
    fixability = _FIXABILITY.get(str(postmortem.get("primary_failure") or ""), 0)
    # A cost-bleed candidate whose gross PnL was positive is the most fixable
    # shape in the pool: the cadence, not the signal, is the defect.
    if "cost_bleed" in codes and "fees_erased_edge" in codes:
        fixability = 3
    progress = float(
        (postmortem.get("progress_from_previous") or {}).get("objective_delta") or 0.0
    )
    return (
        bool(latest.get("execution_valid")),
        # A parameter tweak of the incumbent cannot beat the incumbent
        # significantly on 35-day slices; the neighborhood search owns it.
        str(candidate.get("parent_source") or "") != "incumbent",
        fixability,
        _candidate_score(latest.get("outcome") or {}),
        progress,
        -int(candidate.get("slot") or 0),
    )


def _focus_candidates(
    state: Mapping[str, Any], policy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Top-ranked open candidates that receive the remaining repair budget."""
    limit = max(1, int(policy.get("focus_candidates") or 3))
    pool = [
        item
        for item in state.get("candidates") or []
        if item.get("status") in {"repair_pending", "quick_running"}
        and item.get("attempts")
    ]
    redesign = state.get("redesign")
    if isinstance(redesign, dict):
        # The designer chose the focus set: the candidates it kept plus the
        # replacements it added, each with the focus attempt cap.
        kept = {str(item) for item in redesign.get("kept") or []}
        initial = int(policy.get("generated_programs") or 0)
        pool = [
            item
            for item in pool
            if str(item.get("candidate_id")) in kept
            or int(item.get("slot") or 0) > initial
        ]
        limit = max(1, len(pool))
    return sorted(pool, key=_focus_rank, reverse=True)[:limit]


def _stamp_focus(state: dict[str, Any], policy: Mapping[str, Any]) -> None:
    if not _screen_before_repair(policy):
        return
    phase = "focus" if _screen_complete(state, policy) else "screen"
    state["focus"] = {
        "phase": phase,
        "candidate_ids": [
            str(item["candidate_id"]) for item in _focus_candidates(state, policy)
        ]
        if phase == "focus"
        else [],
        "computed_at": utc_now_iso(),
    }


def _incumbent_complexity(
    store: JobStore, job_id: str, campaign_id: str
) -> dict[str, Any] | None:
    pack = (
        store.read_json(
            job_id, f"{CAMPAIGN_ROOT}/{campaign_id}/{DIAGNOSTIC_PACK}", default={}
        )
        or {}
    )
    complexity = (pack.get("baseline") or {}).get("complexity")
    return dict(complexity) if isinstance(complexity, dict) else None


def _incumbent_economics(
    store: JobStore, job_id: str, campaign_id: str
) -> dict[str, Any] | None:
    pack = (
        store.read_json(
            job_id, f"{CAMPAIGN_ROOT}/{campaign_id}/{DIAGNOSTIC_PACK}", default={}
        )
        or {}
    )
    economics = (pack.get("baseline") or {}).get("economics")
    return dict(economics) if isinstance(economics, dict) else None


def _risk_budget(
    root: Path, baseline: Mapping[str, Any], research_context: Mapping[str, Any]
) -> dict[str, Any]:
    """The ceilings the finalist gate enforces, stated where designs are made."""
    hard = dict(load_constitution(root).get("hard_constraints") or {})
    stats = baseline.get("stats") if isinstance(baseline.get("stats"), Mapping) else {}
    drawdown = stats.get("max_drawdown_pct") if isinstance(stats, Mapping) else None
    return {
        "max_drawdown_pct": float(hard.get("max_drawdown_pct") or 0.25),
        "max_tail_loss": float(hard.get("max_tail_loss") or 0.15),
        "incumbent_max_drawdown_pct": (
            abs(float(drawdown)) if isinstance(drawdown, int | float) else None
        ),
        "prior_risk_ceiling": [
            {"family": row.get("family"), "implied_scale": row.get("implied_scale")}
            for row in research_context.get("validated_positives") or []
            if row.get("source") == "gate_edge_risk_ceiling"
        ][:5],
    }


# Fees plus slippage a book may spend per 30 days, as a fraction of capital;
# the cadence ceiling is derived from it at the incumbent's notional per fill.
MAX_COST_PCT_OF_CAPITAL_30D = 0.02


def _round_trip_cost_bps(params: Mapping[str, Any]) -> float:
    return round(
        2.0
        * (
            float(params.get("fee_bps") or 0.0)
            + float(params.get("slippage_bps") or 0.0)
        ),
        2,
    )


def _cost_budget(
    baseline: Mapping[str, Any],
    policy: Mapping[str, Any],
    dataset_window: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    economics = baseline.get("economics")
    if not isinstance(economics, Mapping):
        return None
    multiple = float(policy.get("max_fills_per_day_multiple") or 3.0)
    fills = float(economics.get("fills_per_day") or 0.0)
    budget: dict[str, Any] = {
        "incumbent_fills_per_day": fills,
        "incumbent_fee_pct_of_capital_30d": economics.get("fee_pct_of_capital_30d"),
        "incumbent_exposure_pct": economics.get("exposure_pct"),
        "incumbent_avg_hold_minutes": economics.get("avg_hold_minutes"),
        "cost_hurdle_multiple": float(
            policy.get("cost_hurdle_multiple") or COST_HURDLE_MULTIPLE
        ),
    }
    for key in ("gross_bps_per_trade", "cost_coverage"):
        if economics.get(key) is not None:
            budget[f"incumbent_{key}"] = economics[key]
    floor = _min_fills_per_day(policy, dataset_window)
    if floor is not None:
        budget["min_fills_per_day"] = floor
    round_trip = baseline.get("round_trip_cost_bps")
    if round_trip is not None:
        budget["round_trip_cost_bps"] = round_trip
    if baseline.get("maker_round_trip_bps") is not None:
        budget["maker_round_trip_bps"] = baseline["maker_round_trip_bps"]
    # The ceiling is cost arithmetic, not the incumbent's habit: fills per day
    # such that fees plus slippage stay under the 30-day cost budget at the
    # incumbent's notional per fill. Falls back to the cadence multiple when
    # the arithmetic has no inputs.
    cost_budget_30d = float(
        policy.get("max_cost_pct_of_capital_30d") or MAX_COST_PCT_OF_CAPITAL_30D
    )
    days = float(economics.get("window_days") or 0.0)
    turnover_multiple = float(economics.get("turnover_multiple") or 0.0)
    ceiling = None
    if round_trip and fills > 0 and days > 0 and turnover_multiple > 0:
        notional_fraction_per_fill = turnover_multiple / (fills * days)
        per_side_cost = float(round_trip) / 2.0 / 1e4
        if notional_fraction_per_fill > 0 and per_side_cost > 0:
            ceiling = (cost_budget_30d / 30.0) / (
                notional_fraction_per_fill * per_side_cost
            )
    if ceiling is not None:
        budget["max_fills_per_day"] = round(ceiling, 2)
        budget["max_cost_pct_of_capital_30d"] = cost_budget_30d
        budget["basis"] = "cost_budget"
    else:
        budget["max_fills_per_day"] = round(multiple * fills, 2)
        budget["basis"] = "incumbent_multiple"
    return budget


def _min_fills_per_day(
    policy: Mapping[str, Any], dataset_window: Mapping[str, Any] | None
) -> float | None:
    """Cadence below which the elite participation floor rejects at full dev."""
    days = float((dataset_window or {}).get("days") or 0.0)
    validation = float((policy.get("split") or {}).get("validation") or 0.0)
    floor_trades = int(policy.get("elite_min_validation_trades") or 8)
    if days <= 0 or validation <= 0:
        return None
    return round(floor_trades / (days * validation), 3)


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


def _tuning_drawdown_ceiling(root: Path, *, margin: float = 0.8) -> float:
    """Tuning may not spend the risk budget: trials past this drawdown are
    invalid, so the optimizer cannot buy return by sizing into the ceiling."""
    hard = dict(load_constitution(root).get("hard_constraints") or {})
    return margin * float(hard.get("max_drawdown_pct") or 0.25)


def _prune_risky_trials(
    grid: ExecutionGridResult, max_drawdown_pct: float
) -> tuple[ExecutionGridResult, int]:
    def drawdown(row: Mapping[str, Any]) -> float | None:
        value = row.get("max_drawdown_pct")
        if value is None:
            value = (row.get("stats") or {}).get("max_drawdown_pct")
        return None if value is None else abs(float(value))

    def keep(row: Mapping[str, Any]) -> bool:
        value = drawdown(row)
        return value is None or value <= max_drawdown_pct

    dropped = [row for row in grid.runs if not keep(row)]
    if not dropped:
        return grid, 0
    pruned = replace(
        grid,
        runs=[row for row in grid.runs if keep(row)],
        ranked=[row for row in grid.ranked if keep(row)],
        invalid=[
            *grid.invalid,
            *(
                {**row, "invalid_reason": "drawdown_over_tuning_ceiling"}
                for row in dropped
            ),
        ],
    )
    return pruned, len(dropped)


def _run_evolution_optuna(
    subject: dict[str, Any],
    dataset: PreparedExecutionDataset,
    search_space: dict[str, Any],
    *,
    trials: int,
    bars: int,
    timeout: float | None,
    max_drawdown_pct: float | None = None,
) -> tuple[ExecutionGridResult, dict[str, Any]]:
    search_data = _tail(dataset, bars) if bars > 0 else dataset
    started = perf_counter()
    specialized = bool(declared_regimes(search_space))
    rank_by = "regime_score" if specialized else "net_return"
    grid = run_optuna_search(
        subject["script"],
        search_data,
        subject["spec"],
        search_space,
        rank_by=rank_by,
        n_trials=trials,
        seed=_OPTUNA_SEED,
        timeout=timeout,
        objectives=[] if specialized else ["net_return", "max_drawdown_pct"],
    )
    risk_pruned = 0
    if max_drawdown_pct is not None:
        grid, risk_pruned = _prune_risky_trials(grid, max_drawdown_pct)
    return grid, {
        "status": "complete" if grid.ranked else "no_valid_trials",
        "risk_pruned": risk_pruned,
        "max_drawdown_pct": max_drawdown_pct,
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


FLAT_STRATEGY_SOURCE = (
    '"""Cash: the book that trades nothing. Applied when a campaign found '
    'nothing that beats it and the incumbent was losing to it."""\n\n\n'
    "def decide(ctx):\n"
    "    return []\n"
)


def retire_to_flat_verdict(
    store: JobStore, job_id: str, *, state: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Whether the campaign's outcome is "retire the incumbent to cash": the
    policy allows it, the incumbent lost to cash over the baseline window,
    and nothing graduated or is still on probation to replace it."""
    state = state or campaign_status(store, job_id)
    campaign_id = str(state.get("campaign_id") or "")
    policy = _campaign_policy(store, job_id, campaign_id) if campaign_id else {}
    pack_ref = state.get("diagnostic_pack")
    pack_path = (
        str(pack_ref.get("path") or "")
        if isinstance(pack_ref, Mapping)
        else str(pack_ref or "")
    )
    pack = store.read_json(job_id, pack_path, default={}) if pack_path else {}
    vs_cash = ((pack or {}).get("baseline") or {}).get("vs_cash") or {}
    pending = [
        str(candidate.get("candidate_id") or "")
        for candidate in state.get("candidates") or []
        if candidate.get("status") in {"probation", "proposed", "dev_frontier"}
    ]
    enabled = bool(policy.get("retire_to_flat_when_incumbent_negative", True))
    losing = vs_cash.get("beats_cash") is False
    recommended = enabled and losing and not pending
    if not enabled:
        reason = "policy disabled"
    elif vs_cash.get("net_return") is None:
        reason = "incumbent baseline unavailable"
    elif not losing:
        reason = "incumbent beats cash over the baseline window"
    elif pending:
        reason = f"{len(pending)} candidate(s) still in flight: {', '.join(pending)}"
    else:
        reason = (
            f"incumbent lost {100 * abs(float(vs_cash['net_return'])):.1f}% over "
            f"{float(vs_cash.get('window_days') or 0.0):.0f} days and nothing "
            "graduated; cash beats it"
        )
    return {
        "recommended": recommended,
        "reason": reason,
        "incumbent_net_return": vs_cash.get("net_return"),
        "incumbent_fee_pct_of_capital": vs_cash.get("fee_pct_of_capital"),
        "pending_candidates": pending,
    }


def flat_bundle(store: JobStore, job_id: str, destination: Path) -> Path:
    """The job's own bundle with a strategy that never trades: the cash
    position, applied through the ordinary bundle path so revision,
    validation and the world's spec identity all hold."""
    root = store.job_dir(job_id)
    copy_job_bundle(root, destination)
    job_data = _load_job_yaml(destination)
    script = store.resolve_script_entrypoint(
        job_id, job_data, candidate_dir=destination
    )
    if script is None:
        raise FileNotFoundError("job has no script entrypoint to flatten")
    script.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(script, FLAT_STRATEGY_SOURCE)
    for stale in destination.glob("workspace/src/*.py"):
        if stale != script:
            stale.unlink()
    return destination


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
                hard = dict(
                    load_constitution(store.job_dir(job_id)).get("hard_constraints")
                    or {}
                )
                risk_normalization: dict[str, Any] | None = None
                if economic.get("ready") is not True:
                    risk_normalization = _normalize_finalist_risk(
                        store,
                        job_id,
                        candidate,
                        candidate_root=candidate_root,
                        campaign_id=campaign_id,
                        economic=economic,
                        hard=hard,
                    )
                    if risk_normalization and risk_normalization.get("applied"):
                        economic = risk_normalization.pop("economic")
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
                outcome["gate"] = _gate_summary(economic, hard, risk_normalization)
                if risk_normalization is not None:
                    outcome["risk_normalization"] = risk_normalization
                    if risk_normalization.get("revision"):
                        outcome["revision"] = risk_normalization["revision"]
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
        # The gate report is the memory the next campaign learns from.
        _archive_campaign_candidate(store, job_id, candidate)
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
        state["retire_to_flat"] = retire_to_flat_verdict(store, job_id, state=state)
        _save_campaign(store, job_id, state)
    if state["retire_to_flat"].get("recommended"):
        # Production proposes; the bench applies (bench/recurrence.py). Either
        # way the loop can now say "nothing beats cash, stop bleeding".
        store.append_journal(
            job_id,
            {
                "type": "retire_to_flat_recommended",
                "campaign_id": state["campaign_id"],
                **state["retire_to_flat"],
            },
        )
    store.append_journal(
        job_id,
        {
            "type": "evolution_campaign_completed",
            "campaign_id": state["campaign_id"],
            "counts": state["counts"],
            "funnel": summarize_evolution_funnel(state),
            **(
                {"evaluation_plan": state["evaluation_plan"]}
                if state.get("evaluation_plan") is not None
                else {}
            ),
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


def _risk_ceiling_scale(
    economic: Mapping[str, Any], hard: Mapping[str, Any], *, margin: float
) -> tuple[float, dict[str, Any]] | None:
    """Scale that would bring a finalist under the hard ceilings, when the
    ONLY thing wrong with it is risk: every gate reason is a ceiling breach
    and the paired edge over the incumbent is positive."""
    reasons = [str(reason) for reason in economic.get("reasons") or []]
    if economic.get("status") != "ok" or not reasons:
        return None
    if not all(reason.startswith(_RISK_CEILING_REASON_PREFIXES) for reason in reasons):
        return None
    delta = economic.get("paired_incumbent_delta") or {}
    if float(delta.get("estimate") or 0.0) <= 0:
        return None
    vector = (economic.get("objective") or {}).get("candidate") or {}
    drawdown = float(vector.get("max_drawdown_pct") or 0.0)
    tail = float(vector.get("tail_loss") or 0.0)
    ceiling_drawdown = float(hard.get("max_drawdown_pct") or 0.25)
    ceiling_tail = float(hard.get("max_tail_loss") or 0.15)
    ratios = [1.0]
    if drawdown > 0:
        ratios.append(ceiling_drawdown / drawdown)
    if tail > 0:
        ratios.append(ceiling_tail / tail)
    scale = round(margin * min(ratios), 4)
    return scale, {
        "max_drawdown_pct": drawdown,
        "tail_loss": tail,
        "net_log_growth": vector.get("net_log_growth"),
        "ceiling_max_drawdown_pct": ceiling_drawdown,
        "ceiling_max_tail_loss": ceiling_tail,
    }


def _gate_summary(
    economic: Mapping[str, Any],
    hard: Mapping[str, Any],
    risk_normalization: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Five numbers and a class: what the next design turn needs to know."""
    vector = (economic.get("objective") or {}).get("candidate") or {}
    delta = economic.get("paired_incumbent_delta") or {}
    reasons = [str(reason) for reason in economic.get("reasons") or []]
    ceiling_only = bool(reasons) and all(
        reason.startswith(_RISK_CEILING_REASON_PREFIXES) for reason in reasons
    )
    if economic.get("ready") is True:
        klass = "staged"
    elif ceiling_only and float(delta.get("estimate") or 0.0) > 0:
        klass = "risk_ceiling"
    else:
        klass = "no_edge"
    return {
        "class": klass,
        "reasons": reasons[:6],
        "observed_max_drawdown_pct": vector.get("max_drawdown_pct"),
        "ceiling_max_drawdown_pct": hard.get("max_drawdown_pct"),
        "tail_loss": vector.get("tail_loss"),
        "oos_net_log_growth": vector.get("net_log_growth"),
        "paired_estimate": delta.get("estimate"),
        "paired_lcb": delta.get("lcb"),
        "implied_scale": (risk_normalization or {}).get("scale"),
    }


def _normalize_finalist_risk(
    store: JobStore,
    job_id: str,
    candidate: dict[str, Any],
    *,
    candidate_root: Path,
    campaign_id: str,
    economic: Mapping[str, Any],
    hard: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Size a risk-only rejection to the ceiling and re-run the gate once.

    Edge was proven; position size is not the designer's claim. The scale
    lands in the bundle's execution_params, so the sized strategy is what
    probation and any later apply run. Below the floor the design itself is
    too risky and stays rejected.
    """
    policy = _campaign_policy(store, job_id, campaign_id)
    if not bool(policy.get("finalist_risk_normalization", True)):
        return None
    classified = _risk_ceiling_scale(
        economic, hard, margin=float(policy.get("finalist_risk_margin") or 0.9)
    )
    if classified is None:
        return None
    scale, before = classified
    result: dict[str, Any] = {
        "class": "risk_ceiling",
        "scale": scale,
        "before": before,
        "applied": False,
    }
    if scale < _RISK_NORMALIZATION_FLOOR:
        result["reason"] = "scale_below_floor"
        return result
    job_data = _load_job_yaml(candidate_root)
    params = dict(job_data.get("execution_params") or {})
    params["size_scale"] = clamp_size_scale(
        float(params.get("size_scale") or 1.0) * scale
    )
    job_data["execution_params"] = params
    atomic_write_text(
        candidate_root / "job.yaml", yaml.safe_dump(job_data, sort_keys=False)
    )
    result["revision_before"] = str(candidate.get("revision") or "")
    candidate["revision"] = compute_workspace_revision(candidate_root)
    result["revision"] = candidate["revision"]
    result["size_scale"] = params["size_scale"]
    second = _isolated_economic_gate(store, job_id, candidate, campaign_id=campaign_id)
    vector = (second.get("objective") or {}).get("candidate") or {}
    result.update(
        {
            "applied": True,
            "after": {
                key: vector.get(key)
                for key in ("max_drawdown_pct", "tail_loss", "net_log_growth")
            },
            "ready": second.get("ready") is True,
            "economic": second,
        }
    )
    return result


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


def _campaign_trials(store: JobStore, job_id: str) -> int:
    """Quick attempts spent so far in the active campaign — the searches that
    produced whatever is being certified."""
    state = store.read_json(job_id, CAMPAIGN_STATE_PATH, default={}) or {}
    return max(1, int((state.get("counts") or {}).get("quick_attempts") or 0))


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
    policy = _campaign_policy(store, job_id, campaign_id)
    dataset_root = (
        _verified_protected_dataset_root(store, job_id, campaign_id)
        if _protected_fold_policy(policy)["enabled"]
        else (store.job_dir(job_id) / CAMPAIGN_ROOT / campaign_id / CAMPAIGN_DATA_ROOT)
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
            trials=_campaign_trials(store, job_id),
            dataset_root=dataset_root,
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


def _screen_table(
    state: Mapping[str, Any], policy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The compact per-candidate record the redesign turn reads: what each
    slot was, what its screen said, and how fixable that failure is."""
    rows: list[dict[str, Any]] = []
    for candidate in state.get("candidates") or []:
        attempts = list(candidate.get("attempts") or [])
        latest = attempts[-1] if attempts else {}
        postmortem = latest.get("postmortem") or {}
        screen = postmortem.get("screen") or {}
        slices = screen.get("slices") or {}
        primary = str(postmortem.get("primary_failure") or "")
        rows.append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "slot": candidate.get("slot"),
                "family": candidate.get("family"),
                "parent_source": candidate.get("parent_source"),
                "starter_seed_id": candidate.get("starter_seed_id"),
                "policy_ref": candidate.get("policy_ref"),
                "status": candidate.get("status"),
                "attempts": len(attempts),
                "execution_valid": bool(latest.get("execution_valid")),
                "primary_failure": primary or None,
                "failure_codes": list(postmortem.get("failure_codes") or []),
                "fixability": _FIXABILITY.get(primary, 0),
                "screen": {
                    "cost_coverage": screen.get("cost_coverage"),
                    "total_trades": screen.get("total_trades"),
                    "slices": {
                        label: {
                            "net_return": (report or {}).get("net_return"),
                            "lcb": (report or {}).get("lcb"),
                        }
                        for label, report in slices.items()
                        if isinstance(report, dict)
                    },
                },
            }
        )
    return rows


def _redesign_prompt_block(
    store: JobStore, job_id: str, state: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any]:
    """The checkpoint turn: the designer sees every screen and may abandon
    falsified slots, keep fixable ones and add replacements, including kernel
    slots for policy-scan survivors. The mechanical focus ranking could never
    say "this family is dead, try a relay"."""
    root = store.job_dir(job_id)
    policy = manifest.get("policy") or {}
    pack_path = (root / str(state["diagnostic_pack"])).resolve()
    manifest_path = (root / str(state["manifest"])).resolve()
    pack = store.read_json(job_id, str(state["diagnostic_pack"]), default={}) or {}
    table = _screen_table(state, policy)
    open_ids = [
        str(row["candidate_id"]) for row in table if row["status"] == "repair_pending"
    ]
    used_policies = {
        str(candidate.get("policy_id") or "")
        for candidate in state.get("candidates") or []
    }
    policy_refs = [
        str(row.get("pointer"))
        for row in (pack.get("policy_scan") or {}).get("survivors") or []
        if row.get("pointer")
        and (row.get("recipe") or {}).get("module")
        and str(row.get("policy_id") or "") not in used_policies
    ]
    counts = state.get("counts") or {}
    remaining = max(
        0,
        int(policy.get("max_quick_attempts") or 0)
        - int(counts.get("quick_attempts") or 0),
    )
    max_new = int(policy.get("redesign_slots") or 3)
    table_text = "; ".join(
        f"{row['candidate_id']} ({row['family']}, {row['parent_source']}"
        + (f" {row['starter_seed_id']}" if row.get("starter_seed_id") else "")
        + (f" {row['policy_ref']}" if row.get("policy_ref") else "")
        + f"): {row['status']}, {row['attempts']} attempt(s), "
        + (
            f"{row['primary_failure']} {row['failure_codes']}"
            if row.get("primary_failure")
            else "passed the screen"
        )
        + (
            f", coverage {row['screen']['cost_coverage']}, "
            f"{row['screen']['total_trades']} trades, slices "
            + ", ".join(
                f"{label} {float(item.get('net_return') or 0):+.1%}"
                for label, item in row["screen"]["slices"].items()
            )
            if row["screen"].get("slices")
            else ""
        )
        + f", fixability {row['fixability']}"
        for row in table
    )
    next_action = (
        "Redesign checkpoint: every initial slot has had its screen attempt. "
        f"Read `{pack_path}` again if needed. The screens: {table_text}. "
        f"Open (repairable) candidates: {open_ids}. Attempts remaining: {remaining}. "
        "Decide once for the whole table: abandon the candidates whose family "
        "the screen falsified (cost-negative gross, no trades, or a slice loss "
        "the mechanism cannot fix), keep the ones a bounded repair can fix, "
        f"and add up to {max_new} replacement slots for the material the "
        "screens and the pack point at instead"
        + (
            f": policy-scan survivors not yet tried {policy_refs} (a policy_kernel "
            "slot with policy_ref instantiates one with no new code)"
            if policy_refs
            else ""
        )
        + ". Replacement slots follow the design rules (hypothesis with "
        "evidence_refs, slot with slot_id, wildcard=false, hypothesis_id, "
        "parent_source, mutation_kind, family, summary; no wildcards, no "
        "incumbent slot); each replacement costs one screen attempt from the "
        "remaining budget. Kept candidates plus the replacements become the "
        "focus set for the remaining repairs. Call wayfinder_core_jobs("
        f'action="evolution_redesign", job_id="{job_id}", redesign={{"abandon": '
        '[...], "keep": [...], "hypotheses": [...], "slots": [...]}) exactly once '
        "(hypotheses and slots may be empty lists; keeping everything is a valid "
        "decision), then end this stage."
    )
    return {
        "job_id": job_id,
        "campaign_id": state["campaign_id"],
        "stage": state["stage"],
        "session_stage": REDESIGN_STAGE,
        "artifact_key": REDESIGN_STAGE,
        "agent_name": "wayfinder-evolution-designer",
        "deadline_at": state["deadline_at"],
        "counts": state["counts"],
        "diagnostic_pack": str(pack_path),
        "manifest_path": str(manifest_path),
        "valid_evidence_pointers": valid_evidence_pointers(pack),
        "next_action": next_action,
        "constraints": {
            "redesign": {
                "open": open_ids,
                "attempts_remaining": remaining,
                "max_new_slots": max_new,
                "policy_refs": policy_refs,
                "table": table,
            }
        },
    }


def submit_campaign_redesign(
    store: JobStore, job_id: str, *, redesign: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Apply the checkpoint decision: close abandoned candidates on their
    best attempt, extend the accepted design with the replacement slots, and
    record the focus set. One checkpoint per campaign."""
    if not isinstance(redesign, Mapping):
        raise ValueError("redesign must be an object")
    with job_state_lock(store.repo_root, job_id, name="evolution_campaign"):
        state = _active_campaign(store, job_id)
        if str(state.get("schema_version") or "") != SCHEMA_VERSION:
            raise ValueError("legacy evolution campaigns have no redesign checkpoint")
        campaign_id = str(state["campaign_id"])
        manifest = _campaign_manifest(store, job_id, campaign_id)
        policy = manifest["policy"]
        if state.get("redesign") is not None:
            raise ValueError("the redesign checkpoint was already used")
        if not _redesign_due(state, policy):
            raise ValueError(
                "redesign is not due: it runs once, after every initial slot has "
                "had its screen attempt and while attempts remain"
            )
        by_id = {
            str(item.get("candidate_id")): item
            for item in state.get("candidates") or []
        }
        abandon = [str(item) for item in redesign.get("abandon") or []]
        keep = [str(item) for item in redesign.get("keep") or []]
        unknown = [cid for cid in [*abandon, *keep] if cid not in by_id]
        if unknown:
            raise ValueError(f"redesign names unknown candidates: {unknown}")
        both = sorted(set(abandon) & set(keep))
        if both:
            raise ValueError(f"redesign both abandons and keeps: {both}")
        closed = [
            cid
            for cid in [*abandon, *keep]
            if by_id[cid].get("status") != "repair_pending"
        ]
        if closed:
            raise ValueError(
                f"only open (repair_pending) candidates can be abandoned or kept: {closed}"
            )
        hypotheses = list(redesign.get("hypotheses") or [])
        slots = list(redesign.get("slots") or [])
        if bool(hypotheses) != bool(slots):
            raise ValueError("replacement hypotheses and slots come together")
        root = store.job_dir(job_id)
        design_path = root / str(state["campaign_design"])
        design = json.loads(design_path.read_text(encoding="utf-8"))
        added: list[str] = []
        if slots:
            pack = (
                store.read_json(job_id, str(state["diagnostic_pack"]), default={}) or {}
            )
            reserved = {
                *(str(item.get("slot_id")) for item in design.get("slots") or []),
                *(str(item.get("id")) for item in design.get("hypotheses") or []),
            }
            extension = _validate_campaign_design(
                {"hypotheses": hypotheses, "slots": slots},
                manifest=manifest,
                diagnostic_pack=pack,
                extension=True,
                reserved_ids=reserved,
            )
            design["hypotheses"].extend(extension["hypotheses"])
            design["slots"].extend(extension["slots"])
            added = [str(item["slot_id"]) for item in extension["slots"]]
            atomic_write_json(design_path, design)
            state["design"] = {
                **dict(state.get("design") or {}),
                "sha256": _file_hash(design_path),
                "hypotheses": len(design["hypotheses"]),
                "slots": len(design["slots"]),
            }
        for cid in abandon:
            candidate = by_id[cid]
            _close_designed_candidate(store, job_id, state=state, candidate=candidate)
            candidate["abandoned_at_redesign"] = True
        state["redesign"] = {
            "at": utc_now_iso(),
            "abandoned": abandon,
            "kept": keep,
            "added_slots": added,
            "extra_slots": len(added),
        }
        _stamp_focus(state, policy)
        _save_campaign(store, job_id, state)
    store.append_journal(
        job_id,
        {
            "type": "evolution_campaign_redesigned",
            "campaign_id": campaign_id,
            "abandoned": len(abandon),
            "kept": len(keep),
            "added": len(added),
        },
    )
    return {
        "status": "accepted",
        "campaign_id": campaign_id,
        "abandoned": abandon,
        "kept": keep,
        "added_slots": added,
        "focus": list((state.get("focus") or {}).get("candidate_ids") or []),
    }


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
    if state.get("stage") == COMPOSE_STAGE and current < deadline:
        return _compose_prompt_block(store, job_id, state, manifest)
    if state.get("stage") in {"design", COMPOSE_STAGE}:
        if current >= deadline:
            return {
                "job_id": job_id,
                "campaign_id": state["campaign_id"],
                "stage": str(state.get("stage")),
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
            if item.get("starter_id") and item.get("compatible", True)
        ]
        research_seed_ids = [
            str(item.get("seed_id"))
            for item in manifest.get("research_seeds") or []
            if item.get("seed_id")
        ]
        policy_refs = [
            str(row.get("pointer"))
            for row in (diagnostic_pack.get("policy_scan") or {}).get("survivors") or []
            if row.get("pointer") and (row.get("recipe") or {}).get("module")
        ]
        cost_budget = _cost_budget(
            diagnostic_pack.get("baseline") or {}, policy, manifest.get("dataset")
        )
        risk_budget = _risk_budget(
            store.job_dir(job_id),
            diagnostic_pack.get("baseline") or {},
            diagnostic_pack.get("research_context") or {},
        )
        risk_instruction = (
            "Risk budget: OOS max drawdown <= "
            f"{risk_budget['max_drawdown_pct']:.0%} and tail loss <= "
            f"{risk_budget['max_tail_loss']:.0%} are hard ceilings at the finalist "
            "gate"
            + (
                f"; the incumbent runs at {risk_budget['incumbent_max_drawdown_pct']:.0%}"
                if risk_budget.get("incumbent_max_drawdown_pct") is not None
                else ""
            )
            + ". Size and stop every slot for the ceiling; sizing is set "
            "mechanically at the gate, never a search dimension"
            + (
                "; prior candidates with proven edge that only failed the ceiling: "
                + ", ".join(
                    f"{row['family']} (size <= {row['implied_scale']}x)"
                    for row in risk_budget["prior_risk_ceiling"]
                )
                if risk_budget.get("prior_risk_ceiling")
                else ""
            )
            + ". "
        )
        vs_cash = (diagnostic_pack.get("baseline") or {}).get("vs_cash") or {}
        cash_instruction = (
            (
                f"The incumbent {'made' if vs_cash['beats_cash'] else 'lost'} "
                f"{100 * abs(float(vs_cash['net_return'])):.1f}% over "
                f"{float(vs_cash.get('window_days') or 0.0):.0f} days"
                + (
                    f" and paid {100 * float(vs_cash['fee_pct_of_capital']):.1f}% of "
                    "capital in fees"
                    if vs_cash.get("fee_pct_of_capital") is not None
                    else ""
                )
                + (
                    "; cash beat it, and a campaign that finds nothing better "
                    "retires it to cash"
                    if not vs_cash["beats_cash"]
                    else ""
                )
                + ". Every slot must beat cash on its own validation window, not "
                "only the incumbent; cite /baseline/vs_cash. "
            )
            if vs_cash.get("net_return") is not None
            else ""
        )
        cost_instruction = (
            "Cost budget: "
            + (
                f"at ~{cost_budget['round_trip_cost_bps']:.0f} bps round trip a "
                f"trade must capture at least {cost_budget['cost_hurdle_multiple']:.1f}x "
                f"that gross ({cost_budget['cost_hurdle_multiple'] * cost_budget['round_trip_cost_bps']:.0f} bps) "
                "or the screen rejects it before anything else"
                + (
                    f" (a post-only resting entry pays the ~{cost_budget['maker_round_trip_bps']:.0f} bps "
                    "maker round trip instead and its offset is price improvement: "
                    "the lever for a fast signal whose move is real but small)"
                    if cost_budget.get("maker_round_trip_bps") is not None
                    else ""
                )
                + "; "
                if cost_budget.get("round_trip_cost_bps") is not None
                else ""
            )
            + f"the incumbent trades {cost_budget['incumbent_fills_per_day']:.1f} fills/day"
            + (
                f" capturing {cost_budget['incumbent_gross_bps_per_trade']:+.1f} bps "
                "gross per trade"
                if cost_budget.get("incumbent_gross_bps_per_trade") is not None
                else ""
            )
            + (
                f" and pays {100 * float(cost_budget['incumbent_fee_pct_of_capital_30d']):.1f}% "
                "of capital in fees per 30 days"
                if cost_budget.get("incumbent_fee_pct_of_capital_30d") is not None
                else ""
            )
            + f". Every slot must plausibly stay under {cost_budget['max_fills_per_day']:.1f} fills/day"
            + (
                f" (the {100 * float(cost_budget['max_cost_pct_of_capital_30d']):.0f}% "
                "of capital per 30 days cost budget at the incumbent's notional per fill)"
                if cost_budget.get("basis") == "cost_budget"
                else ""
            )
            + (
                f" and above {cost_budget['min_fills_per_day']:.2f} (the elite "
                "participation floor rejects inert books)"
                if cost_budget.get("min_fills_per_day") is not None
                else ""
            )
            + ". Continuous per-bar rebalancing is dead on arrival; cite "
            "/baseline/economics when sizing cadence. "
            if cost_budget
            else ""
        )
        failure_target = (
            ((diagnostic_pack.get("baseline") or {}).get("failure_modes") or {}).get(
                "slices"
            )
            or {}
        ).get("recent") or {}
        failure_instruction = (
            "Failure-mode target: on the recent screen slice the incumbent lost on "
            f"{failure_target['losing_days']} of {failure_target['days']} days "
            f"({100 * float(failure_target.get('losing_return') or 0.0):+.1f}% on "
            f"those days; worst regime {failure_target.get('worst_regime')!r}). A "
            "candidate that repairs those days while staying non-inferior on the "
            "incumbent's winning days passes the screen by the failure-mode route; "
            "cite /baseline/failure_modes. "
            if failure_target.get("days")
            else ""
        )
        concentration = leader_attribution_sentence(
            failure_target.get("leader_attribution")
        )
        if failure_instruction and concentration:
            failure_instruction += (
                f"The incumbent's losses concentrate: {concentration}; a design "
                "that stands down on that side of the market repairs those days. "
            )
        validated = diagnostic_pack.get("validated_signals") or {}
        validated_rows = (
            list(validated.get("signals") or []) if validated.get("available") else []
        )
        ideation = diagnostic_pack.get("research_ideation") or {}
        ideation_rows = [
            (index, row)
            for index, row in enumerate(ideation.get("hypotheses") or [])
            if ideation.get("valid") and row.get("bucket") == "testable"
        ]
        ideation_instruction = (
            "Researcher hypotheses from the latest expedition (cite "
            "/research_ideation/hypotheses/<i>): "
            + "; ".join(f"[{index}] {row['title']}" for index, row in ideation_rows[:5])
            + ". Ground at least one non-wildcard slot on a testable one, or say in "
            "that hypothesis's falsifier why the pack refutes it. "
            if ideation_rows
            else (
                "The latest researcher artifact failed its contract ("
                + "; ".join(str(problem) for problem in ideation.get("problems") or [])
                + "); do not cite it. "
                if ideation and ideation.get("valid") is False
                else ""
            )
        )
        funnel = validated.get("funnel") or {}
        funnel_text = (
            f" (of {funnel.get('tests')} tests: {funnel.get('directional_fold_stable')} "
            f"directional and fold-stable, {funnel.get('cost_positive')} positive net of "
            f"the round trip, {funnel.get('t_net_at_floor')} at the t_net floor, "
            f"{funnel.get('q_at_threshold')} at q <= {float(validated.get('q_threshold') or 0.2):.2f}, "
            f"{funnel.get('powered')} with enough events, {funnel.get('non_inferior')} "
            "non-inferior on both slices"
            + (
                f"; {funnel.get('replicated') or 0} replicated regardless of cost, "
                f"{funnel.get('passive_only') or 0} of them passive-only and "
                f"{funnel.get('mechanism_required') or 0} needing a mechanism; "
                f"{funnel.get('regime_tests') or 0} regime-conditioned tests"
                if funnel.get("replicated") is not None
                else ""
            )
            + ")"
            if funnel.get("tests")
            else ""
        )
        replicated_rows = (
            list(validated.get("replicated") or [])
            if validated.get("available")
            else []
        )
        replicated_instruction = (
            "Replicated signals (directional, 3/4 fold-stable, powered and "
            "non-inferior on both slices, NOT family-corrected: q is reported and "
            f"~{float(validated.get('expected_lucky_passes') or 0):.0f} of "
            f"{int(validated.get('tests') or 0)} tests would pass |t|>=2 by luck; "
            "cite /validated_signals/replicated/<i>): "
            + "; ".join(
                f"[{index}] {row['signal']} {row['direction']} {row['symbol']} "
                f"{row['timeframe']} x{row['horizon']}"
                + (f" in {row['regime']}" if row.get("regime") else "")
                + f" (t {float(row.get('t_stat') or 0):+.1f}, q "
                f"{float(row.get('q_value') or 0):.2f}, {row.get('events', 0)} events, "
                f"gross {float(row.get('gross_edge_bps') or 0):+.1f} bps vs taker "
                f"{float(row.get('taker_round_trip_bps') or 0):.0f} / maker "
                f"{float(row.get('maker_round_trip_bps') or 0):.0f} bps: "
                f"{row.get('execution_hint')})"
                for index, row in enumerate(replicated_rows[:10])
            )
            + ". A passive_only or mechanism_required entry is monetized by its "
            "execution (a post-only resting entry at an offset and a passive "
            "take-profit; its how_to_use says so), never by taking the close"
            + (
                "; with no validated signal offered, every grounded de_novo or "
                "research_context slot must cite one of these"
                if not validated_rows
                else ""
            )
            + ". "
            if replicated_rows
            else ""
        )
        signal_instruction = (
            (
                "Validated signals (fold-stable, cost-net edge on the full train "
                "split, Benjamini-Hochberg q at or under "
                f"{float(validated.get('q_threshold') or 0.2):.2f} across "
                f"{int(validated.get('tests') or 0)} tests, non-inferior on both "
                "screen slices; cite /validated_signals/signals/<i>): "
                + "; ".join(
                    f"{row['signal']} {row['direction']} {row['symbol']} "
                    f"{row['timeframe']} x{row['horizon']} "
                    f"(t_net {row.get('t_net', 0):+.1f}, q {row.get('q_value') or 0:.2f}, "
                    f"{row.get('events', 0)} events)"
                    for row in validated_rows[:10]
                )
                + ". Every grounded de_novo or research_context slot must cite one "
                "of these; a design that does not is rejected. Each entry's "
                "how_to_use gives the one-call precompute (library_signal_on_bars), "
                "the fixed-horizon exit, and the warmup to declare; "
                "/validated_signals/how_to_use_notes carries the shared rules. "
                if validated_rows
                else (
                    "No library signal cleared the family-corrected edge test on this "
                    "dataset"
                    + funnel_text
                    + "; de_novo slots must state why their mechanism should earn here"
                    + (
                        " (nearest misses, not evidence: "
                        + "; ".join(
                            f"{row['signal']} {row['direction']} {row['symbol']} "
                            f"{row['timeframe']} x{row['horizon']} [{row.get('shortfall')}]"
                            for row in (validated.get("near_misses") or [])[:3]
                        )
                        + ")"
                        if validated.get("near_misses")
                        else ""
                    )
                    + ". "
                    if validated.get("available")
                    else ""
                )
            )
            + replicated_instruction
            + _mechanism_instruction(
                job_id, diagnostic_pack, [*validated_rows, *replicated_rows]
            )
            + _cross_sectional_instruction(validated.get("cross_sectional") or {})
            + _policy_scan_instruction(diagnostic_pack.get("policy_scan") or {})
        )
        regime_context = manifest.get("regime_context") or {}
        specialist_design = bool(
            policy.get("regime_specialist_enabled") and regime_context.get("available")
        )
        regime_instruction = (
            "Every slot must declare target_regimes as a list of one or two "
            f"cells from {list(REGIME_LABELS)}. Span at least two cells and "
            "include at least one slot for the measured counter-regime "
            f"{regime_context.get('counter_regime')!r}; the recent primary is "
            f"{regime_context.get('primary_regime')!r}. "
            if specialist_design
            else ""
        )
        macro_regime = regime_context.get("macro")
        macro_instruction = macro_regime_sentence(
            macro_regime if isinstance(macro_regime, dict) else None
        )
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
                f"{research_instruction}{regime_instruction}{macro_instruction}"
                f"{cash_instruction}{cost_instruction}{risk_instruction}{ideation_instruction}"
                f"{failure_instruction}{signal_instruction}"
                "Use at most one incumbent slot and at "
                "most two parameter slots. One wildcard must be de_novo. "
                'Call wayfinder_core_jobs with action="evolution_design", '
                f'job_id="{job_id}", and campaign_design={{"hypotheses": [...], '
                '"slots": [...]}}, background=true. Each hypothesis needs id, family, '
                "causal_mechanism, falsifier, evidence_refs. Each slot needs "
                "slot_id, wildcard, hypothesis_id (null for wildcard), "
                "parent_source, mutation_kind, family, summary"
                f"{', target_regimes' if specialist_design else ''}. parent_source "
                "must be exactly one of incumbent, qd_elite, crossover, de_novo, "
                "starter_seed, research_seed, research_context, policy_kernel; it "
                "is an enum, so do not append a starter id or other qualifier. "
                "mutation_kind must be exactly structural or parameter. For a "
                "starter_seed slot, set optional starter_seed_id to one of "
                f"{starter_ids}; this structured id, not summary prose, selects "
                "the executable seed. For a research_seed slot, set optional "
                f"research_seed_id to one of {research_seed_ids}. "
                + (
                    "For a policy_kernel slot, set policy_ref to one of "
                    f"{policy_refs}: the survivor's kernel is instantiated with its "
                    "recipe params as the candidate (no new code), and the worker "
                    "runs it; use it for the portfolio policies the scan found. "
                    if policy_refs
                    else ""
                )
                + "If a hypothesis uses an exact family listed under "
                "research_context.refuted_families, it must also include "
                "addresses_refutation and new_evidence_refs (a non-empty "
                "subset of evidence_refs). "
                "Do not wait for the detached result; end immediately after launch."
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
                "regime_specialist_design": specialist_design,
                "regime_context": regime_context if specialist_design else None,
                "macro_regime": (
                    macro_regime if isinstance(macro_regime, dict) else None
                ),
                "cost_budget": cost_budget,
                "risk_budget": risk_budget,
                "research_ideation": [row["title"] for _, row in ideation_rows] or None,
                "failure_modes": failure_target or None,
                "validated_signals": [
                    f"{row['symbol']}:{row['signal']}:{row['timeframe']}:{row['horizon']}"
                    for row in validated_rows
                ]
                if validated.get("available")
                else None,
                "replicated_signals": [
                    f"{row['symbol']}:{row['signal']}:{row['timeframe']}:{row['horizon']}"
                    for row in replicated_rows
                ]
                if validated.get("available")
                else None,
            },
            "deadline_elapsed": current >= deadline,
        }
    policy = manifest.get("policy") or {}
    budget = _program_budget(state, policy)
    awaiting_evaluation = _awaiting_evaluation(state, policy)
    deadline_elapsed = current >= deadline
    draining = deadline - CAMPAIGN_DRAIN <= current < deadline
    if not deadline_elapsed and not draining and _redesign_due(state, policy):
        return _redesign_prompt_block(store, job_id, state, manifest)
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
        repair_work_order: dict[str, Any] | None = None
        if candidate.get("status") == "repair_pending":
            latest_attempt = (candidate.get("attempts") or [])[-1]
            postmortem_path = str(
                store.job_dir(job_id) / str(latest_attempt.get("postmortem_path") or "")
            )
            work_inputs.append(postmortem_path)
            raw_order = (latest_attempt.get("postmortem") or {}).get(
                "repair_work_order"
            )
            repair_work_order = dict(raw_order) if isinstance(raw_order, dict) else None
            repair_instruction = (
                f"This is repair {int(candidate.get('attempt_count') or 0)} of "
                f"at most {_attempt_cap(state, candidate, policy)}. Read "
                f"the deterministic postmortem at "
                f"`{store.job_dir(job_id) / str(latest_attempt.get('postmortem_path') or '')}`. "
                "Change the named causal mechanism in response to that evidence; "
                "do not rename the family or substitute a generic new idea. "
            )
            if repair_work_order:
                repair_instruction += _repair_work_order_sentence(repair_work_order)
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
        "repair_work_order": repair_work_order if awaiting_evaluation else None,
        "focus": state.get("focus"),
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
            # The searches spent so far: full development certifies the
            # validation window against their expected maximum t.
            "trials_so_far": int(
                (state.get("counts") or {}).get("quick_attempts") or 0
            ),
        },
        "deadline_elapsed": deadline_elapsed,
    }


def _awaiting_evaluation(
    state: Mapping[str, Any], policy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Candidates the next stage may work on, in service order.

    Screen-first allocation: never-evaluated slots always go first; parked
    repairs wait until every slot has had its screen attempt, then only the
    focus set is served, best rank first.  The legacy depth-first order is
    kept behind ``screen_before_repair: false`` for the bench control arm.
    """
    candidates = list(state.get("candidates") or [])
    fresh = [
        item
        for item in candidates
        if item.get("status") in {"prepared", "quick_failed"}
    ]
    if not _screen_before_repair(policy):
        return fresh + [
            item for item in candidates if item.get("status") == "repair_pending"
        ]
    if not _screen_complete(state, policy):
        return fresh
    focus = [
        item
        for item in _focus_candidates(state, policy)
        if item.get("status") == "repair_pending"
    ]
    return fresh + focus


def _repair_work_order_sentence(order: Mapping[str, Any]) -> str:
    budget = order.get("budget") or {}
    text = f"Diagnosis: {order.get('diagnosis')} Admissible repairs: " + "; ".join(
        str(item) for item in order.get("admissible_repairs") or []
    )
    forbidden = "; ".join(str(item) for item in order.get("forbidden") or [])
    if forbidden:
        text += f". Do not: {forbidden}"
    if budget.get("max_fills_per_day") is not None:
        text += f". Budget: at most {float(budget['max_fills_per_day']):.1f} fills/day"
    if budget.get("cost_coverage") is not None:
        basis = str(budget.get("cost_basis") or "nominal")
        paid = (
            float(budget.get("realized_cost_bps_per_trade") or 0.0)
            if basis == "realized"
            else float(budget.get("round_trip_cost_bps") or 0.0)
        )
        text += (
            f"; each trade captured {float(budget.get('gross_bps_per_trade') or 0.0):+.1f} "
            f"bps gross vs {paid:.1f} bps {basis} cost (coverage "
            f"{float(budget['cost_coverage']):.2f}x, hurdle "
            f"{float(budget.get('cost_hurdle_multiple') or 0.0):.1f}x"
            + (
                f"; maker round trip ~{float(budget['maker_round_trip_bps']):.0f} bps"
                if budget.get("maker_round_trip_bps") is not None
                else ""
            )
            + ")"
        )
    return text + ". "


_SIGNAL_REF_KEYS = (
    "signal_id",
    "symbol",
    "signal",
    "timeframe",
    "horizon",
    "direction",
    "scope",
    "regime",
    "regime_source",
    "library",
    "t_stat",
    "t_net",
    "q_value",
    "events",
    "gross_edge_bps",
    "taker_round_trip_bps",
    "maker_round_trip_bps",
    "edge_net_maker_bps",
    "execution_hint",
    "expression",
    "min_bars",
    "source",
    "warmup_bars_required",
    "how_to_use",
)


def _cited_signals(
    store: JobStore, job_id: str, manifest: Mapping[str, Any], refs: Sequence[str]
) -> list[dict[str, Any]]:
    """The validated-signal entries a hypothesis cites, resolved from the
    frozen pack so the worker's candidate.json carries the recipe."""
    indices: list[tuple[str, int]] = []
    for ref in refs:
        match = re.match(
            r"^/validated_signals/(signals|replicated)/(\d+)(?:/|$)", str(ref)
        )
        if match and (match.group(1), int(match.group(2))) not in indices:
            indices.append((match.group(1), int(match.group(2))))
    if not indices:
        return []
    pack_path = str((manifest.get("diagnostic_pack") or {}).get("path") or "")
    pack = store.read_json(job_id, pack_path, default={}) if pack_path else {}
    block = (pack or {}).get("validated_signals") or {}
    shared = {
        key: block[key]
        for key in ("taker_round_trip_bps", "maker_round_trip_bps")
        if block.get(key) is not None
    }
    cited = []
    for tier, index in indices:
        rows = list(block.get(tier) or [])
        if 0 <= index < len(rows):
            row = {**shared, **rows[index]}
            cited.append(
                {
                    "pointer": f"/validated_signals/{tier}/{index}",
                    "tier": "validated" if tier == "signals" else "replicated",
                }
                | {key: row.get(key) for key in _SIGNAL_REF_KEYS if key in row}
            )
    return cited


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
        (
            f"{item.get('id')} (edge proven, size <= {item['implied_scale']}x)"
            if item.get("implied_scale")
            else str(item.get("id") or "")
        )
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
    if source == "policy_kernel":
        return (
            f"The bundle instantiates policy-scan survivor `{candidate.get('policy_ref')}` "
            f"(policy_id {candidate.get('policy_id')}): workspace/src/strategy.py "
            "re-exports the kernel and execution_params carry the scanned recipe, "
            "warmup and lookback. It is a screen, not evidence: run it as is first "
            "and let the screen judge it; change a parameter only when the "
            "hypothesis names why, and do not add code the recipe does not need. "
        )
    if source == "starter_seed":
        return (
            f"The bundle contains audited starter `{candidate.get('starter_seed_id')}` "
            "as an adaptation seed. Its own warmup/lookback is already set, but "
            "its historical evidence was reset: adapt the tactic to this job's "
            "target universe and make it re-earn every gate. The screen pairs it "
            "against the incumbent, not against the starter, so a seed that beats "
            "the incumbent on both slices passes as submitted; do not change a "
            "working mechanism just to differ from it. "
        )
    if source == "research_seed":
        return (
            f"The bundle contains sensor-authored research seed "
            f"`{candidate.get('research_seed_id')}`. Treat its hypothesis as a "
            "starting point only: its prior evidence was reset and it must re-earn "
            "every campaign gate, paired against the incumbent on the screen. "
            "Preserve the current job's operational contract."
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
        "target_regimes": list(candidate.get("target_regimes") or []),
        "signal_refs": list(candidate.get("signal_refs") or []),
        "attempt_count": int(candidate.get("attempt_count") or 0),
        "best_attempt": candidate.get("best_attempt"),
    }
    neighborhood = candidate.get("neighborhood")
    if isinstance(neighborhood, dict) and neighborhood.get("available"):
        handoff["neighborhood"] = {
            key: neighborhood.get(key)
            for key in ("searched", "incumbent", "best", "plateau", "applied", "reason")
            if neighborhood.get(key) is not None
        }
    if isinstance(candidate.get("complexity"), dict):
        handoff["complexity"] = dict(candidate["complexity"])
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
                "total_fees",
                "total_turnover_usd",
                "exposure_pct",
            )
            if quick_stats.get(key) is not None
        }
    evidence = candidate.get("evidence")
    if evidence:
        handoff["evidence"] = _compact_evidence(evidence, limit=240)
    recovery_reason = candidate.get("evaluation_recovery_reason")
    if recovery_reason:
        handoff["evaluation_recovery_reason"] = str(recovery_reason)[:240]
    if isinstance(candidate.get("submission_rejection"), Mapping):
        handoff["submission_rejection"] = {
            **candidate["submission_rejection"],
            "instruction": "fix the bundle and resubmit; no attempt was charged",
        }
    attempts = candidate.get("attempts") or []
    if attempts:
        latest = attempts[-1]
        latest_postmortem = latest.get("postmortem") or {}
        latest_summary: dict[str, Any] = {
            "attempt": latest.get("attempt"),
            "path": latest.get("postmortem_path"),
            **compact_postmortem(latest_postmortem),
        }
        order = latest_postmortem.get("repair_work_order")
        if isinstance(order, dict):
            latest_summary["repair_work_order"] = {
                key: order.get(key)
                for key in ("primary_failure", "diagnosis", "budget")
            }
        handoff["latest_postmortem"] = latest_summary
        # The trend across a candidate's own attempts is what a focus repair
        # needs to see; the last postmortem alone hides direction.
        handoff["trajectory"] = [
            _attempt_trajectory_row(item) for item in attempts[-6:]
        ]
    return handoff


def _attempt_trajectory_row(attempt: Mapping[str, Any]) -> dict[str, Any]:
    postmortem = attempt.get("postmortem") or {}
    economics = ((postmortem.get("economics") or {}).get("candidate")) or {}
    exits = ((postmortem.get("exits") or {}).get("candidate")) or {}
    quick = ((attempt.get("outcome") or {}).get("quick") or {}).get("stats") or {}
    row = {
        "attempt": attempt.get("attempt"),
        "primary_failure": postmortem.get("primary_failure"),
        "net_return": quick.get("net_return"),
        "fills_per_day": economics.get("fills_per_day"),
        "fee_pct_of_capital": economics.get("fee_pct_of_capital"),
    }
    if economics.get("gross_bps_per_trade") is not None:
        row["gross_bps_per_trade"] = economics["gross_bps_per_trade"]
    if quick.get("max_drawdown_pct") is not None:
        row["max_drawdown_pct"] = round(abs(float(quick["max_drawdown_pct"])), 6)
    if exits.get("closes"):
        row["stop_share"] = exits.get("stop_share")
    return row


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
            "target_regimes",
            "full_dev_failure_codes",
            "evaluation_plan",
            "gate",
            "risk_normalization",
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
    if isinstance(payload, dict):
        payload = normalize_search_space(payload)
    if not isinstance(payload, dict) or not is_search_space(payload):
        offending = untyped_search_keys(payload) if isinstance(payload, dict) else []
        raise ValueError(
            "candidate search space is not typed: "
            + (f"keys {offending} " if offending else "")
            + 'each dimension must be {"type": "float"|"int"|"categorical", '
            '"low", "high"} or {"type": "categorical", "choices": [...]} '
            f"({search_path})"
        )
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
    policy = _campaign_policy(store, job_id, campaign_id)
    certification_policy = _protected_fold_policy(policy)
    if certification_policy["enabled"]:
        train_end, validation_end = 1.0, 1.0
        train = subject["dataset"]
        validation = None
    else:
        train_end, validation_end = _split_bounds(
            store, job_id, campaign_id=campaign_id
        )
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
        search_bars = int(policy.get("inner_optuna_train_bars") or 0)
        search_timeout = float(policy.get("inner_optuna_timeout_seconds") or 0) or None
        grid, tuning = _run_evolution_optuna(
            subject,
            train,
            search,
            trials=min(int(policy["inner_optuna_trials"]), 20),
            bars=search_bars,
            timeout=search_timeout,
            max_drawdown_pct=_tuning_drawdown_ceiling(root),
        )
        selected, plateau = _plateau_select(
            grid,
            _typed_search_dimensions(candidate_search),
            str(getattr(grid, "rank_by", None) or "net_return"),
        )
        tuning["plateau"] = plateau
        if selected is not None:
            params = dict(selected["params"])
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
            f"{probe['window']} bars ({_probe_mismatch_text(probe)}) — "
            f"{BOUNDED_WINDOW_HINT}"
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
    if certification_policy["enabled"]:
        return _protected_fold_full_dev(
            store,
            job_id,
            candidate,
            campaign_id=campaign_id,
            root=root,
            subject=subject,
            params=params,
            stress_params=stress_params,
            calibration=calibration,
            tuning=tuning,
            revision=revision,
            train_stats=train_stats,
            compact_train=compact_train,
            policy=policy,
            certification_policy=certification_policy,
            manifest=manifest,
        )
    assert validation is not None
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
    compact_validation["exits"] = receipt_exits(
        result_receipt(validation_result, revision=revision)
    )
    compact_validation["forensics"] = _validation_forensics(
        validation_result, validation
    )
    # The validation window is judged against what this campaign's search
    # would have produced from noise: its daily log returns must clear the
    # expected maximum t of the quick attempts spent so far.
    validation_haircut = haircut(
        [value for _, value in daily_log_returns(validation_result.equity_curve)],
        _campaign_trials(store, job_id),
    )
    compact_validation["haircut"] = validation_haircut
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
    validation_regime = validation_stats.get("regime") or {}
    stress_regime = stress_stats.get("regime") or {}
    specialized = bool(validation_regime.get("target_regimes"))
    validation_trades = _decision_trade_count(validation_stats)
    validation_return = float(
        (
            validation_regime.get("target_net_return")
            if specialized
            else validation_stats.get("net_return")
        )
        or 0.0
    )
    stress_return = float(
        (
            stress_regime.get("target_net_return")
            if specialized
            else stress_stats.get("net_return")
        )
        or 0.0
    )
    regime_config = load_constitution(root).get("evaluation", {}).get("regime", {})
    outside_loss_budget = float(regime_config.get("max_out_of_regime_loss_pct", 0.02))
    outside_loss_ok = not specialized or (
        float(validation_regime.get("outside_loss_pct") or 0.0) <= outside_loss_budget
        and float(stress_regime.get("outside_loss_pct") or 0.0) <= outside_loss_budget
    )
    train_regime = train_stats.get("regime") or {}
    train_return = float(
        (
            train_regime.get("target_net_return")
            if specialized
            else train_stats.get("net_return")
        )
        or 0.0
    )
    verdict = _full_dev_verdict(
        specialized=specialized,
        valid=train_valid and validation_valid and stress_valid,
        validation_trades=validation_trades,
        minimum_validation_trades=minimum_validation_trades,
        train_return=train_return,
        validation_return=validation_return,
        stress_return=stress_return,
        outside_loss_ok=outside_loss_ok,
        target_days=len(validation_regime.get("target_daily") or []),
        min_target_days=int(regime_config.get("min_target_days") or 10),
        audit_passed=bool(calibration["audit_passed"]),
        haircut_cleared=validation_haircut.get("cleared"),
        haircut_text=(
            f"t {validation_haircut['t_stat']} vs {validation_haircut['expected_max_t']} "
            f"expected from {validation_haircut['trials']} trials"
            if validation_haircut.get("t_stat") is not None
            else None
        ),
    )
    passed = bool(verdict["passed"])
    return {
        "status": verdict["status"],
        "full_dev_failure_codes": verdict["failure_codes"],
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
            "validation_trades": validation_trades,
            "minimum": minimum_validation_trades,
            "target": int(
                (manifest.get("policy") or {}).get("elite_participation_target_trades")
                or 12
            ),
        },
        "evidence": verdict["evidence"],
    }


def _protected_fold_full_dev(
    store: JobStore,
    job_id: str,
    candidate: dict[str, Any],
    *,
    campaign_id: str,
    root: Path,
    subject: dict[str, Any],
    params: dict[str, Any],
    stress_params: dict[str, Any],
    calibration: dict[str, Any],
    tuning: dict[str, Any] | None,
    revision: str,
    train_stats: dict[str, Any],
    compact_train: dict[str, Any],
    policy: Mapping[str, Any],
    certification_policy: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Certify locked candidate bytes on an agent-invisible chronological tail."""
    protected_root = _verified_protected_dataset_root(
        store, job_id, campaign_id, manifest=manifest
    )
    protected_subject = _load_subject(
        store,
        job_id,
        root,
        campaign_id=campaign_id,
        dataset_root=protected_root,
    )
    dataset: PreparedExecutionDataset = protected_subject["dataset"]
    timestamps = dataset.bars.timestamps
    evaluation_plan = dict(manifest.get("evaluation_plan") or {})
    fold_bounds, layout_error = _certification_fold_bounds(
        timestamps,
        evaluation_plan,
        folds=int(certification_policy["folds"]),
        minimum_bars=int(certification_policy["minimum_fold_bars"]),
    )
    if layout_error is not None:
        return {
            "status": "low_fidelity_rejected",
            "full_dev_failure_codes": ["insufficient_certification_history"],
            "revision": revision,
            "params": params,
            "tuning": tuning,
            "execution_calibration": calibration,
            "dev": {"train": compact_train},
            "objective": _objective(train_stats, params),
            "behavior": {},
            "elite_eligible": False,
            "elite_activity": {
                "validation_trades": 0,
                "minimum": int(policy.get("elite_min_validation_trades") or 8),
                "target": int(policy.get("elite_participation_target_trades") or 12),
            },
            "evaluation_plan": {
                **evaluation_plan,
                "certification_policy": dict(certification_policy),
            },
            "evidence": layout_error,
        }

    warmup = _strategy_warmup_bars(subject["script"], params)
    base_equity: list[dict[str, Any]] = []
    stress_equity: list[dict[str, Any]] = []
    base_trades: list[dict[str, Any]] = []
    stress_trades: list[dict[str, Any]] = []
    base_stats_rows: list[dict[str, Any]] = []
    stress_stats_rows: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    all_valid = True
    for fold, (start, end) in enumerate(fold_bounds):
        base_result, base_stats, scoped_equity, scoped_trades = _indexed_window_result(
            protected_subject, start=start, end=end, params=params, warmup=warmup
        )
        base_valid = bool(base_result.validation.get("execution_valid"))
        base_compact = _compact_result(base_result, stats=base_stats)
        base_stats_rows.append(base_stats)
        base_equity.extend(_chain_fold_equity(base_equity, scoped_equity))
        base_trades.extend(scoped_trades)
        if stress_params == params:
            stress_valid = base_valid
            stress_stats = dict(base_stats)
            stress_compact = {**base_compact, "reused_base_costs": True}
            scoped_stress_equity = list(scoped_equity)
            scoped_stress_trades = list(scoped_trades)
        else:
            stress_result, stress_stats, scoped_stress_equity, scoped_stress_trades = (
                _indexed_window_result(
                    protected_subject,
                    start=start,
                    end=end,
                    params=stress_params,
                    warmup=warmup,
                )
            )
            stress_valid = bool(stress_result.validation.get("execution_valid"))
            stress_compact = _compact_result(stress_result, stats=stress_stats)
            del stress_result
        stress_stats_rows.append(stress_stats)
        stress_equity.extend(_chain_fold_equity(stress_equity, scoped_stress_equity))
        stress_trades.extend(scoped_stress_trades)
        del base_result
        gc.collect()

        base_return = _decision_return(base_stats)
        stress_return = _decision_return(stress_stats)
        all_valid = all_valid and base_valid and stress_valid
        fold_rows.append(
            {
                "fold": fold,
                "test": {
                    "start": str(timestamps[start]),
                    "end": str(timestamps[end - 1]),
                    "bars": end - start,
                    "warmup_bars": min(warmup, start),
                },
                "base": base_compact,
                "stress": stress_compact,
                "base_return": round(base_return, 8),
                "stress_return": round(stress_return, 8),
                "positive": base_return > 0.0 and stress_return > 0.0,
            }
        )

    base_vector = objective_vector(base_equity, base_trades)
    stress_vector = objective_vector(stress_equity, stress_trades)
    base_regime = _pooled_regime_stats(base_stats_rows)
    stress_regime = _pooled_regime_stats(stress_stats_rows)
    pooled_base = _pooled_fold_stats(base_stats_rows, base_vector, base_regime)
    pooled_stress = _pooled_fold_stats(stress_stats_rows, stress_vector, stress_regime)
    validation_haircut = haircut(
        [value for _, value in daily_log_returns(base_equity)],
        _campaign_trials(store, job_id),
    )
    minimum_trades = int(policy.get("elite_min_validation_trades") or 8)
    constitution = load_constitution(root)
    regime_config = constitution.get("evaluation", {}).get("regime", {})
    specialized = bool(base_regime.get("target_regimes"))
    outside_budget = float(regime_config.get("max_out_of_regime_loss_pct") or 0.02)
    outside_loss_ok = not specialized or (
        float(base_regime.get("outside_loss_pct") or 0.0) <= outside_budget
        and float(stress_regime.get("outside_loss_pct") or 0.0) <= outside_budget
    )
    verdict = _protected_fold_verdict(
        fold_rows,
        valid=all_valid,
        validation_trades=sum(_decision_trade_count(row) for row in base_stats_rows),
        minimum_validation_trades=minimum_trades,
        pooled_return=_decision_return(pooled_base),
        pooled_stress_return=_decision_return(pooled_stress),
        train_return=_decision_return(train_stats),
        specialized=specialized,
        target_days=len(base_regime.get("target_daily") or []),
        min_target_days=int(regime_config.get("min_target_days") or 10),
        outside_loss_ok=outside_loss_ok,
        base_vector=base_vector,
        stress_vector=stress_vector,
        hard_constraints=constitution.get("hard_constraints") or {},
        required_positive_folds=int(certification_policy["required_positive_folds"]),
        max_fold_loss_pct=float(certification_policy["max_fold_loss_pct"]),
        audit_passed=bool(calibration["audit_passed"]),
        haircut_cleared=validation_haircut.get("cleared"),
    )
    validation_trades = sum(_decision_trade_count(row) for row in base_stats_rows)
    certificate_dataset = _slice(
        dataset, timestamps, fold_bounds[0][0], fold_bounds[-1][1]
    )
    synthetic = SimpleNamespace(trades=base_trades, stats=pooled_base)
    validation_block = {
        "stats": pooled_base,
        "validation": {"execution_valid": all_valid},
        "profile": {"mode": "protected_chronological_folds_v1"},
        "folds": fold_rows,
        "haircut": validation_haircut,
        "exits": receipt_exits({"trades": base_trades}),
        "forensics": _validation_forensics(synthetic, certificate_dataset),
    }
    objective = {
        key: round(float(base_vector[key]), 8)
        for key in (
            "net_log_growth",
            "downside_deviation",
            "tail_loss",
            "max_drawdown_pct",
        )
    }
    if specialized:
        objective["out_of_regime_loss_pct"] = round(
            float(base_regime.get("outside_loss_pct") or 0.0), 8
        )
    result_plan = {
        **evaluation_plan,
        "certification_policy": dict(certification_policy),
        "locked_revision": revision,
        "folds": [row["test"] for row in fold_rows],
    }
    return {
        "status": verdict["status"],
        "full_dev_failure_codes": verdict["failure_codes"],
        "revision": revision,
        "params": params,
        "tuning": tuning,
        "execution_calibration": calibration,
        "dev": {
            "train": compact_train,
            "validation": validation_block,
            "validation_stress": {
                "stats": pooled_stress,
                "validation": {"execution_valid": all_valid},
                "profile": {"mode": "protected_chronological_folds_v1"},
            },
        },
        "objective": objective,
        "behavior": _behavior(
            synthetic, certificate_dataset, protected_subject["spec"], stats=pooled_base
        ),
        "elite_eligible": bool(verdict["passed"]),
        "elite_activity": {
            "validation_trades": validation_trades,
            "minimum": minimum_trades,
            "target": int(policy.get("elite_participation_target_trades") or 12),
        },
        "evaluation_plan": result_plan,
        "evidence": verdict["evidence"],
    }


def _indexed_window_result(
    subject: Mapping[str, Any],
    *,
    start: int,
    end: int,
    params: dict[str, Any],
    warmup: int,
) -> tuple[Any, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    dataset: PreparedExecutionDataset = subject["dataset"]
    timestamps = dataset.bars.timestamps
    evaluation = _slice(dataset, timestamps, max(0, start - warmup), end)
    result = simulate_execution(subject["script"], evaluation, subject["spec"], params)
    boundary = timestamps[start]
    stats = _test_window_stats(result, boundary, subject["spec"], params)
    equity = [
        row for row in result.equity_curve if pd.Timestamp(row["timestamp"]) >= boundary
    ]
    trades = [
        row for row in result.trades if pd.Timestamp(row["timestamp"]) >= boundary
    ]
    return result, stats, equity, trades


def _certification_fold_bounds(
    timestamps: Sequence[pd.Timestamp],
    evaluation_plan: Mapping[str, Any],
    *,
    folds: int,
    minimum_bars: int,
) -> tuple[list[tuple[int, int]], str | None]:
    certification = evaluation_plan.get("certification") or {}
    start_raw = certification.get("start")
    audit_raw = (evaluation_plan.get("audit") or {}).get("start")
    if not timestamps or not start_raw or not audit_raw:
        return [], "protected certification boundaries are unavailable"
    start_stamp = pd.Timestamp(start_raw)
    audit_stamp = pd.Timestamp(audit_raw)
    start = next(
        (index for index, stamp in enumerate(timestamps) if stamp >= start_stamp),
        len(timestamps),
    )
    end = next(
        (index for index, stamp in enumerate(timestamps) if stamp >= audit_stamp),
        len(timestamps),
    )
    available = end - start
    if available < folds * minimum_bars:
        return [], (
            f"protected certification has {available} bars; needs at least "
            f"{folds * minimum_bars} for {folds} folds"
        )
    bounds = [
        (
            start + available * index // folds,
            start + available * (index + 1) // folds,
        )
        for index in range(folds)
    ]
    return bounds, None


def _decision_return(stats: Mapping[str, Any]) -> float:
    regime = stats.get("regime") or {}
    return float(
        (
            regime.get("target_net_return")
            if regime.get("target_regimes")
            else stats.get("net_return")
        )
        or 0.0
    )


def _pooled_regime_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    regimes = [row.get("regime") or {} for row in rows]
    specialized = next((row for row in regimes if row.get("target_regimes")), None)
    if specialized is None:
        return {}
    target_daily = [item for row in regimes for item in (row.get("target_daily") or [])]
    outside_daily = [
        item for row in regimes for item in (row.get("outside_daily") or [])
    ]
    target_growth = sum(float(value) for _, value in target_daily)
    outside_growth = sum(float(value) for _, value in outside_daily)
    return {
        "target_regimes": list(specialized.get("target_regimes") or []),
        "target_daily": target_daily,
        "outside_daily": outside_daily,
        "target_net_log_growth": target_growth,
        "target_net_return": math.expm1(target_growth),
        "outside_loss_pct": max(0.0, 1.0 - math.exp(outside_growth)),
    }


def _pooled_fold_stats(
    rows: Sequence[Mapping[str, Any]],
    vector: Mapping[str, Any],
    regime: Mapping[str, Any],
) -> dict[str, Any]:
    trade_count = sum(_decision_trade_count(dict(row)) for row in rows)
    weighted_duration = sum(
        float(row.get("avg_trade_duration_s") or 0.0) * _decision_trade_count(dict(row))
        for row in rows
    )
    return {
        "net_return": math.expm1(float(vector["net_log_growth"])),
        "trade_count": trade_count,
        "max_drawdown_pct": -float(vector["max_drawdown_pct"]),
        "total_fees": sum(float(row.get("total_fees") or 0.0) for row in rows),
        "total_turnover_usd": sum(
            float(row.get("total_turnover_usd") or 0.0) for row in rows
        ),
        "avg_trade_duration_s": (
            weighted_duration / trade_count if trade_count else 0.0
        ),
        "regime": dict(regime),
    }


def _protected_fold_verdict(
    folds: Sequence[Mapping[str, Any]],
    *,
    valid: bool,
    validation_trades: int,
    minimum_validation_trades: int,
    pooled_return: float,
    pooled_stress_return: float,
    train_return: float,
    specialized: bool,
    target_days: int,
    min_target_days: int,
    outside_loss_ok: bool,
    base_vector: Mapping[str, Any],
    stress_vector: Mapping[str, Any],
    hard_constraints: Mapping[str, Any],
    required_positive_folds: int,
    max_fold_loss_pct: float,
    audit_passed: bool,
    haircut_cleared: bool | None,
) -> dict[str, Any]:
    awaiting = specialized and valid and target_days < min_target_days
    positive_folds = sum(bool(row.get("positive")) for row in folds)
    overdrawn = [
        int(row.get("fold") or 0)
        for row in folds
        if min(
            float(row.get("base_return") or 0.0),
            float(row.get("stress_return") or 0.0),
        )
        < -max_fold_loss_pct
    ]
    max_drawdown = max(
        float(base_vector.get("max_drawdown_pct") or 0.0),
        float(stress_vector.get("max_drawdown_pct") or 0.0),
    )
    max_tail = max(
        float(base_vector.get("tail_loss") or 0.0),
        float(stress_vector.get("tail_loss") or 0.0),
    )
    drawdown_ceiling = float(hard_constraints.get("max_drawdown_pct") or 0.25)
    tail_ceiling = float(hard_constraints.get("max_tail_loss") or 0.15)
    failure_codes: list[str] = []
    if not valid:
        failure_codes.append("invalid_execution")
    if validation_trades < minimum_validation_trades:
        failure_codes.append("activity_below_floor")
    if positive_folds < required_positive_folds:
        failure_codes.append("insufficient_positive_folds")
    if overdrawn:
        failure_codes.append("fold_loss_bound")
    if pooled_return <= 0.0:
        failure_codes.append(
            "negative_in_target_regime" if specialized else "negative_after_costs"
        )
    if pooled_stress_return <= 0.0:
        failure_codes.append("negative_under_stress")
    if not outside_loss_ok:
        failure_codes.append("out_of_regime_loss_budget")
    if max_drawdown > drawdown_ceiling:
        failure_codes.append("hard_drawdown_ceiling")
    if max_tail > tail_ceiling:
        failure_codes.append("hard_tail_loss_ceiling")
    if not audit_passed:
        failure_codes.append("execution_cost_audit")
    if haircut_cleared is False:
        failure_codes.append("validation_not_significant_after_trials")
    if train_return > 0.0 and pooled_return < 0.0:
        failure_codes.append("screen_inversion")
    passed = not awaiting and not failure_codes
    if passed:
        return {
            "status": "dev_frontier",
            "passed": True,
            "failure_codes": [],
            "evidence": (
                f"protected chronological certificate passed: {positive_folds}/"
                f"{len(folds)} positive base-and-stress folds"
            ),
        }
    if awaiting:
        return {
            "status": "awaiting_regime",
            "passed": False,
            "failure_codes": [],
            "evidence": (
                f"protected folds held {target_days} target-regime days, below "
                f"{min_target_days}; not evidence against the idea"
            ),
        }
    return {
        "status": "low_fidelity_rejected",
        "passed": False,
        "failure_codes": list(dict.fromkeys(failure_codes)),
        "evidence": (
            f"protected chronological certificate failed: {positive_folds}/"
            f"{len(folds)} positive folds"
            + (f"; fold loss bound breached in {overdrawn}" if overdrawn else "")
        ),
    }


_FORENSICS_MAX_CLOSES = 120
_FORENSICS_KEEP = (
    "count",
    "avg_realized_bps",
    "avg_hold_mae_bps",
    "avg_hold_mfe_bps",
    "avg_post_exit_favorable_bps",
    "stop_survival_rate",
)


def _validation_forensics(result: Any, dataset: Any) -> dict[str, Any]:
    """The candidate's own exit-quality view on the validation window: per
    exit reason, adverse/favorable excursion while held, what the price did
    after the exit, and which stop widths the winners would have survived.

    This is the same aggregate the baseline backtest writes for the incumbent
    (``results/backtest/trade_forensics.json``), so a design can compare the
    two directly. Best-effort like the baseline's: a forensics failure is not
    evidence against the candidate.
    """
    try:
        trades = list(result.trades or [])
        closes = [
            {
                **row,
                "price": row.get("avg_price"),
                "closed_at": row.get("timestamp"),
                "net_pnl": row.get("realized_pnl_delta"),
            }
            for row in trades
            if row.get("reduce_only")
        ][-_FORENSICS_MAX_CLOSES:]
        if not closes:
            return {"closes": 0, "by_exit_reason": {}}
        view = dataset.bars
        bars_by_symbol = {symbol: view.symbol_frame(symbol) for symbol in view.symbols}
        rows = forensics_for_closed_trades(bars_by_symbol, closes, trades)
        aggregate = aggregate_trade_forensics(rows)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not reject a candidate
        return {"error": str(exc)[:240]}
    return {
        "closes": len(rows),
        "by_exit_reason": {
            reason: {key: cell.get(key) for key in _FORENSICS_KEEP if key in cell}
            for reason, cell in (aggregate.get("by_exit_reason") or {}).items()
        },
    }


def _probe_mismatch_text(probe: Mapping[str, Any]) -> str:
    rows = probe.get("mismatches") or []
    if not rows:
        return "intents differ"
    parts = []
    for row in rows[:3]:
        gap = row.get("rel")
        parts.append(
            f"{row.get('path')} {row.get('base')} vs {row.get('wide')}"
            + (f" ({10_000 * float(gap):.1f} bps)" if gap is not None else "")
        )
    return "; ".join(parts)


def _full_dev_verdict(
    *,
    specialized: bool,
    valid: bool,
    validation_trades: int,
    minimum_validation_trades: int,
    train_return: float,
    validation_return: float,
    stress_return: float,
    outside_loss_ok: bool,
    target_days: int,
    min_target_days: int,
    audit_passed: bool,
    haircut_cleared: bool | None = None,
    haircut_text: str | None = None,
) -> dict[str, Any]:
    """Status, evidence, and failure codes for one full-development result.

    A specialist whose validation window never contained enough of its
    declared regime is not refuted by that window; it waits for the regime.
    """
    awaiting = specialized and valid and target_days < min_target_days
    passed = (
        valid
        and not awaiting
        and validation_trades >= minimum_validation_trades
        and validation_return > 0.0
        and stress_return > 0.0
        and outside_loss_ok
        and audit_passed
        and haircut_cleared is not False
    )
    failure_codes: list[str] = []
    if not passed and not awaiting:
        if validation_trades < minimum_validation_trades:
            failure_codes.append("activity_below_floor")
        if validation_return <= 0.0:
            failure_codes.append(
                "negative_in_target_regime" if specialized else "negative_after_costs"
            )
        if haircut_cleared is False:
            failure_codes.append("validation_not_significant_after_trials")
        if train_return > 0.0 and validation_return < 0.0:
            failure_codes.append("screen_inversion")
        if not outside_loss_ok:
            failure_codes.append("out_of_regime_loss_budget")
    if passed:
        status = "dev_frontier"
        evidence = "positive independent validation with sufficient activity" + (
            f"; cleared the trial haircut ({haircut_text})" if haircut_text else ""
        )
    elif awaiting:
        status = "awaiting_regime"
        evidence = (
            f"validation window held {target_days} target-regime days, below "
            f"{min_target_days}; not evidence against the idea"
        )
    else:
        status = "low_fidelity_rejected"
        evidence = (
            "failed independent validation: activity below elite floor"
            if validation_trades < minimum_validation_trades
            else (
                f"failed independent validation: not significant after the "
                f"campaign's trials ({haircut_text})"
                if haircut_cleared is False and validation_return > 0.0
                else "failed independent validation"
            )
        )
    return {
        "status": status,
        "passed": passed,
        "evidence": evidence,
        "failure_codes": failure_codes,
    }


def _load_subject(
    store: JobStore,
    job_id: str,
    root: Path,
    *,
    campaign_id: str | None = None,
    dataset_root: Path | None = None,
    include_store_features: bool = False,
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
    # The subject bundle owns its workspace/ feature files; the protected
    # dataset root owns the store. Gating must load exactly what probation
    # and the live driver will, or a bundle-owned file activates ungated.
    dataset = _load_dataset(
        resolved_dataset_root,
        spec,
        job_data,
        feature_roots=(root, resolved_dataset_root),
        include_store_features=include_store_features,
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


_SCREEN_SLICE_BARS = 10_000
_SCREEN_MIN_EARLIER_BARS = 2_000
# Macro regime of a window (see regime.MACRO_*): the universe-median
# cumulative close-to-close return over it, at the scale a designer means by
# "bear flipping to bull".
_MACRO_WEEK_MOVE = 0.08
_MACRO_LEG_WEEKS = 4
_MACRO_LEG_RETURN = 0.15
_SCREEN_MACRO_STEP_BARS = 2_500
_macro_label = macro_label


def _slice_macro_regime(dataset: PreparedExecutionDataset) -> dict[str, Any]:
    view = dataset.bars
    by_symbol: dict[str, float] = {}
    for symbol in view.symbols:
        closes = view.symbol_frame(symbol)["close"].astype(float)
        if len(closes) >= 2 and float(closes.iloc[0]) > 0:
            by_symbol[symbol] = float(closes.iloc[-1]) / float(closes.iloc[0]) - 1.0
    median = statistics.median(by_symbol.values()) if by_symbol else 0.0
    timestamps = view.timestamps
    return {
        "label": _macro_label(median),
        "median_return": round(median, 4),
        "by_symbol": {symbol: round(value, 4) for symbol, value in by_symbol.items()},
        "start": timestamps[0].isoformat() if len(timestamps) else None,
        "end": timestamps[-1].isoformat() if len(timestamps) else None,
    }


def _macro_regime_context(frame: pd.DataFrame) -> dict[str, Any]:
    """Universe-median returns over the recent 7, 28 and 90 days, plus what
    the whole window covers in bull, bear and chop weeks and whether it
    holds a multi-week leg of each sign. A design can only be validated in
    a regime the window contains; the pack says so instead of letting the
    screen certify a bear-fitted book the week the market turns."""
    close = (
        frame.assign(timestamp=pd.to_datetime(frame["timestamp"], utc=True))
        .pivot_table(index="timestamp", columns="symbol", values="close")
        .sort_index()
        .astype(float)
    )
    if close.empty:
        raise ValueError("campaign dataset has no closes")
    end = close.index[-1]
    recent: dict[str, Any] = {}
    for days in (7, 28, 90):
        window = close[close.index > end - pd.Timedelta(days=days)]
        first, last = window.iloc[0], window.iloc[-1]
        returns = (last / first - 1.0).dropna()
        median = float(returns.median()) if len(returns) else 0.0
        recent[f"{days}d"] = {
            "median_return": round(median, 4),
            "label": _macro_label(median),
            "by_symbol": {str(k): round(float(v), 4) for k, v in returns.items()},
        }
    weekly = close.resample("7D", origin="end").last()
    weekly_median = (weekly / weekly.shift(1) - 1.0).median(axis=1).dropna()
    bull_weeks = int((weekly_median >= _MACRO_WEEK_MOVE).sum())
    bear_weeks = int((weekly_median <= -_MACRO_WEEK_MOVE).sum())
    leg = (weekly / weekly.shift(_MACRO_LEG_WEEKS) - 1.0).median(axis=1).dropna()
    return {
        "basis": "universe-median close-to-close return",
        "as_of": end.isoformat(),
        "recent": recent,
        "coverage": {
            "weeks": int(len(weekly_median)),
            "bull_weeks": bull_weeks,
            "bear_weeks": bear_weeks,
            "chop_weeks": int(len(weekly_median) - bull_weeks - bear_weeks),
            "has_bull_leg": bool((leg >= _MACRO_LEG_RETURN).any()),
            "has_bear_leg": bool((leg <= -_MACRO_LEG_RETURN).any()),
            "leg_weeks": _MACRO_LEG_WEEKS,
            "leg_return": _MACRO_LEG_RETURN,
        },
    }


def _store_latest_values(
    store_path: Path, names: Collection[str]
) -> dict[str, tuple[str, float]]:
    """Newest (timestamp, value) per feature name in one streamed scan of the
    campaign's frozen store."""
    latest: dict[str, tuple[str, float]] = {}
    if not store_path.exists():
        return latest
    needles = {name: f'"name": "{name}"' for name in names}
    with store_path.open(encoding="utf-8") as handle:
        for line in handle:
            hit = next(
                (name for name, needle in needles.items() if needle in line), None
            )
            if hit is None:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            stamp = str(row.get("timestamp") or "")
            if stamp > latest.get(hit, ("", 0.0))[0]:
                latest[hit] = (stamp, float(row.get("value") or 0.0))
    return latest


def _leader_context(latest: Mapping[str, tuple[str, float]]) -> dict[str, Any] | None:
    """The leaders' latest returns and broad state from the frozen store,
    plus how a strategy declares and reads the runtime column."""
    if LEADER_FEATURE_NAME not in latest:
        return None
    stamp, code = latest[LEADER_FEATURE_NAME]
    state = next(
        (name for name, value in LEADER_CODES.items() if float(value) == code),
        "neutral",
    )
    ret_7d = {
        symbol: latest[f"{symbol.lower()}_ret_7d"][1]
        for symbol in LEADER_SYMBOLS
        if f"{symbol.lower()}_ret_7d" in latest
    }
    if LEADER_RETURN_FEATURE_NAMES[0] in latest:
        ret_7d["median"] = latest[LEADER_RETURN_FEATURE_NAMES[0]][1]
    ret_28d = (
        {"median": latest[LEADER_RETURN_FEATURE_NAMES[1]][1]}
        if LEADER_RETURN_FEATURE_NAMES[1] in latest
        else {}
    )
    return {
        "symbols": list(LEADER_SYMBOLS),
        "ret_7d": ret_7d,
        "ret_28d": ret_28d,
        "state": state,
        "as_of": stamp,
        "thresholds": {"rally": LEADER_RALLY_RETURN, "selloff": LEADER_SELLOFF_RETURN},
        "runtime_feature": {
            "name": LEADER_FEATURE_NAME,
            "columns": list(leader_feature_names()),
            "codes": {label: int(value) for label, value in LEADER_CODES.items()},
            "declare": {"name": LEADER_FEATURE_NAME, "source": "file"},
            "read": f"ctx.view.feature({LEADER_FEATURE_NAME!r}, default=0.0)",
        },
    }


def _macro_runtime_feature(*, available: bool) -> dict[str, Any]:
    """Whether the campaign's frozen feature store carries the macro label a
    strategy can read at decision time, and how to consume it."""
    return {
        "available": available,
        "name": MACRO_FEATURE_NAME,
        "columns": [MACRO_FEATURE_NAME, *MACRO_RETURN_FEATURE_NAMES],
        "codes": {label: int(code) for label, code in MACRO_CODES.items()},
        "declare": {"name": MACRO_FEATURE_NAME, "source": "file"},
        "read": f"ctx.view.feature({MACRO_FEATURE_NAME!r}, default=0.0)",
        "cadence": "hourly, causal (as-of the last completed bar)",
    }


def macro_regime_sentence(macro: Mapping[str, Any] | None) -> str:
    if not macro or not macro.get("recent"):
        return ""
    recent = macro["recent"]
    coverage = macro.get("coverage") or {}
    seven, month = recent.get("7d") or {}, recent.get("28d") or {}
    text = (
        f"Macro regime: last 7 days universe-median "
        f"{100 * float(seven.get('median_return') or 0):+.0f}% ({seven.get('label')}), "
        f"last 28 days {100 * float(month.get('median_return') or 0):+.0f}% "
        f"({month.get('label')}); the window holds {coverage.get('bull_weeks')} bull, "
        f"{coverage.get('bear_weeks')} bear and {coverage.get('chop_weeks')} chop weeks"
    )
    missing = [
        name
        for name, key in (("bull", "has_bull_leg"), ("bear", "has_bear_leg"))
        if not coverage.get(key)
    ]
    if missing:
        text += (
            f"; it contains no {' or '.join(missing)} leg of {coverage.get('leg_weeks')}+ "
            "weeks, so a design that earns only there cannot be validated here"
        )
    leaders = macro.get("leaders") or {}
    if leaders:
        ret_7d = leaders.get("ret_7d") or {}
        named = ", ".join(
            f"{symbol} {100 * float(ret_7d[symbol]):+.0f}%"
            for symbol in leaders.get("symbols") or []
            if symbol in ret_7d
        )
        state = {"rally": "broad rally", "selloff": "broad selloff"}.get(
            str(leaders.get("state")), "neither"
        )
        month = (leaders.get("ret_28d") or {}).get("median")
        text += f"; leaders {named} over 7 days ({state})"
        if month is not None:
            text += f", median {100 * float(month):+.0f}% over 28 days"
    text += ". Say which macro regime each hypothesis earns in. "
    runtime = macro.get("runtime_feature") or {}
    if runtime.get("available"):
        text += (
            f"The same label is a runtime feature column: declare "
            f"{json.dumps(runtime.get('declare'))} under "
            "execution_spec.data_contract.features in the candidate job.yaml and "
            f"read {runtime.get('read')} in decide() (1 bull, 0 chop, -1 bear; "
            f"{', '.join(MACRO_RETURN_FEATURE_NAMES)} alongside), refreshed hourly "
            "and causal, so a strategy can condition its entries on the macro "
            "regime its hypothesis names. A book that earns in one macro regime "
            "may keep that mechanism gated on the column and run a different "
            "mechanism in the other regime, or stand aside there: the screen "
            "judges the book as a whole (positive and significant against the "
            f"incumbent across both slices) while no slice may give back more than "
            f"{100 * SCREEN_SLICE_MAX_LOSS:.0f}%, and a regime-conditioned book "
            "gets a complexity budget per branch. The default covers the bars before the "
            "column's first value (28 days of history for the macro label, 7 for "
            "the leader state); an undefaulted read raises there, and a column "
            "that is read must be declared or the read fails live. "
        )
    leader_runtime = (leaders or {}).get("runtime_feature") or {}
    if leader_runtime:
        text += (
            f"{LEADER_FEATURE_NAME} is a second column the same way (declare "
            f"{json.dumps(leader_runtime.get('declare'))}, read "
            f"{leader_runtime.get('read')}; +1 broad rally when "
            f"every leader's 7-day return exceeds {100 * LEADER_RALLY_RETURN:+.0f}%, "
            f"-1 broad selloff below {100 * LEADER_SELLOFF_RETURN:+.0f}%, 0 otherwise; "
            f"{', '.join(leader_runtime.get('columns') or [])} alongside). It has no "
            "forward-return edge: broad-rally days concentrate a short book's "
            "losses and broad-selloff days a long-fade book's, so use it to gate "
            "the side being run over, not to pick direction. "
        )
    return text


_SCREEN_BOOTSTRAP_BLOCK_DAYS = 2
_SCREEN_BOOTSTRAP_ITERATIONS = 300


def _policy_screen_slices(
    train: PreparedExecutionDataset, policy: Mapping[str, Any]
) -> list[tuple[str, PreparedExecutionDataset]]:
    return _screen_slices(
        train,
        slices=int(policy.get("screen_slices") or 2),
        days=policy.get("screen_slice_days"),
    )


def _dataset_bar_seconds(train: PreparedExecutionDataset) -> int:
    stamps = train.bars.timestamps
    if len(stamps) < 2:
        return 0
    diffs = sorted(
        (later - earlier).total_seconds()
        for earlier, later in zip(stamps[:200], stamps[1:201], strict=False)
        if later > earlier
    )
    return int(diffs[len(diffs) // 2]) if diffs else 0


def _screen_slices(
    train: PreparedExecutionDataset,
    *,
    bars: int = _SCREEN_SLICE_BARS,
    slices: int = 2,
    days: float | None = None,
) -> list[tuple[str, PreparedExecutionDataset]]:
    """Disjoint screen windows: the recent tail plus the tail before it.

    A candidate repaired against one slice is selected for that slice; a
    second, disjoint slice is the cheapest test that it generalizes at all.
    Short datasets fall back to the single recent slice. With ``days`` the
    window is sized in calendar time from the bar interval (35 days is
    10,080 five-minute bars or 210 four-hour bars) so a slow lane gets
    slices instead of one multi-year window.
    """
    timestamps = train.bars.timestamps
    total = len(timestamps)
    step = _SCREEN_MACRO_STEP_BARS
    min_earlier = _SCREEN_MIN_EARLIER_BARS
    if days is not None:
        bar_seconds = _dataset_bar_seconds(train)
        if bar_seconds > 0:
            bars = max(50, int(round(float(days) * 86_400.0 / bar_seconds)))
            step = max(1, bars // 4)
            min_earlier = min(_SCREEN_MIN_EARLIER_BARS, bars)
    recent = _tail(train, bars)
    out = [("recent", recent)]
    remaining = total - min(bars, total)
    if slices >= 2 and remaining >= min_earlier:
        out.append(
            (
                "earlier",
                _earlier_screen_slice(train, remaining, bars, recent, step=step),
            )
        )
    return out


def _earlier_screen_slice(
    train: PreparedExecutionDataset,
    remaining: int,
    bars: int,
    recent: PreparedExecutionDataset,
    *,
    step: int = _SCREEN_MACRO_STEP_BARS,
) -> PreparedExecutionDataset:
    """The most recent disjoint window whose macro regime differs from the
    recent slice's; adjacent windows share a regime, and two slices of one
    regime cannot say whether a design survives the other. Falls back to
    the adjacent window when the history holds only one regime."""
    timestamps = train.bars.timestamps
    adjacent = _slice(train, timestamps, max(0, remaining - bars), remaining)
    recent_label = _slice_macro_regime(recent)["label"]
    end = remaining
    while end - bars >= 0:
        candidate = _slice(train, timestamps, end - bars, end)
        if _slice_macro_regime(candidate)["label"] != recent_label:
            return candidate
        end -= step
    return adjacent


def _screen_confidence(
    attempt_index: int, *, base: float = 0.70, step: float = 0.10, cap: float = 0.90
) -> float:
    """Each repair is another look at the same slices; the bar rises with it."""
    return round(min(cap, base + step * max(0, attempt_index - 1)), 4)


def _screen_slice_report(
    result: Any,
    reference_daily: Sequence[tuple[str, float]] | None,
    *,
    confidence: float,
    leader_days: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    candidate_daily = daily_log_returns(result.equity_curve)
    deltas = (
        paired_daily_deltas(
            [(str(day), float(value)) for day, value in reference_daily],
            candidate_daily,
        )
        if reference_daily
        else [value for _, value in candidate_daily]
    )
    report: dict[str, Any] = {
        "net_return": float(result.stats.get("net_return") or 0.0),
        "trade_count": int(result.stats.get("trade_count") or 0),
        # Positive fraction like the gate's ceiling; the simulator reports the
        # drawdown as a negative return.
        "max_drawdown_pct": round(
            abs(float(result.stats.get("max_drawdown_pct") or 0.0)), 6
        ),
        "paired_days": len(deltas),
        "lcb": _screen_lcb(deltas, confidence),
        # Pooled by the verdict into one whole-book bound, then dropped.
        "deltas": [round(float(value), 8) for value in deltas],
    }
    attribution = _leader_attribution(candidate_daily, leader_days or {})
    if attribution:
        report["leader_attribution"] = attribution
    if reference_daily:
        # Where the reference lost is where an improvement is both most
        # valuable and easiest to see: the effect is concentrated on few days.
        reference_map = {str(day): float(value) for day, value in reference_daily}
        losing = [
            value - reference_map[day]
            for day, value in candidate_daily
            if day in reference_map and reference_map[day] < 0
        ]
        winning = [
            value - reference_map[day]
            for day, value in candidate_daily
            if day in reference_map and reference_map[day] >= 0
        ]
        report["failure_mode"] = {
            "losing_days": len(losing),
            "losing_delta": round(sum(losing), 8),
            "losing_lcb": _screen_lcb(losing, confidence),
            "winning_days": len(winning),
            "winning_delta": round(sum(winning), 8),
        }
    return report


def _screen_lcb(deltas: Sequence[float], confidence: float) -> float | None:
    if len(deltas) < 4:
        return None
    lcb = block_bootstrap_lcb(
        list(deltas),
        block_len=_SCREEN_BOOTSTRAP_BLOCK_DAYS,
        iterations=_SCREEN_BOOTSTRAP_ITERATIONS,
        confidence=confidence,
    )
    return None if lcb is None else round(float(lcb), 8)


# Winning-day tolerance for the failure-mode route: a repair may not cost
# more than this (log return over the slice) where the reference was earning.
_SCREEN_NON_INFERIORITY_TOLERANCE = 0.005
_SCREEN_FAILURE_MODE_MIN_DAYS = 3


def _slice_route(row: Mapping[str, Any]) -> str | None:
    """How a positive slice clears the bar: global significance, or by
    repairing the reference's losing days without hurting its winning days."""
    lcb = row.get("lcb")
    if lcb is not None and float(lcb) > 0:
        return "global"
    mode = row.get("failure_mode") or {}
    if (
        int(mode.get("losing_days") or 0) >= _SCREEN_FAILURE_MODE_MIN_DAYS
        and float(mode.get("losing_delta") or 0.0) > 0
        and (mode.get("losing_lcb") is None or float(mode["losing_lcb"]) > 0)
        and float(mode.get("winning_delta") or 0.0)
        >= -_SCREEN_NON_INFERIORITY_TOLERANCE
    ):
        return "failure_mode"
    if lcb is None:
        return "point_estimate"
    return None


# The most a single screen slice may give back for an all-weather book: it
# may stand aside in a regime, or give a little back, but not bleed there.
SCREEN_SLICE_MAX_LOSS = 0.02


def _screen_verdict(
    slices: Mapping[str, Mapping[str, Any]],
    *,
    min_trades: int,
    pooled_lcb: float | None = None,
    max_slice_loss: float = SCREEN_SLICE_MAX_LOSS,
    cost_coverage: float | None = None,
    cost_hurdle: float = COST_HURDLE_MULTIPLE,
    cost_basis: str | None = None,
) -> dict[str, Any]:
    """The all-weather bar. The book as a whole must be positive and
    significant against the reference (pooled paired deltas, or every slice
    clearing its own route) and populated across the slices together; no
    slice may give back more than ``max_slice_loss``. A slice where the book
    stood aside is zero trades and zero return, and passes: flipping off in a
    regime is allowed, bleeding in it is not."""
    nets = {label: float(row["net_return"]) for label, row in slices.items()}
    combined = 1.0
    for value in nets.values():
        combined *= 1.0 + value
    combined -= 1.0
    total_trades = sum(int(row["trade_count"]) for row in slices.values())
    routes = {label: _slice_route(row) for label, row in slices.items()}
    significant = (pooled_lcb is not None and float(pooled_lcb) > 0) or all(
        route is not None for route in routes.values()
    )
    overdrawn = [label for label, value in nets.items() if value < -max_slice_loss]
    code: str | None = None
    if total_trades < min_trades:
        code = "activity_below_floor"
    elif cost_coverage is not None and float(cost_coverage) < cost_hurdle:
        # Cost arithmetic first: a book whose trades do not capture the
        # hurdle multiple of the round trip gross is paying to trade, whatever
        # the slices say.
        code = "cost_not_covered"
    elif overdrawn:
        code = "screen_slice_loss_bound"
    # Significance is recorded, not required: 35-day slices cannot certify a
    # realistic edge (a lower bound above zero needs an annualized Sharpe
    # near 1.7 at 70%), so the screen filters and full development certifies
    # on the whole validation window against the campaign's trial count.
    return {
        "passed": combined > 0 and code is None,
        "code": code,
        "edge_significant": significant,
        "combined_net_return": round(combined, 6),
        "pooled_lcb": pooled_lcb,
        "cost_coverage": cost_coverage,
        "cost_hurdle": cost_hurdle,
        "cost_basis": cost_basis,
        "total_trades": total_trades,
        "max_slice_loss": max_slice_loss,
        "overdrawn": overdrawn,
        "slices": {
            label: {**dict(row), "route": routes[label]}
            for label, row in slices.items()
        },
    }


def _screen_min_trades(
    policy: Mapping[str, Any], manifest: Mapping[str, Any], result: Any
) -> int:
    """The cadence-band floor prorated to the slice's span (at least one)."""
    floor = _min_fills_per_day(policy, manifest.get("dataset"))
    curve = list(getattr(result, "equity_curve", None) or [])
    if floor is None or len(curve) < 2:
        return 1
    try:
        days = (
            pd.Timestamp(curve[-1]["timestamp"]) - pd.Timestamp(curve[0]["timestamp"])
        ).total_seconds() / 86_400.0
    except (KeyError, TypeError, ValueError):
        return 1
    return max(1, math.ceil(floor * days))


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
    regime = stats.get("regime") or {}
    if regime.get("target_regimes"):
        target_daily = [float(value) for _, value in regime.get("target_daily") or []]
        downside = (
            math.sqrt(
                sum(value * value for value in target_daily if value < 0)
                / len(target_daily)
            )
            if target_daily
            else 0.0
        )
        outside_loss = float(regime.get("outside_loss_pct") or 0.0)
        return {
            "net_log_growth": round(
                float(regime.get("target_net_log_growth") or 0.0), 8
            ),
            "downside_deviation": round(downside, 8),
            "tail_loss": 0.0,
            "max_drawdown_pct": round(
                abs(float(stats.get("max_drawdown_pct") or 0.0)), 8
            ),
            "out_of_regime_loss_pct": round(outside_loss, 8),
        }
    net = max(float(stats.get("net_return") or 0.0), -0.999999)
    capital = float(params.get("initial_capital") or 10_000.0)
    worst = min(float(stats.get("worst_trade_pnl") or 0.0), 0.0)
    return {
        "net_log_growth": round(math.log1p(net), 8),
        "downside_deviation": round(abs(float(stats.get("avg_drawdown") or 0.0)), 8),
        "tail_loss": round(abs(worst) / max(capital, 1.0), 8),
        "max_drawdown_pct": round(abs(float(stats.get("max_drawdown_pct") or 0.0)), 8),
    }


def _decision_trade_count(stats: dict[str, Any]) -> int:
    # Activity protects the frontier from sparse gate stacks.  Count the whole
    # strategy because transition trades can enter before their target regime.
    return int(stats.get("trade_count") or 0)


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
        - float(objective.get("out_of_regime_loss_pct") or 0.0)
    )


# Validation checks that are authoring mistakes with a mechanical fix: the
# submission is handed back uncharged with the check's hint.
_CONTRACT_CHECKS = frozenset(
    {
        "no_bounded_index_clock",
        "declared_features_valid",
        "feature_policy_replayable",
        "undeclared_feature_read",
    }
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
    store: JobStore,
    job_id: str,
    campaign_root: Path,
    *,
    dataset_symbols: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    job_data = _load_job_yaml(campaign_root / "source")
    target_params = dict(job_data.get("execution_params") or {})
    target_symbols = {str(symbol) for symbol in target_params.get("symbols") or []}
    universe = target_symbols or {str(symbol) for symbol in dataset_symbols} or None
    # Without declared target symbols the starter's own list survives install,
    # so it must fit the frozen universe too.
    target_overrides_symbols = bool(target_symbols)
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
                **_starter_compatibility(
                    definition,
                    universe,
                    target_overrides_symbols=target_overrides_symbols,
                ),
            }
        )
    return snapshots


def _starter_required_symbols(definition: StarterDefinition) -> set[str]:
    """Symbol literals a starter's params embed (sleeves, pairs, ...).

    ``symbols`` itself is replaced by the target job at install time; the
    literals inside other params are not, and they are the KeyError site.
    """
    declared = {str(symbol) for symbol in definition.symbols}
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, str):
            if value in declared:
                found.add(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                visit(item)
        elif isinstance(value, list | tuple | set):
            for item in value:
                visit(item)

    params = dict(definition.configured_params())
    params.pop("symbols", None)
    visit(params)
    # Strategy classes carry their own ``default_params`` (sleeves, pairs);
    # those literals survive install exactly like the definition's params.
    module = importlib.import_module(definition.module)
    for attribute in vars(module).values():
        defaults = getattr(attribute, "default_params", None)
        if isinstance(attribute, type) and isinstance(defaults, Mapping):
            visit({key: value for key, value in defaults.items() if key != "symbols"})
    return found


def _starter_compatibility(
    definition: StarterDefinition,
    universe: set[str] | None,
    *,
    target_overrides_symbols: bool = True,
) -> dict[str, Any]:
    required_set = _starter_required_symbols(definition)
    if not target_overrides_symbols:
        required_set |= {str(symbol) for symbol in definition.symbols}
    required = sorted(required_set)
    if universe is None:
        # Unknown universe (no declared symbols, no frozen bars): nothing to
        # check against, so the starter stays selectable.
        return {"required_symbols": required, "compatible": True}
    missing = [symbol for symbol in required if symbol not in universe]
    reason: str | None = None
    if missing:
        reason = (
            f"requires symbols {missing} not in the job dataset; available: "
            f"{sorted(universe)}"
        )
    elif definition.family == "relative_value_pair" and len(universe) != 2:
        reason = (
            "is a pair strategy that needs exactly two symbols; the job trades "
            f"{len(universe)}"
        )
    return {
        "required_symbols": required,
        "compatible": reason is None,
        "missing_symbols": missing,
        **({"incompatibility_reason": reason} if reason else {}),
    }


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


def _freeze_research_ideation(store: JobStore, job_id: str) -> dict[str, Any] | None:
    """The researcher's ranked hypotheses, frozen for the designer.

    The wake writes research/ideation/latest.json; until now nothing in the
    campaign read it, so a validated artifact changed nothing downstream.
    Only a contract-valid artifact offers hypotheses; an invalid one is
    surfaced as such so the designer does not build on it.
    """
    from wayfinder_paths.jobs.worker import _IDEATION_PATH, validate_ideation_artifact

    doc = store.read_json(job_id, _IDEATION_PATH)
    if not isinstance(doc, dict):
        return None
    report = validate_ideation_artifact(doc)
    if not report["valid"]:
        return {
            "valid": False,
            "generated_at": report["generated_at"],
            "problems": report["problems"],
        }
    order = {"testable": 0, "starved": 1, "refuted": 2}
    hypotheses = sorted(
        (row for row in doc.get("hypotheses") or [] if isinstance(row, dict)),
        key=lambda row: order.get(str(row.get("bucket")), 3),
    )
    return {
        "valid": True,
        "generated_at": report["generated_at"],
        "buckets": report["buckets"],
        "sources": [
            {
                "tool": str(row.get("tool"))[:120],
                "takeaway": str(row.get("takeaway"))[:160],
            }
            for row in (doc.get("sources_consulted") or [])
            if isinstance(row, dict) and row.get("tool")
        ][:5],
        "hypotheses": [
            {
                "title": str(row.get("title"))[:120],
                "thesis": str(row.get("thesis"))[:240],
                "bucket": str(row.get("bucket")),
                "next_step": str(row.get("next_step"))[:200],
            }
            for row in hypotheses
        ][:5],
    }


def _freeze_research_context(store: JobStore, job_id: str) -> dict[str, Any]:
    """Two load-bearing lists, distilled from existing mechanical records."""
    archive = load_archive(store, job_id).get("candidates") or []
    refuted_by_family: dict[str, dict[str, Any]] = {}
    risk_positives: list[dict[str, Any]] = []
    for entry in archive:
        metadata = entry.get("metadata") or {}
        gate = metadata.get("gate") or {}
        if (
            entry.get("status") == "proposal_rejected"
            and gate.get("class") == "risk_ceiling"
        ):
            # Edge was proven at the gate; only the size failed. That is a
            # validated win with a sizing hint, never a refuted family.
            risk_positives.append(
                {
                    "source": "gate_edge_risk_ceiling",
                    "id": entry.get("candidate_id"),
                    "family": entry.get("family"),
                    "evidence": (
                        f"edge {float(gate.get('oos_net_log_growth') or 0.0):+.3f} "
                        f"OOS growth, paired {float(gate.get('paired_estimate') or 0.0):+.3f}; "
                        f"failed drawdown {float(gate.get('observed_max_drawdown_pct') or 0.0):.3f} "
                        f"vs ceiling {float(gate.get('ceiling_max_drawdown_pct') or 0.0):.2f}; "
                        f"size <= {gate.get('implied_scale')}x"
                    )[:240],
                    "implied_scale": gate.get("implied_scale"),
                    "recorded_at": entry.get("updated_at") or entry.get("created_at"),
                }
            )
            continue
        postmortem = metadata.get("latest_postmortem") or {}
        failure_codes = list(
            dict.fromkeys(
                [
                    *(postmortem.get("failure_codes") or []),
                    *(metadata.get("full_dev_failure_codes") or []),
                ]
            )
        )
        status = entry.get("status")
        rejected = status in {"audit_rejected", "proposal_rejected"} or (
            status == "low_fidelity_rejected"
            and bool(_REFUTING_FAILURE_CODES.intersection(failure_codes))
        )
        if status != "refuted" and not rejected:
            continue
        family = str(entry.get("family") or "").strip()
        if family and family not in refuted_by_family:
            refutation = {
                "family": family,
                "candidate_id": entry.get("candidate_id"),
                "evidence": str(entry.get("evidence") or "")[:240],
            }
            forward = metadata.get("forward")
            if isinstance(forward, dict):
                refutation["forward"] = {
                    key: forward.get(key)
                    for key in (
                        "verdict",
                        "paired_days",
                        "estimate",
                        "overall_estimate",
                        "lcb",
                        "ucb",
                        "candidate_trade_count",
                        "candidate_max_drawdown_pct",
                    )
                    if forward.get(key) is not None
                }
            if entry.get("status") != "refuted":
                refutation.update(
                    {
                        "failure_codes": failure_codes[:8],
                        "strength": (
                            "screen_inversion"
                            if "screen_inversion" in failure_codes
                            else "out_of_regime_loss"
                            if "out_of_regime_loss_budget" in failure_codes
                            else "rejected"
                        ),
                    }
                )
            refuted_by_family[family] = refutation

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
    positives.extend(risk_positives)
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
    requested_policy: Mapping[str, Any] | None = None,
    slot: int,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve a requested source to material that actually exists.

    Cold-start QD/crossover slots become distinct audited starter seeds; they
    are never incumbent copies carrying a misleading lineage label.
    """
    pool = (manifest.get("parent_pool") or {}).get("candidates") or []
    if requested_source == "policy_kernel":
        if requested_policy:
            return {
                "source": "policy_kernel",
                "parents": [],
                "policy": dict(requested_policy),
            }
        return {"source": "de_novo", "parents": [], "fallback_from": "policy_kernel"}
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
        starters = [item for item in starters if item.get("compatible", True)]
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
        starter = dict(plan.get("starter") or {})
        if starter.get("compatible") is False:
            raise ValueError(
                f"starter {starter.get('starter_id')} "
                f"{starter.get('incompatibility_reason')}"
            )
        target_symbols = _target_execution_params(frozen_source).get("symbols")
        if isinstance(target_symbols, list):
            missing = sorted(
                set(starter.get("required_symbols") or [])
                - {str(symbol) for symbol in target_symbols}
            )
            if missing:
                raise ValueError(
                    f"starter {starter.get('starter_id')} requires symbols "
                    f"{missing} not in the job dataset; available: "
                    f"{sorted(str(symbol) for symbol in target_symbols)}"
                )
        _copy_clean_scaffold(store, job_id, frozen_source, candidate_root)
        _install_starter_seed(
            store,
            job_id,
            campaign_id=campaign_id,
            candidate_root=candidate_root,
            starter=starter,
        )
    elif source == "policy_kernel":
        _copy_clean_scaffold(store, job_id, frozen_source, candidate_root)
        _install_policy_kernel(
            store,
            job_id,
            candidate_root=candidate_root,
            policy=dict(plan.get("policy") or {}),
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
    # A starter's bar-denominated lookbacks are calibrated for its own
    # timeframe; on a job with a different bar interval they are rescaled so a
    # 3-day momentum stays three days (the 5m rotation ran with 9-day windows
    # on 15m bars and never traded enough to clear cost).
    ratio = _starter_bar_ratio(starter, candidate_root)
    params = _rescale_bar_params(dict(starter.get("params") or {}), ratio)
    params.update(_target_execution_params(candidate_root))
    params.pop("full_history", None)
    # Scale the indicator parameters, then ask the resulting strategy for its
    # real warmup. Scaling the old total also scales away fixed buffers (for
    # example max(lookbacks) + 4), leaving a view permanently shorter than
    # the strategy's own warmup guard after a timeframe adaptation.
    strategy = _load_strategy(script, dict(params))
    warmup = int(getattr(strategy, "warmup_bars", 0) or 0)
    if warmup <= 0:
        raise ValueError("adapted starter strategy declares no warmup_bars")
    params["warmup_bars"] = warmup
    params["lookback_bars"] = warmup + STARTER_LOOKBACK_MARGIN_BARS
    job_data["execution_params"] = params
    atomic_write_text(
        candidate_root / "job.yaml", yaml.safe_dump(job_data, sort_keys=False)
    )


def _starter_bar_ratio(starter: Mapping[str, Any], candidate_root: Path) -> float:
    """Starter bars per job bar: 1.0 on the starter's own interval."""
    starter_seconds = int(
        bar_interval_seconds(str(starter.get("timeframe") or "")) or 0
    )
    job_data = _load_job_yaml(candidate_root)
    spec_data, _ = resolve_execution_spec(candidate_root, job_data)
    job_interval = str(
        ((spec_data or {}).get("data_contract") or {}).get("bar_interval") or ""
    )
    job_seconds = int(bar_interval_seconds(job_interval) or 0)
    if starter_seconds <= 0 or job_seconds <= 0:
        return 1.0
    return starter_seconds / job_seconds


def _rescale_bar_params(params: dict[str, Any], ratio: float) -> dict[str, Any]:
    if ratio == 1.0:
        return params
    scaled = dict(params)
    for key, value in params.items():
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if key.endswith(("_bars", "_period")) or key == "rebalance_offset":
            scaled[key] = max(1, round(value * ratio)) if value > 0 else value
    return scaled


def _policy_survivor_from_pack(
    pack: Mapping[str, Any], ref: str
) -> dict[str, Any] | None:
    """The policy-scan survivor a pointer names, when it carries a kernel."""
    match = re.match(r"^/policy_scan/survivors/(\d+)$", str(ref or ""))
    if not match:
        return None
    survivors = list((pack.get("policy_scan") or {}).get("survivors") or [])
    index = int(match.group(1))
    if index >= len(survivors):
        return None
    row = survivors[index]
    if not isinstance(row, dict) or not (row.get("recipe") or {}).get("module"):
        return None
    return dict(row)


def _policy_survivor(
    store: JobStore, job_id: str, manifest: Mapping[str, Any], ref: str
) -> dict[str, Any] | None:
    if not ref:
        return None
    pack_path = str((manifest.get("diagnostic_pack") or {}).get("path") or "")
    pack = store.read_json(job_id, pack_path, default={}) if pack_path else {}
    return _policy_survivor_from_pack(pack or {}, ref)


def _install_policy_kernel(
    store: JobStore,
    job_id: str,
    *,
    candidate_root: Path,
    policy: Mapping[str, Any],
) -> None:
    """A policy-scan survivor as a running bundle: the kernel re-exported in
    one line and its recipe params beside the job's own execution params;
    warmup and lookback come from the instantiated strategy."""
    recipe = dict(policy.get("recipe") or {})
    module = str(recipe.get("module") or "")
    if not module.startswith("wayfinder_paths.jobs.strategies."):
        raise ValueError("policy kernel recipe must name a jobs strategy module")
    job_data = _load_job_yaml(candidate_root)
    script = store.resolve_script_entrypoint(
        job_id, job_data, candidate_dir=candidate_root
    )
    if script is None:
        raise ValueError("policy kernel candidate has no execution entrypoint")
    script.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(script, f"from {module} import build_strategy\n")
    params = dict(recipe.get("params") or {})
    params.update(_target_execution_params(candidate_root))
    strategy = importlib.import_module(module).build_strategy(dict(params))
    warmup = int(getattr(strategy, "warmup_bars", 0) or 0)
    if warmup <= 0:
        raise ValueError("policy kernel strategy declares no warmup_bars")
    params["warmup_bars"] = warmup
    params["lookback_bars"] = warmup + STARTER_LOOKBACK_MARGIN_BARS
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
    protected_data_root: Path | None = None,
    audit_days: int = 7,
) -> dict[str, Any]:
    """Freeze every input known when candidate generation begins."""
    campaign_root.mkdir(parents=True, exist_ok=False)
    source_bundle = campaign_root / "source"
    copy_job_bundle(active_root, source_bundle)
    data_root = campaign_root / CAMPAIGN_DATA_ROOT
    bars_path = data_root / "results" / "backtest" / "input_bars.json"
    bars_path.parent.mkdir(parents=True, exist_ok=True)
    cutoff, dataset_window = _write_development_prefix(
        dataset_path,
        bars_path,
        fraction=development_fraction,
    )
    dataset_timestamps = list(dataset_window.pop("_timestamps", []))
    protected_bars_path: Path | None = None
    if protected_data_root is not None:
        protected_data_root.mkdir(parents=True, exist_ok=False)
        protected_bars_path = (
            protected_data_root / "results" / "backtest" / "input_bars.json"
        )
        _atomic_copy(dataset_path, protected_bars_path)

    features: list[dict[str, Any]] = []
    job_data = _load_job_yaml(active_root)
    spec_data, _ = resolve_execution_spec(active_root, job_data)
    copied: set[Path] = set()
    if spec_data:
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
            if protected_data_root is not None:
                _atomic_copy(source, protected_data_root / relative)
            copied.add(source)
            features.append(
                {
                    "path": f"{CAMPAIGN_DATA_ROOT}/{relative.as_posix()}",
                    "sha256": _file_hash(destination),
                    "bytes": destination.stat().st_size,
                }
            )
    # The store itself rides along even when the incumbent declares
    # nothing: a candidate may declare a derived column (the macro
    # regime) the incumbent never used, and its screen must find it.
    store_source = (active_root / DEFAULT_FEATURES_PATH).resolve()
    if store_source.exists() and store_source not in copied:
        destination = (data_root / DEFAULT_FEATURES_PATH).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not _write_timeseries_prefix(store_source, destination, cutoff=cutoff):
            shutil.copy2(store_source, destination)
        if protected_data_root is not None:
            _atomic_copy(store_source, protected_data_root / DEFAULT_FEATURES_PATH)
        copied.add(store_source)
        features.append(
            {
                "path": f"{CAMPAIGN_DATA_ROOT}/{DEFAULT_FEATURES_PATH}",
                "sha256": _file_hash(destination),
                "bytes": destination.stat().st_size,
                "declared": False,
            }
        )

    forward_path = campaign_root / FORWARD_SNAPSHOT
    atomic_write_json(forward_path, experience)
    evaluation_plan = _snapshot_evaluation_plan(
        dataset_timestamps,
        development_fraction=development_fraction,
        cutoff=cutoff,
        audit_days=audit_days,
        execution_spec=ExecutionSpec.from_dict(spec_data) if spec_data else None,
        protected_data_root=protected_data_root,
    )
    return {
        "dataset": {
            "path": f"{CAMPAIGN_DATA_ROOT}/results/backtest/input_bars.json",
            "sha256": _file_hash(bars_path),
            "bytes": bars_path.stat().st_size,
            "development_cutoff": cutoff.isoformat() if cutoff is not None else None,
            **dataset_window,
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
        "evaluation_plan": evaluation_plan,
    }


def _atomic_copy(source: Path, destination: Path) -> None:
    """Copy a potentially large frozen input without exposing a partial file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=str(destination.parent), prefix=destination.name, suffix=".tmp"
    )
    try:
        with source.open("rb") as source_handle, os.fdopen(fd, "wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _snapshot_evaluation_plan(
    timestamps: Sequence[pd.Timestamp],
    *,
    development_fraction: float,
    cutoff: pd.Timestamp | None,
    audit_days: int,
    execution_spec: ExecutionSpec | None,
    protected_data_root: Path | None,
) -> dict[str, Any]:
    if protected_data_root is None:
        return {"mode": "single_validation_window_v1", "protected": False}
    if not timestamps or cutoff is None or execution_spec is None:
        raise ValueError("protected fold certification requires timestamped bars")
    discovery_bars = sum(stamp <= cutoff for stamp in timestamps)
    seconds = bar_interval_seconds(execution_spec.data_contract.get("bar_interval"))
    if not seconds:
        raise ValueError("protected fold certification requires a bar interval")
    audit_bars = max(1, int(audit_days * 86_400 // int(seconds)))
    audit_start = max(discovery_bars, len(timestamps) - audit_bars)
    certification_bars = max(0, audit_start - discovery_bars)
    return {
        "mode": "protected_chronological_folds_v1",
        "protected": True,
        "discovery": {
            "fraction": development_fraction,
            "start": timestamps[0].isoformat(),
            "end": cutoff.isoformat(),
            "bars": discovery_bars,
        },
        "certification": {
            "start": (
                timestamps[discovery_bars].isoformat()
                if discovery_bars < audit_start
                else None
            ),
            "end": (
                timestamps[audit_start - 1].isoformat()
                if audit_start > discovery_bars
                else None
            ),
            "bars": certification_bars,
        },
        "audit": {
            "days": audit_days,
            "start": timestamps[audit_start].isoformat(),
            "end": timestamps[-1].isoformat(),
            "bars": len(timestamps) - audit_start,
        },
        "protected_snapshot": _directory_fingerprint(protected_data_root),
    }


def _write_development_prefix(
    source: Path, destination: Path, *, fraction: float
) -> tuple[pd.Timestamp | None, dict[str, Any]]:
    """Write the development prefix; also report the dataset's span and universe."""
    payload = json.loads(source.read_text(encoding="utf-8"))
    bars = payload.get("bars") if isinstance(payload, dict) else None
    if not isinstance(bars, list) or not bars:
        atomic_write_json(destination, payload)
        return None, {}
    timestamp_values = {_row_timestamp(row) for row in bars if isinstance(row, dict)}
    timestamps = sorted(stamp for stamp in timestamp_values if stamp is not None)
    if not timestamps:
        atomic_write_json(destination, payload)
        return None, {}
    count = max(1, min(len(timestamps), int(len(timestamps) * fraction)))
    cutoff = timestamps[count - 1]
    window = {
        "symbols": sorted(
            {
                str(row["symbol"])
                for row in bars
                if isinstance(row, dict) and row.get("symbol") is not None
            }
        ),
        "full_bars": len(timestamps),
        "full_days": round(
            (timestamps[-1] - timestamps[0]).total_seconds() / 86_400.0, 4
        ),
        "bars": count,
        "days": round((cutoff - timestamps[0]).total_seconds() / 86_400.0, 4),
        "start": timestamps[0].isoformat(),
        "end": timestamps[-1].isoformat(),
        # Internal handoff to the evaluation-plan builder. The caller removes
        # it before the window is persisted in the manifest, avoiding a
        # second parse of a potentially large canonical dataset.
        "_timestamps": timestamps,
    }
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
    return cutoff, window


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


def _protected_fold_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    raw = policy.get("protected_fold_certification") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("protected_fold_certification must be a mapping")
    enabled = bool(raw.get("enabled", False))
    discovery_fraction = float(raw.get("discovery_fraction") or 0.60)
    folds = int(raw.get("folds") or 4)
    required = int(raw.get("required_positive_folds") or 3)
    max_loss = float(raw.get("max_fold_loss_pct") or 0.05)
    minimum_bars = int(raw.get("minimum_fold_bars") or 8)
    if not 0.0 < discovery_fraction < 1.0:
        raise ValueError("protected discovery_fraction must be between 0 and 1")
    if folds < 2 or not 1 <= required <= folds:
        raise ValueError("protected fold counts are inconsistent")
    if max_loss <= 0.0 or minimum_bars < 2:
        raise ValueError("protected fold loss and bar floors must be positive")
    return {
        "enabled": enabled,
        # A disabled campaign snapshots every bar and follows the historical
        # train/validation split.  The configured 60% becomes active only
        # when the opt-in itself is active.
        "discovery_fraction": discovery_fraction if enabled else 1.0,
        "folds": folds,
        "required_positive_folds": required,
        "max_fold_loss_pct": max_loss,
        "minimum_fold_bars": minimum_bars,
    }


def _discovery_dataset(
    dataset: PreparedExecutionDataset, policy: Mapping[str, Any]
) -> PreparedExecutionDataset:
    """The model-visible snapshot is already the whole discovery region in
    protected mode; legacy campaigns retain their inner train split."""
    if _protected_fold_policy(policy)["enabled"]:
        return dataset
    split = policy.get("split") or {}
    train, _, _ = _split_dataset(
        dataset,
        train_end=float(split.get("train") or 0.8),
        validation_end=1.0,
    )
    return train


def _protected_campaign_dataset_root(
    store: JobStore, job_id: str, campaign_id: str
) -> Path:
    allowed = (store.repo_root / "audit" / job_id / PROTECTED_CAMPAIGN_ROOT).resolve()
    resolved = (allowed / campaign_id / CAMPAIGN_DATA_ROOT).resolve()
    if not resolved.is_relative_to(allowed) or resolved.parent.parent != allowed:
        raise ValueError("protected campaign dataset escapes the audit plane")
    return resolved


def _verified_protected_dataset_root(
    store: JobStore,
    job_id: str,
    campaign_id: str,
    *,
    manifest: Mapping[str, Any] | None = None,
) -> Path:
    resolved = _protected_campaign_dataset_root(store, job_id, campaign_id)
    bars = resolved / "results" / "backtest" / "input_bars.json"
    plan = dict(
        (manifest or _campaign_manifest(store, job_id, campaign_id)).get(
            "evaluation_plan"
        )
        or {}
    )
    expected = str((plan.get("protected_snapshot") or {}).get("sha256") or "")
    if plan.get("mode") != "protected_chronological_folds_v1" or not expected:
        raise ValueError("campaign has no protected certification snapshot")
    if not bars.is_file() or _directory_fingerprint(resolved)["sha256"] != expected:
        raise ValueError("protected certification snapshot revision mismatch")
    record_evidence_access(
        store.repo_root,
        job_id,
        "evolution_protected_certification",
        {"campaign_id": campaign_id, "sha256": expected},
    )
    return resolved


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
        and (item.get("gate") or {}).get("class") != "risk_ceiling"
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


def _directory_fingerprint(root: Path) -> dict[str, Any]:
    """Hash relative paths and bytes for the complete protected input tree."""
    digest = hashlib.sha256()
    files = [path for path in sorted(root.rglob("*")) if path.is_file()]
    total_bytes = 0
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                total_bytes += len(chunk)
        digest.update(b"\0")
    return {"sha256": digest.hexdigest(), "files": len(files), "bytes": total_bytes}


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
