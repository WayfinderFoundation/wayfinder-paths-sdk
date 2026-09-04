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
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
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
from wayfinder_paths.jobs.constitution import load_constitution
from wayfinder_paths.jobs.economics import (
    block_bootstrap_lcb,
    daily_log_returns,
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
    leader_attribution_sentence,
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
from wayfinder_paths.jobs.improver.spec import ImproverSpec, revision_stamp
from wayfinder_paths.jobs.indicators import REGIME_LABELS
from wayfinder_paths.jobs.isolated_phase import run_isolated_phase
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.multiple_testing import haircut
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
    scan_signals,
)
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
from wayfinder_paths.jobs.trade_forensics import (
    aggregate_trade_forensics,
    forensics_for_closed_trades,
)
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
    # Two campaigns' worth: a weekly loop must still see the week before last.
    historical_lessons = evolution_lessons_block(store, job_id, limit=16)
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
    starter_seeds = _snapshot_starter_seeds(
        store,
        job_id,
        campaign_root,
        dataset_symbols=tuple(snapshots["dataset"].get("symbols") or ()),
    )
    research_context = _freeze_research_context(store, job_id)
    research_ideation = _freeze_research_ideation(store, job_id)
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
    validated_signals = _validated_signals(
        store, job_id, campaign_root, policy=campaign_policy
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


def _validated_signals(
    store: JobStore, job_id: str, campaign_root: Path, *, policy: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Library signals with fold-stable, cost-net, family-corrected edge on
    this dataset, selected by statistical power rather than cadence.

    Discipline, in order: the scan's own on the FULL train split (events
    decimated to horizon spacing, |t|>=2 against drift, sign agreement in 3
    of 4 chronological folds, edge positive net of the round trip); the
    Benjamini-Hochberg q over the whole family at or under the threshold (the
    trial-count haircut at signal level); at least the minimum event count;
    then non-inferiority on both disjoint screen slices. The designer must
    build grounded de-novo slots on what survives.
    """
    if not bool(policy.get("signal_first_seeding", False)):
        return None
    try:
        subject = _load_subject(
            store,
            job_id,
            campaign_root / "source",
            dataset_root=campaign_root / CAMPAIGN_DATA_ROOT,
        )
        split = policy.get("split") or {}
        train, _, _ = _split_dataset(
            subject["dataset"],
            train_end=float(split.get("train") or 0.8),
            validation_end=1.0,
        )
        params, _, _ = _calibrated_params(store, job_id, subject)
        bar_seconds = bar_interval_seconds(
            subject["spec"].data_contract.get("bar_interval")
        )
        if not bar_seconds:
            raise ValueError("execution spec requires a positive bar_interval")
        bar_seconds = int(bar_seconds)
        timeframes = _signal_timeframes(bar_seconds)
        slices = _screen_slices(train, slices=int(policy.get("screen_slices") or 2))
        universe = regime_universe(params, subject["dataset"].bars.symbols)
        # resample_ohlcv labels the coarser bars by symbol, so keep the column.
        columns = ["timestamp", "symbol", "open", "high", "low", "close", "volume"]
        scan_kwargs: dict[str, Any] = {
            "bar_seconds": bar_seconds,
            "timeframes": timeframes,
            "holdout_fraction": 0.0,
            "min_events": int(policy.get("signal_scan_min_events") or 30),
            "fee_bps": float(params.get("fee_bps") or 5.0),
            "slippage_bps": float(params.get("slippage_bps") or 3.5),
        }
        train_frame = train.bars.to_frame()
        stamps = train.bars.timestamps
        train_days = max((stamps[-1] - stamps[0]).total_seconds() / 86_400.0, 1e-9)
        full_rows: list[dict[str, Any]] = []
        for symbol in universe:
            rows = train_frame[train_frame["symbol"] == symbol][columns].reset_index(
                drop=True
            )
            if len(rows) < 200:
                continue
            result = scan_signals(rows, **scan_kwargs)
            for row in result.get("_all_rows") or []:
                full_rows.append({**row, "symbol": symbol})
        max_q = float(policy.get("signal_first_max_q") or 0.20)
        apply_bh_verdicts(full_rows, q_threshold=max_q, min_folds_agree=3)
        per_slice: dict[str, dict[tuple[str, str, str, int], dict[str, Any]]] = {}
        for label, dataset in slices:
            frame = dataset.bars.to_frame()
            table: dict[tuple[str, str, str, int], dict[str, Any]] = {}
            for symbol in universe:
                rows = frame[frame["symbol"] == symbol][columns].reset_index(drop=True)
                if len(rows) < 200:
                    continue
                result = scan_signals(rows, **{**scan_kwargs, "min_events": 10})
                for row in result.get("_all_rows") or []:
                    key = (
                        symbol,
                        str(row["signal"]),
                        str(row["timeframe"]),
                        int(row["horizon"]),
                    )
                    table[key] = row
            per_slice[label] = table
        selected, near, funnel = _select_validated_rows(
            full_rows,
            per_slice,
            days=train_days,
            min_events=int(policy.get("signal_first_min_events") or 40),
            max_q=max_q,
            slice_min_t=float(policy.get("signal_first_slice_min_t") or 1.0),
            min_t_net=float(policy.get("signal_first_min_t_net") or 2.0),
        )
        for row in [*selected, *near]:
            row["warmup_bars_required"] = library_signal_warmup_bars(
                row["signal"], row["timeframe"], bar_seconds=bar_seconds
            )
            row["how_to_use"] = (
                "in precompute(): from wayfinder_paths.jobs.research import "
                "library_signal_on_bars; column = library_signal_on_bars(frame, "
                f"{row['signal']!r}, {row['timeframe']!r}, bar_seconds={bar_seconds}); "
                f"enter {row['direction']} on True, exit after "
                f"{row['horizon']} {row['timeframe']} bars; declare warmup_bars >= "
                f"{row['warmup_bars_required']}; inside the risk envelope: one "
                "position per symbol, a stop via add_stop_atr or an explicit "
                "invalidation level, size so a 3x typical adverse move costs < 5% "
                "of equity (the gate sizes to the ceiling; needing < 0.5x is a "
                "weak design)"
            )
    except Exception as exc:  # noqa: BLE001 - seeding never blocks a start
        return {"available": False, "reason": str(exc)[:240]}
    limit = int(policy.get("signal_first_limit") or 10)
    return {
        "available": True,
        "method": (
            "library event study on the full train split (decimated events, "
            "|t|>=2 vs drift, 3/4 fold sign agreement, positive edge net of the "
            "round trip with t_net>=floor, Benjamini-Hochberg q over the whole "
            "family at or under the threshold, at least the minimum event "
            "count), then non-inferiority on both screen slices"
        ),
        "timeframes": timeframes,
        "tests": len(full_rows),
        # The haircut the designer should see: this many tests would clear a
        # 5% bar by luck alone, which is why q gates and t alone does not.
        "expected_lucky_passes": round(0.05 * len(full_rows), 1),
        "q_threshold": max_q,
        "funnel": funnel,
        "train_days": round(train_days, 2),
        "signals": selected[:limit],
        # Passed the scan's own bar but not q, event count or slice
        # non-inferiority: direction for the designer, not evidence.
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Replicated, cost-surviving, dense signals from a full-train scan.

    Direction and strength come from the scan's gross t-stat (its
    ``direction`` field; ``t_net`` is the directional cost-adjusted t and its
    sign is NOT the trade side).  Benjamini-Hochberg q-values ride along as
    evidence but do not gate: a family of ~2,000 tests on 80 days of bars
    never promotes at q<=0.10, and the screen, full-dev and holdout gate the
    strategy built on the signal anyway.  What replicates is what seeds.
    """
    selected: list[dict[str, Any]] = []
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
    }
    for row in full_rows:
        direction = row.get("direction")
        t_gross = float(row.get("t_stat_vs_drift") or 0.0)
        t_net = float(row.get("t_net") or 0.0)
        if direction not in ("long", "short") or not bool(row.get("fold_stable")):
            continue
        funnel["directional_fold_stable"] += 1
        if float(row.get("edge_net_bps") or 0.0) <= 0:
            continue
        funnel["cost_positive"] += 1
        t_ok = t_net >= min_t_net
        funnel["t_net_at_floor"] += int(t_ok)
        key = (
            str(row["symbol"]),
            str(row["signal"]),
            str(row["timeframe"]),
            int(row["horizon"]),
        )
        events = int(row.get("n") or 0)
        density = events / days
        slice_t = {
            label: float((table.get(key) or {}).get("t_stat_vs_drift") or 0.0)
            for label, table in per_slice.items()
        }
        # Non-inferiority, not confirmation: a 35-day slice may be flat for
        # the signal, it may not be significantly against its side.
        side = 1.0 if t_gross > 0 else -1.0
        non_inferior = all(value * side > -slice_min_t for value in slice_t.values())
        q_value = row.get("q_value")
        q_ok = q_value is not None and float(q_value) <= max_q
        powered = events >= min_events
        funnel["q_at_threshold"] += int(t_ok and q_ok)
        funnel["powered"] += int(t_ok and q_ok and powered)
        funnel["non_inferior"] += int(t_ok and q_ok and powered and non_inferior)
        entry = {
            "symbol": key[0],
            "signal": key[1],
            "family": row.get("family"),
            "description": str(row.get("description") or "")[:160],
            "timeframe": key[2],
            "horizon": key[3],
            "direction": str(direction),
            "t_stat": round(t_gross, 3),
            "t_net": round(t_net, 3),
            "q_value": row.get("q_value"),
            "bh_verdict": row.get("verdict"),
            "edge_net_bps": round(float(row.get("edge_net_bps") or 0.0), 2),
            "events": events,
            "events_per_day": round(density, 3),
            "folds_agreeing": row.get("folds_agreeing"),
            "t_stat_by_slice": {
                label: round(value, 3) for label, value in slice_t.items()
            },
            "score": round(t_net, 3),
        }
        if t_ok and powered and q_ok and non_inferior:
            selected.append(entry)
        else:
            entry["shortfall"] = (
                "t_net_below_floor"
                if not t_ok
                else (
                    "underpowered"
                    if not powered
                    else ("q_above_threshold" if not q_ok else "slice_against")
                )
            )
            near.append(entry)
    selected.sort(key=lambda item: (-item["score"], item["symbol"], item["signal"]))
    near.sort(key=lambda item: (-item["score"], item["symbol"], item["signal"]))
    return selected, near, funnel


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
        train_end, validation_end = _split_bounds(
            store, job_id, campaign_id=campaign_id
        )
        train, _, _ = _split_dataset(
            subject["dataset"], train_end=train_end, validation_end=validation_end
        )
        params, _, _ = _calibrated_params(store, job_id, subject)
        strategy = _load_strategy(subject["script"], dict(params))
        tunables = _numeric_tunables(getattr(strategy, "params", {}) or {}, params)
        if not tunables:
            return {"available": False, "reason": "no numeric tunables"}
        slices = _screen_slices(train, slices=int(policy.get("screen_slices") or 2))
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
        split = policy.get("split") or {}
        train, _, _ = _split_dataset(
            subject["dataset"],
            train_end=float(split.get("train") or 0.8),
            validation_end=1.0,
        )
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
        for label, dataset in _screen_slices(
            train, slices=int(policy.get("screen_slices") or 2)
        ):
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
    }
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
    if wildcard_count != int(manifest["policy"].get("wildcard_slots") or 0):
        raise ValueError("campaign design has the wrong number of wildcard slots")
    grounded = [slot for slot in normalized_slots if not slot["wildcard"]]
    offered = list(
        ((diagnostic_pack.get("validated_signals") or {}).get("signals")) or []
    )
    if offered:
        # Signals that survived the family-corrected scan are the evidence a
        # grounded free-form slot must build on; narratives are not.
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
                str(ref).startswith("/validated_signals/signals/") for ref in refs
            ):
                names = ", ".join(
                    f"{row.get('signal')} {row.get('symbol')} {row.get('timeframe')}"
                    for row in offered[:6]
                )
                raise ValueError(
                    f"slot {slot['slot_id']} must build on a validated signal: its "
                    "hypothesis cites none of /validated_signals/signals/<i> while "
                    f"the pack offers {len(offered)} ({names})"
                )
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
        "secondary_parent_bundle": (parent_plan.get("secondary") or {}).get("bundle"),
        "mutation_kind": chosen_mutation,
        "neighborhood": neighborhood,
        "forced_jump": forced_jump,
        "bundle": relative,
        "warmup_bars": seeded_window,
        "seed_revision": seed_revision,
        "reference_bundle": reference_relative,
        "reference_revision": reference_revision,
        "evidence_reset": source in {"starter_seed", "research_seed"},
        "design_slot_id": design_slot.get("slot_id"),
        "hypothesis_id": design_slot.get("hypothesis_id"),
        "wildcard": bool(design_slot.get("wildcard")),
        "target_regimes": target_regimes,
        "evidence_refs": list(hypothesis.get("evidence_refs") or []),
        "signal_refs": _cited_signals(
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
        screen_slices = _screen_slices(
            train, slices=int(policy.get("screen_slices") or 2)
        )
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
    train_end, validation_end = _split_bounds(store, job_id, campaign_id=campaign_id)
    train, _, _ = _split_dataset(
        subject["dataset"], train_end=train_end, validation_end=validation_end
    )
    params, _, _ = _calibrated_params(store, job_id, subject)
    policy = _campaign_policy(store, job_id, campaign_id)
    slices = _screen_slices(train, slices=int(policy.get("screen_slices") or 2))
    _, quick = slices[0]
    result = simulate_execution(subject["script"], quick, subject["spec"], params)
    receipt = result_receipt(
        result,
        revision=compute_workspace_revision(reference),
        objective=_objective(result.stats, params),
        behavior=_behavior(result, quick, subject["spec"]),
    )
    receipt["round_trip_cost_bps"] = _round_trip_cost_bps(params)
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


def _screen_complete(state: Mapping[str, Any], policy: Mapping[str, Any]) -> bool:
    budget = int(policy.get("generated_programs") or 0)
    return len(state.get("candidates") or []) >= budget


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
            if item.get("starter_id") and item.get("compatible", True)
        ]
        research_seed_ids = [
            str(item.get("seed_id"))
            for item in manifest.get("research_seeds") or []
            if item.get("seed_id")
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
        cost_instruction = (
            "Cost budget: "
            + (
                f"at ~{cost_budget['round_trip_cost_bps']:.0f} bps round trip a "
                f"trade must capture at least {cost_budget['cost_hurdle_multiple']:.1f}x "
                f"that gross ({cost_budget['cost_hurdle_multiple'] * cost_budget['round_trip_cost_bps']:.0f} bps) "
                "or the screen rejects it before anything else; "
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
            "non-inferior on both slices)"
            if funnel.get("tests")
            else ""
        )
        signal_instruction = (
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
            "the fixed-horizon exit, and the warmup to declare. "
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
                f"{cost_instruction}{risk_instruction}{ideation_instruction}"
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
                "starter_seed, research_seed, research_context; it is an enum, "
                "so do not append a starter id or other qualifier. mutation_kind "
                "must be exactly structural or parameter. For a starter_seed "
                "slot, set "
                "optional starter_seed_id to one of "
                f"{starter_ids}; this structured id, not summary prose, selects "
                "the executable seed. For a research_seed slot, set optional "
                f"research_seed_id to one of {research_seed_ids}. "
                "If a hypothesis uses an exact family listed under "
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
            },
            "deadline_elapsed": current >= deadline,
        }
    policy = manifest.get("policy") or {}
    budget = int(policy.get("generated_programs") or 0)
    awaiting_evaluation = _awaiting_evaluation(state, policy)
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
        text += (
            f"; each trade captured {float(budget.get('gross_bps_per_trade') or 0.0):+.1f} "
            f"bps gross vs {float(budget.get('round_trip_cost_bps') or 0.0):.0f} bps "
            f"round trip (coverage {float(budget['cost_coverage']):.2f}x, hurdle "
            f"{float(budget.get('cost_hurdle_multiple') or 0.0):.1f}x)"
        )
    return text + ". "


_SIGNAL_REF_KEYS = (
    "symbol",
    "signal",
    "timeframe",
    "horizon",
    "direction",
    "t_net",
    "q_value",
    "events",
    "warmup_bars_required",
    "how_to_use",
)


def _cited_signals(
    store: JobStore, job_id: str, manifest: Mapping[str, Any], refs: Sequence[str]
) -> list[dict[str, Any]]:
    """The validated-signal entries a hypothesis cites, resolved from the
    frozen pack so the worker's candidate.json carries the recipe."""
    indices: list[int] = []
    for ref in refs:
        match = re.match(r"^/validated_signals/signals/(\d+)(?:/|$)", str(ref))
        if match and int(match.group(1)) not in indices:
            indices.append(int(match.group(1)))
    if not indices:
        return []
    pack_path = str((manifest.get("diagnostic_pack") or {}).get("path") or "")
    pack = store.read_json(job_id, pack_path, default={}) if pack_path else {}
    rows = list(((pack or {}).get("validated_signals") or {}).get("signals") or [])
    cited = []
    for index in indices:
        if 0 <= index < len(rows):
            row = rows[index]
            cited.append(
                {"pointer": f"/validated_signals/signals/{index}"}
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


def _screen_slices(
    train: PreparedExecutionDataset,
    *,
    bars: int = _SCREEN_SLICE_BARS,
    slices: int = 2,
) -> list[tuple[str, PreparedExecutionDataset]]:
    """Disjoint screen windows: the recent tail plus the tail before it.

    A candidate repaired against one slice is selected for that slice; a
    second, disjoint slice is the cheapest test that it generalizes at all.
    Short datasets fall back to the single recent slice.
    """
    timestamps = train.bars.timestamps
    total = len(timestamps)
    recent = _tail(train, bars)
    out = [("recent", recent)]
    remaining = total - min(bars, total)
    if slices >= 2 and remaining >= _SCREEN_MIN_EARLIER_BARS:
        out.append(("earlier", _earlier_screen_slice(train, remaining, bars, recent)))
    return out


def _earlier_screen_slice(
    train: PreparedExecutionDataset,
    remaining: int,
    bars: int,
    recent: PreparedExecutionDataset,
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
        end -= _SCREEN_MACRO_STEP_BARS
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
    cutoff, dataset_window = _write_development_prefix(
        dataset_path,
        bars_path,
        fraction=development_fraction,
    )

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
