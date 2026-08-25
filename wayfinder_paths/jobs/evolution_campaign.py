"""Isolated, budgeted open-ended strategy evolution campaigns.

The model remains the code mutation operator; this module owns everything
that must not be left to model discretion: cadence, immutable context,
lineage, stage budgets, sealed-tail access, archive accounting, and the
paper-only terminal state.  Candidate bundles never replace the active
workspace and no function in this module can authorize live trading.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.archive import (
    load_archive,
    quality_diversity_snapshot,
    record_candidate,
    set_candidate_status,
)
from wayfinder_paths.jobs.execution.features import parse_feature_specs
from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
from wayfinder_paths.jobs.execution.optimize import is_search_space, run_optuna_search
from wayfinder_paths.jobs.execution.primitives import (
    ExecutionSpec,
    bar_interval_seconds,
)
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)
from wayfinder_paths.jobs.execution.validation import (
    resolve_execution_spec,
    validate_execution_job,
)
from wayfinder_paths.jobs.execution.walk_forward import _slice, _test_window_stats
from wayfinder_paths.jobs.forward_experience import (
    CALIBRATION_PATH,
    build_forward_experience,
    execution_cost_assumptions,
)
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.improver.spec import ImproverSpec, revision_stamp
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.probation import open_paper_probation_leg
from wayfinder_paths.jobs.robustness import _strategy_warmup_bars
from wayfinder_paths.jobs.starter_casebook import select_starter_cases
from wayfinder_paths.jobs.store import JobStore

CAMPAIGN_STATE_PATH = "state/evolution_campaign.json"
CAMPAIGN_ROOT = "research/evolution/campaigns"
CAMPAIGN_DATA_ROOT = "dataset"
FORWARD_SNAPSHOT = "forward_experience.json"
SCHEMA_VERSION = "1.0"
_PARENT_SOURCES = ("incumbent", "qd_elite", "crossover", "de_novo")


def campaign_status(store: JobStore, job_id: str) -> dict[str, Any]:
    return store.read_json(job_id, CAMPAIGN_STATE_PATH, default={}) or {}


def maybe_start_campaign(
    store: JobStore, job_id: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Start the due rollout campaign; return None outside its feature gate."""
    spec = ImproverSpec.load(store.job_dir(job_id))
    if not spec.evolution_enabled_for(job_id):
        return None
    existing = campaign_status(store, job_id)
    if existing.get("status") == "active":
        return existing
    cooldown_anchor = existing.get("completed_at") or existing.get("started_at")
    if cooldown_anchor:
        elapsed = _aware(now or datetime.now(UTC)) - _parse(cooldown_anchor)
        cooldown = timedelta(hours=float(spec.evolution["cooldown_hours"]))
        if elapsed < cooldown:
            return existing
    return start_campaign(store, job_id, now=now)


def start_campaign(
    store: JobStore,
    job_id: str,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    spec = ImproverSpec.load(store.job_dir(job_id))
    if not spec.evolution_enabled_for(job_id):
        raise ValueError(f"open evolution is not enabled for job {job_id!r}")
    existing = campaign_status(store, job_id)
    current = _aware(now or datetime.now(UTC))
    if existing.get("status") == "active" and not force:
        return existing
    cooldown_anchor = existing.get("completed_at") or existing.get("started_at")
    if cooldown_anchor and not force:
        elapsed = current - _parse(cooldown_anchor)
        cooldown = timedelta(hours=float(spec.evolution["cooldown_hours"]))
        if elapsed < cooldown:
            raise ValueError("evolution campaign cooldown has not elapsed")

    root = store.job_dir(job_id)
    dataset_path = root / "results" / "backtest" / "input_bars.json"
    if not dataset_path.exists():
        raise FileNotFoundError("evolution needs the job's canonical backtest dataset")
    source_revision = compute_workspace_revision(root)
    campaign_stem = f"{current.strftime('%Y%m%dT%H%M%SZ')}-{source_revision[:8]}"
    campaign_id = campaign_stem
    suffix = 2
    while (root / CAMPAIGN_ROOT / campaign_id).exists():
        campaign_id = f"{campaign_stem}-{suffix}"
        suffix += 1
    deadline = current + timedelta(hours=float(spec.evolution["campaign_hours"]))
    experience = build_forward_experience(store, job_id, now=current)
    cases = select_starter_cases(_job_tags(store, job_id))
    relative_root = f"{CAMPAIGN_ROOT}/{campaign_id}"
    campaign_root = root / relative_root
    snapshots = _snapshot_campaign_inputs(
        root, campaign_root, dataset_path=dataset_path, experience=experience
    )
    campaign_policy = {
        **spec.evolution,
        "same_family_non_wins": spec.stuck_same_family_non_wins,
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "job_id": job_id,
        "started_at": current.isoformat(),
        "deadline_at": deadline.isoformat(),
        "source_revision": source_revision,
        "dataset": snapshots["dataset"],
        "features": snapshots["features"],
        "forward_experience": snapshots["forward_experience"],
        "forward_context_cutoff": experience["forward_context_cutoff"],
        "execution_calibration": experience["live_execution"]["recommended"],
        "casebook": cases,
        "policy": campaign_policy,
        **revision_stamp(root),
    }
    manifest_path = campaign_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    state = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "status": "active",
        "stage": "generate",
        "started_at": current.isoformat(),
        "deadline_at": deadline.isoformat(),
        "manifest": f"{relative_root}/manifest.json",
        "forward_context_cutoff": manifest["forward_context_cutoff"],
        "candidates": [],
        "counts": {"generated": 0, "quick_evaluated": 0, "full_dev": 0, "audited": 0},
    }
    store.write_json(job_id, CAMPAIGN_STATE_PATH, state)
    store.append_journal(
        job_id, {"type": "evolution_campaign_started", "campaign_id": campaign_id}
    )
    return state


def prepare_candidate(
    store: JobStore,
    job_id: str,
    *,
    family: str,
    summary: str,
    mutation_kind: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    state = _active_campaign(store, job_id)
    if _aware(now or datetime.now(UTC)) >= _parse(state["deadline_at"]):
        raise ValueError("evolution campaign generation deadline has elapsed")
    policy = _campaign_policy(store, job_id, str(state["campaign_id"]))
    limit = int(policy["generated_programs"])
    slot = len(state["candidates"]) + 1
    if slot > limit:
        raise ValueError(f"campaign generated-program budget exhausted ({limit})")

    forced_jump = _same_family_nonwins(
        state, family, int(policy["same_family_non_wins"])
    )
    source = "de_novo" if forced_jump else _parent_source(slot, policy["parent_mix"])
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

    parents = _select_parents(store, job_id, source, slot)
    candidate_id = f"{state['campaign_id']}-c{slot:02d}"
    relative = f"{CAMPAIGN_ROOT}/{state['campaign_id']}/candidates/{candidate_id}"
    candidate_root = store.job_dir(job_id) / relative
    _copy_active_bundle(store.job_dir(job_id), candidate_root)
    candidate = {
        "candidate_id": candidate_id,
        "campaign_id": state["campaign_id"],
        "slot": slot,
        "family": family,
        "summary": summary[:160],
        "status": "prepared",
        "parent_source": source,
        "parent_candidate_ids": parents,
        "mutation_kind": chosen_mutation,
        "forced_jump": forced_jump,
        "bundle": relative,
        "prepared_at": utc_now_iso(),
    }
    (candidate_root / "candidate.json").write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    state["candidates"].append(candidate)
    state["counts"]["generated"] = slot
    store.write_json(job_id, CAMPAIGN_STATE_PATH, state)
    record_candidate(
        store,
        job_id,
        candidate_id=candidate_id,
        family=family,
        summary=summary,
        status="generated",
        objective=None,
        parent_candidate_ids=parents,
        metadata={
            "campaign_id": state["campaign_id"],
            "bundle": relative,
            "mutation_kind": chosen_mutation,
            "parent_source": source,
        },
    )
    return candidate


def evaluate_candidate(
    store: JobStore, job_id: str, candidate_id: str
) -> dict[str, Any]:
    """Run static checks and the low-fidelity train screen for one bundle."""
    state = _active_campaign(store, job_id)
    candidate = _candidate(state, candidate_id)
    if candidate["status"] not in {"prepared", "quick_failed"}:
        return candidate
    root = store.job_dir(job_id)
    candidate_root = root / str(candidate["bundle"])
    report = validate_execution_job(job_id, candidate_dir=candidate_root, store=store)
    if not _candidate_validation_passed(report):
        return _reject_candidate(
            store, job_id, state, candidate, "invalid", {"validation": report}
        )
    revision = compute_workspace_revision(candidate_root)
    manifest = store.read_json(job_id, str(state["manifest"]), default={}) or {}
    if (
        revision == manifest.get("source_revision")
        and not (candidate_root / "search_space.json").exists()
    ):
        return _reject_candidate(
            store,
            job_id,
            state,
            candidate,
            "invalid",
            {"error": "candidate bundle is identical to its source revision"},
        )
    try:
        subject = _load_subject(
            store, job_id, candidate_root, campaign_id=str(state["campaign_id"])
        )
        train_end, validation_end = _split_bounds(
            store, job_id, campaign_id=str(state["campaign_id"])
        )
        train, _, _ = _split_dataset(
            subject["dataset"], train_end=train_end, validation_end=validation_end
        )
        quick = _tail(train, 10_000)
        params, _, calibration = _calibrated_params(store, job_id, subject)
        result = simulate_execution(subject["script"], quick, subject["spec"], params)
    except Exception as exc:  # noqa: BLE001 - candidate failure is evidence
        return _reject_candidate(
            store,
            job_id,
            state,
            candidate,
            "invalid",
            {"error": str(exc)[:500]},
        )
    compact = _compact_result(result)
    if not result.validation.get("execution_valid"):
        return _reject_candidate(
            store, job_id, state, candidate, "low_fidelity_rejected", compact
        )
    candidate.update(
        {
            "status": "quick_complete",
            "revision": revision,
            "quick": compact,
            "objective": _objective(result.stats, params),
            "behavior": _behavior(result, quick, subject["spec"]),
            "execution_calibration": calibration,
            "evaluated_at": utc_now_iso(),
        }
    )
    state["counts"]["quick_evaluated"] += 1
    _save_campaign(store, job_id, state)
    record_candidate(
        store,
        job_id,
        candidate_id=candidate_id,
        family=str(candidate["family"]),
        summary=str(candidate["summary"]),
        status="generated",
        objective=candidate["objective"],
        revision=candidate["revision"],
        behavior=candidate["behavior"],
        evidence="low-fidelity train screen passed",
        metadata={"quick": compact, "execution_calibration": calibration},
    )
    return candidate


def finalize_campaign(store: JobStore, job_id: str) -> dict[str, Any]:
    """Spend bounded full-dev and sealed-audit budgets, then close campaign."""
    state = _active_campaign(store, job_id)
    policy = _campaign_policy(store, job_id, str(state["campaign_id"]))
    quick = [
        item for item in state["candidates"] if item.get("status") == "quick_complete"
    ]
    quick.sort(key=_candidate_score, reverse=True)
    remaining_dev = max(
        0,
        int(policy["full_dev_survivors"]) - int(state["counts"]["full_dev"]),
    )
    survivors = quick[:remaining_dev]
    state["stage"] = "full_dev"
    _save_campaign(store, job_id, state)
    dev_passed = [
        item for item in state["candidates"] if item.get("status") == "dev_frontier"
    ]
    full_dev_before = int(state["counts"]["full_dev"])
    for index, candidate in enumerate(survivors):
        try:
            outcome = _full_dev(
                store,
                job_id,
                candidate,
                tune=(full_dev_before + index < int(policy["inner_optuna_finalists"])),
            )
        except Exception as exc:  # noqa: BLE001 - isolate candidate failures
            outcome = {
                "status": "invalid",
                "evidence": f"full development evaluation failed: {str(exc)[:300]}",
            }
        state["counts"]["full_dev"] += 1
        candidate.update(outcome)
        if candidate["status"] == "dev_frontier":
            dev_passed.append(candidate)
        set_candidate_status(
            store,
            job_id,
            str(candidate["candidate_id"]),
            str(candidate["status"]),
            evidence=str(candidate.get("evidence") or "full development evaluation"),
        )
        record_candidate(
            store,
            job_id,
            candidate_id=str(candidate["candidate_id"]),
            family=str(candidate["family"]),
            summary=str(candidate["summary"]),
            status=str(candidate["status"]),
            objective=candidate.get("objective"),
            revision=candidate.get("revision"),
            behavior=candidate.get("behavior"),
            evidence=str(candidate.get("evidence") or "full development evaluation"),
            metadata={
                "dev": candidate.get("dev"),
                "tuning": candidate.get("tuning"),
            },
        )
        _save_campaign(store, job_id, state)

    dev_passed.sort(key=_candidate_score, reverse=True)
    remaining_audits = max(
        0, int(policy["sealed_audits"]) - int(state["counts"]["audited"])
    )
    audits = dev_passed[:remaining_audits]
    state["stage"] = "sealed_audit"
    state["audit_opened_at"] = utc_now_iso()
    _save_campaign(store, job_id, state)
    for candidate in audits:
        try:
            outcome = _sealed_audit(store, job_id, state, candidate)
        except Exception as exc:  # noqa: BLE001 - isolate candidate failures
            outcome = {
                "status": "audit_rejected",
                "evidence": f"reserved audit failed: {str(exc)[:300]}",
            }
        candidate.update(outcome)
        state["counts"]["audited"] += 1
        set_candidate_status(
            store,
            job_id,
            str(candidate["candidate_id"]),
            str(candidate["status"]),
            evidence=str(candidate.get("evidence") or "sealed audit"),
        )
        record_candidate(
            store,
            job_id,
            candidate_id=str(candidate["candidate_id"]),
            family=str(candidate["family"]),
            summary=str(candidate["summary"]),
            status=str(candidate["status"]),
            objective=candidate.get("objective"),
            revision=candidate.get("revision"),
            behavior=candidate.get("behavior"),
            evidence=str(candidate.get("evidence") or "reserved audit"),
            metadata={"audit": candidate.get("audit")},
        )
        _save_campaign(store, job_id, state)

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
        },
    )
    return state


def campaign_prompt_block(store: JobStore, job_id: str) -> dict[str, Any] | None:
    """Bounded dynamic context; the full casebook is never reloaded into prompts."""
    try:
        state = maybe_start_campaign(store, job_id)
    except (FileNotFoundError, ValueError) as exc:
        return {"status": "blocked", "reason": str(exc)}
    if not state or state.get("status") != "active":
        return None
    manifest = store.read_json(job_id, str(state["manifest"]), default={}) or {}
    candidates = state.get("candidates") or []
    prepared = [item for item in candidates if item.get("status") == "prepared"]
    policy = manifest.get("policy") or {}
    deadline_elapsed = datetime.now(UTC) >= _parse(state["deadline_at"])
    if deadline_elapsed:
        next_action = "Generation deadline elapsed; run evolution-finalize now."
    elif prepared:
        next_action = (
            f"Edit only files inside {prepared[0]['bundle']} (workspace, job.yaml, "
            "and optional search_space.json), then run evolution-evaluate for "
            f"{prepared[0]['candidate_id']}."
        )
    elif len(candidates) < int(policy.get("generated_programs") or 0):
        next_action = (
            "Prepare the next candidate with evolution-prepare. At least half "
            "the campaign must change signal/exit/regime/portfolio structure."
        )
    else:
        next_action = "Run evolution-finalize in the isolated background worker."
    return {
        "campaign_id": state["campaign_id"],
        "stage": state["stage"],
        "deadline_at": state["deadline_at"],
        "counts": state["counts"],
        "next_action": next_action,
        "cases": manifest.get("casebook") or [],
        "forward_context_cutoff": state.get("forward_context_cutoff"),
        "constraints": {
            "paper_only": True,
            "live_requires_owner": True,
            "no_audit_access_before_finalists": True,
        },
        "deadline_elapsed": deadline_elapsed,
    }


def _full_dev(
    store: JobStore, job_id: str, candidate: dict[str, Any], *, tune: bool
) -> dict[str, Any]:
    root = store.job_dir(job_id) / str(candidate["bundle"])
    campaign_id = str(
        candidate.get("campaign_id")
        or str(candidate["candidate_id"]).rsplit("-c", 1)[0]
    )
    subject = _load_subject(store, job_id, root, campaign_id=campaign_id)
    train_end, validation_end = _split_bounds(store, job_id, campaign_id=campaign_id)
    train, validation, _ = _split_dataset(
        subject["dataset"], train_end=train_end, validation_end=validation_end
    )
    params, stress_params, calibration = _calibrated_params(store, job_id, subject)
    tuning: dict[str, Any] | None = None
    search_path = root / "search_space.json"
    if tune and search_path.exists():
        search = {
            **params,
            **json.loads(search_path.read_text(encoding="utf-8")),
        }
        if not is_search_space(search):
            raise ValueError(f"candidate search space is not typed: {search_path}")
        policy = _campaign_policy(store, job_id, campaign_id)
        grid = run_optuna_search(
            subject["script"],
            train,
            subject["spec"],
            search,
            rank_by="net_return",
            n_trials=min(int(policy["inner_optuna_trials"]), 20),
            objectives=["net_return", "max_drawdown_pct"],
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
            (root / "job.yaml").write_text(
                yaml.safe_dump(job_data, sort_keys=False), encoding="utf-8"
            )
        tuning = {"trials": len(grid.runs), "selected_params": params}
    revision = compute_workspace_revision(root)
    manifest = (
        store.read_json(
            job_id, f"{CAMPAIGN_ROOT}/{campaign_id}/manifest.json", default={}
        )
        or {}
    )
    if revision == manifest.get("source_revision"):
        raise ValueError("candidate has no effective mutation after tuning")
    train_result, train_stats = _window_result(subject, 0.0, train_end, params)
    validation_result, validation_stats = _window_result(
        subject, train_end, validation_end, params
    )
    stress_result, stress_stats = _window_result(
        subject, train_end, validation_end, stress_params
    )
    passed = (
        train_result.validation.get("execution_valid")
        and validation_result.validation.get("execution_valid")
        and stress_result.validation.get("execution_valid")
        and int(validation_stats.get("trade_count") or 0) > 0
        and float(validation_stats.get("net_return") or 0.0) > 0.0
        and float(stress_stats.get("net_return") or 0.0) > 0.0
        and bool(calibration["audit_passed"])
    )
    objective = _objective(validation_stats, params)
    return {
        "status": "dev_frontier" if passed else "low_fidelity_rejected",
        "revision": revision,
        "params": params,
        "tuning": tuning,
        "execution_calibration": calibration,
        "dev": {
            "train": _compact_result(train_result, stats=train_stats),
            "validation": _compact_result(validation_result, stats=validation_stats),
            "validation_stress": _compact_result(stress_result, stats=stress_stats),
        },
        "objective": objective,
        "behavior": _behavior(
            validation_result,
            validation,
            subject["spec"],
            stats=validation_stats,
            start_at=validation.bars.timestamps[0],
        ),
        "evidence": "positive independent validation"
        if passed
        else "failed independent validation",
    }


def _sealed_audit(
    store: JobStore,
    job_id: str,
    state: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    root = store.job_dir(job_id)
    campaign_id = str(state["campaign_id"])
    subject = _load_subject(
        store,
        job_id,
        root / str(candidate["bundle"]),
        campaign_id=campaign_id,
    )
    incumbent = _load_subject(store, job_id, root, campaign_id=campaign_id)
    candidate_params, candidate_stress_params, calibration = _calibrated_params(
        store, job_id, subject, base=candidate.get("params")
    )
    incumbent_params, _, _ = _calibrated_params(store, job_id, incumbent)
    from wayfinder_paths.jobs.governance import record_evidence_access

    record_evidence_access(
        store.repo_root,
        job_id,
        "evolution_sealed_audit",
        {
            "campaign_id": state["campaign_id"],
            "candidate_id": candidate["candidate_id"],
        },
    )
    _, audit_start = _split_bounds(store, job_id, campaign_id=campaign_id)
    result, result_stats = _window_result(subject, audit_start, 1.0, candidate_params)
    stress, stress_stats = _window_result(
        subject, audit_start, 1.0, candidate_stress_params
    )
    baseline, baseline_stats = _window_result(
        incumbent, audit_start, 1.0, incumbent_params
    )
    candidate_net = float(result_stats.get("net_return") or 0.0)
    baseline_net = float(baseline_stats.get("net_return") or 0.0)
    trades = int(result_stats.get("trade_count") or 0)
    if (
        not calibration["audit_passed"]
        or not result.validation.get("execution_valid")
        or not stress.validation.get("execution_valid")
        or not baseline.validation.get("execution_valid")
        or float(stress_stats.get("net_return") or 0.0) <= 0.0
    ):
        return {
            "status": "audit_rejected",
            "audit": {
                "candidate": _compact_result(result, stats=result_stats),
                "candidate_stress": _compact_result(stress, stats=stress_stats),
                "incumbent": _compact_result(baseline, stats=baseline_stats),
            },
            "evidence": ("failed live-cost stress or held-out execution-cost coverage"),
        }
    try:
        shadow_stream = f"results/forward/shadows/{candidate['candidate_id']}"
        open_paper_probation_leg(
            store,
            job_id,
            name=f"evolution-{candidate['candidate_id']}",
            symbol="portfolio",
            kill_criterion="negative net PnL after 5 closed shadow trades",
            kill_rules={"min_closed_trades": 5, "max_net_pnl": 0.0},
            candidate_net=candidate_net,
            baseline_net=baseline_net,
            backtest_trades=trades,
            candidate_bundle_id=str(candidate["candidate_id"]),
            candidate_bundle=str(candidate["bundle"]),
            campaign_id=str(state["campaign_id"]),
            candidate_revision=str(candidate.get("revision") or ""),
            forward_context_cutoff=str(state.get("forward_context_cutoff") or ""),
            shadow_stream=shadow_stream,
        )
    except ValueError as exc:
        return {
            "status": "audit_rejected",
            "audit": {
                "candidate": _compact_result(result, stats=result_stats),
                "candidate_stress": _compact_result(stress, stats=stress_stats),
                "incumbent": _compact_result(baseline, stats=baseline_stats),
            },
            "evidence": str(exc)[:300],
        }
    return {
        "status": "paper_probation",
        "audit": {
            "candidate": _compact_result(result, stats=result_stats),
            "candidate_stress": _compact_result(stress, stats=stress_stats),
            "incumbent": _compact_result(baseline, stats=baseline_stats),
        },
        "shadow_stream": shadow_stream,
        "evidence": "sealed audit admitted candidate to isolated paper probation",
    }


def _load_subject(
    store: JobStore,
    job_id: str,
    root: Path,
    *,
    campaign_id: str | None = None,
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
    dataset_root = (
        store.job_dir(job_id) / CAMPAIGN_ROOT / campaign_id / CAMPAIGN_DATA_ROOT
        if campaign_id
        else root
    )
    dataset = _load_dataset(
        dataset_root,
        spec,
        job_data,
        feature_roots=(root, dataset_root),
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


def _reject_candidate(
    store: JobStore,
    job_id: str,
    state: dict[str, Any],
    candidate: dict[str, Any],
    status: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    was_pending = candidate.get("status") in {"prepared", "quick_failed"}
    candidate.update(
        {"status": status, "evidence": evidence, "evaluated_at": utc_now_iso()}
    )
    if was_pending:
        state["counts"]["quick_evaluated"] += 1
    _save_campaign(store, job_id, state)
    set_candidate_status(
        store,
        job_id,
        str(candidate["candidate_id"]),
        status,
        evidence=json.dumps(evidence, default=str)[:300],
    )
    return candidate


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


def _select_parents(store: JobStore, job_id: str, source: str, slot: int) -> list[str]:
    archive = load_archive(store, job_id).get("candidates") or []
    incumbents = [
        str(item["candidate_id"])
        for item in archive
        if item.get("status") == "incumbent"
    ]
    elites = [
        str(item["candidate_id"])
        for entries in quality_diversity_snapshot(store, job_id).values()
        for item in entries
    ]
    frontier = [
        str(item["candidate_id"]) for item in archive if item.get("on_frontier")
    ]
    active = f"active:{compute_workspace_revision(store.job_dir(job_id))}"
    pool = list(dict.fromkeys(elites + frontier + incumbents + [active]))
    if source == "de_novo":
        return []
    if source == "incumbent":
        return incumbents[:1] or [active]
    if source == "qd_elite":
        return [pool[(slot - 1) % len(pool)]] if pool else []
    if source == "crossover" and pool:
        first = (slot - 1) % len(pool)
        return list(dict.fromkeys([pool[first], pool[(first + 1) % len(pool)]]))
    return []


def _copy_active_bundle(source_root: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copy2(source_root / "job.yaml", destination / "job.yaml")
    shutil.copytree(source_root / "workspace", destination / "workspace")
    spec = source_root / "execution_spec.json"
    if spec.exists():
        shutil.copy2(spec, destination / "execution_spec.json")


def _snapshot_campaign_inputs(
    active_root: Path,
    campaign_root: Path,
    *,
    dataset_path: Path,
    experience: dict[str, Any],
) -> dict[str, Any]:
    """Copy the mutable research inputs once so every stage sees one record."""
    campaign_root.mkdir(parents=True, exist_ok=False)
    data_root = campaign_root / CAMPAIGN_DATA_ROOT
    bars_path = data_root / "results" / "backtest" / "input_bars.json"
    bars_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dataset_path, bars_path)

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
    forward_path.write_text(
        json.dumps(experience, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "dataset": {
            "path": f"{CAMPAIGN_DATA_ROOT}/results/backtest/input_bars.json",
            "sha256": _file_hash(bars_path),
            "bytes": bars_path.stat().st_size,
        },
        "features": features,
        "forward_experience": {
            "path": FORWARD_SNAPSHOT,
            "sha256": _file_hash(forward_path),
            "bytes": forward_path.stat().st_size,
        },
    }


def _campaign_policy(store: JobStore, job_id: str, campaign_id: str) -> dict[str, Any]:
    manifest = (
        store.read_json(
            job_id, f"{CAMPAIGN_ROOT}/{campaign_id}/manifest.json", default={}
        )
        or {}
    )
    policy = manifest.get("policy")
    if not isinstance(policy, dict):
        raise ValueError(f"evolution campaign {campaign_id!r} has no policy")
    return policy


def _split_bounds(
    store: JobStore, job_id: str, *, campaign_id: str
) -> tuple[float, float]:
    split = _campaign_policy(store, job_id, campaign_id).get("split") or {}
    train = float(split.get("train") or 0.0)
    validation = float(split.get("validation") or 0.0)
    audit = float(split.get("audit") or 0.0)
    if min(train, validation, audit) <= 0 or not math.isclose(
        train + validation + audit, 1.0, abs_tol=1e-9
    ):
        raise ValueError("evolution split must be positive and sum to 1")
    return train, train + validation


def _same_family_nonwins(state: dict[str, Any], family: str, streak: int) -> bool:
    outcomes = [
        item
        for item in reversed(state.get("candidates") or [])
        if item.get("family") == family
        and item.get("status")
        in {
            "invalid",
            "low_fidelity_rejected",
            "audit_rejected",
            "dev_frontier",
            "paper_probation",
        }
    ]
    failures = {"invalid", "low_fidelity_rejected", "audit_rejected"}
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
