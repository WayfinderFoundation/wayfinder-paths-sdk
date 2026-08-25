"""Isolated paper A/B state for hourly-funnel versus evolution candidates."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from wayfinder_paths.jobs.compute_lock import job_state_lock
from wayfinder_paths.jobs.economics import block_bootstrap_lcb
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.improver.spec import ImproverSpec
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.monitor_state import atomic_write_json

EXPERIMENT_STATE_PATH = "state/evolution_experiment.json"
EXPERIMENT_VIEW_PATH = "state/evolution_experiment_view.json"
EXPERIMENT_ROOT = "research/evolution/experiment"
EXPERIMENT_FORWARD_ROOT = "results/forward/experiment"
EXPERIMENT_AUDIT_LEDGER = "audits.jsonl"
EXPERIMENT_AUDIT_SUMMARY = "audits.json"
EXPERIMENT_ARMS = ("control", "evolution")
Arm = Literal["control", "evolution"]


def experiment_status(store: JobStore, job_id: str) -> dict[str, Any]:
    state = store.read_json(job_id, EXPERIMENT_STATE_PATH, default={}) or {}
    return state if isinstance(state, dict) else {}


def ensure_paper_experiment(
    store: JobStore, job_id: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Create the fixed-horizon paper experiment once for its allowed job."""
    spec = ImproverSpec.load(store.job_dir(job_id))
    policy = spec.evolution.get("paper_experiment") or {}
    if not spec.evolution_enabled_for(job_id) or not policy.get("enabled"):
        return None
    _retire_legacy_evolution_legs(store, job_id)
    with job_state_lock(store.repo_root, job_id, name="evolution_experiment"):
        existing = experiment_status(store, job_id)
        if existing:
            return existing
        current = _aware(now or datetime.now(UTC))
        root = store.job_dir(job_id)
        revision = compute_workspace_revision(root)
        experiment_id = f"paper-ab-{current.strftime('%Y%m%dT%H%M%SZ')}"
        candidate_id = f"baseline-{revision[:12]}"
        relative = f"{EXPERIMENT_ROOT}/{experiment_id}/candidates/{candidate_id}"
        _copy_bundle(root, root / relative)
        champion = _champion(
            arm="control",
            candidate_id=candidate_id,
            revision=revision,
            bundle=relative,
            admitted_at=current.isoformat(),
            source="incumbent",
        )
        evolution_champion = _champion(
            arm="evolution",
            candidate_id=candidate_id,
            revision=revision,
            bundle=relative,
            admitted_at=current.isoformat(),
            source="incumbent",
        )
        compute_budget = (
            store.read_json(
                job_id, "state/evolution_compute_budget.json", default={}
            )
            or {}
        )
        state = {
            "schema_version": "1.0",
            "experiment_id": experiment_id,
            "status": "active",
            "started_at": current.isoformat(),
            "ends_at": (
                current + timedelta(days=float(policy.get("duration_days") or 14))
            ).isoformat(),
            "bar_interval": str(policy.get("bar_interval") or "5m"),
            "confidence": float(policy.get("confidence") or 0.90),
            "protocol": {
                "arms": {
                    "control": "hourly_funnel",
                    "evolution": "open_ended_campaign",
                },
                "market_stream": "identical_completed_5m_bars",
                "duration_days": float(policy.get("duration_days") or 14),
                "primary_endpoint": "paired_daily_forward_log_utility_delta_lcb",
                "secondary_endpoints": [
                    "false_promotion_rate",
                    "tokens_per_admission",
                    "sim_hours_per_admission",
                ],
                "decision_rule": {
                    "accrete": "paired_lcb_gt_zero_without_safety_regression",
                    "kill": "paired_ucb_lt_zero_or_hard_constraint_breach",
                    "otherwise": "inconclusive",
                },
                "multiplicity": "shared_benjamini_hochberg_audit_ledger",
                "evolution_compute_duty_cap": float(
                    policy.get("compute_duty_fraction") or 0.20
                ),
                "max_audits_per_arm_per_12h": int(
                    policy.get("max_audits_per_arm_per_window") or 2
                ),
                "resource_budget_tolerance": 0.20,
                "paper_only": True,
            },
            "resource_baseline": {
                "evolution_compute_wall_seconds": float(
                    compute_budget.get("total_wall_seconds") or 0.0
                )
            },
            "initial_revision": revision,
            "last_processed_bar": None,
            "seen_candidates": [],
            "arms": {
                "control": {"champion": champion, "history": []},
                "evolution": {"champion": evolution_champion, "history": []},
            },
            "admissions": {"control": 0, "evolution": 0},
            "windows": {},
        }
        store.write_json(job_id, EXPERIMENT_STATE_PATH, state)
        store.append_journal(
            job_id,
            {
                "type": "evolution_experiment_started",
                "experiment_id": experiment_id,
                "ends_at": state["ends_at"],
            },
        )
        return state


def enqueue_experiment_view(
    store: JobStore,
    job_id: str,
    *,
    rows: list[dict[str, Any]],
    now: pd.Timestamp,
) -> bool:
    """Persist the latest complete rolling view; the worker replays its gaps."""
    state = experiment_status(store, job_id)
    if state.get("status") != "active" or not rows:
        return False
    timestamps = [pd.Timestamp(row["timestamp"]) for row in rows]
    latest = max(timestamps)
    payload = {
        "schema_version": "1.0",
        "captured_at": now.isoformat(),
        "latest_bar": latest.isoformat(),
        "rows": rows,
    }
    atomic_write_json(store.job_dir(job_id) / EXPERIMENT_VIEW_PATH, payload)
    return True


def admit_paper_candidate(
    store: JobStore,
    job_id: str,
    *,
    arm: Arm,
    candidate_id: str,
    candidate_root: Path,
    revision: str,
    source: str,
    evidence: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Auto-accept one screened candidate inside its paper arm only."""
    with job_state_lock(store.repo_root, job_id, name="evolution_shadow_runner"):
        return _admit_paper_candidate(
            store,
            job_id,
            arm=arm,
            candidate_id=candidate_id,
            candidate_root=candidate_root,
            revision=revision,
            source=source,
            evidence=evidence,
            now=now,
        )


def _admit_paper_candidate(
    store: JobStore,
    job_id: str,
    *,
    arm: Arm,
    candidate_id: str,
    candidate_root: Path,
    revision: str,
    source: str,
    evidence: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if arm not in EXPERIMENT_ARMS:
        raise ValueError(f"unknown experiment arm {arm!r}")
    current = _aware(now or datetime.now(UTC))
    ensure_paper_experiment(store, job_id, now=current)
    with job_state_lock(store.repo_root, job_id, name="evolution_experiment"):
        state = experiment_status(store, job_id)
        if state.get("status") != "active":
            raise ValueError("paper experiment is not active")
        policy = (
            ImproverSpec.load(store.job_dir(job_id)).evolution.get("paper_experiment")
            or {}
        )
        window = _window_key(state, current)
        window_state = state.setdefault("windows", {}).setdefault(
            window, {"control": [], "evolution": []}
        )
        limit = int(policy.get("max_audits_per_arm_per_window") or 2)
        if len(window_state[arm]) >= limit:
            raise ValueError(
                f"paper experiment {arm} window admission cap reached ({limit})"
            )
        safe_id = _safe_candidate_id(candidate_id)
        safe_revision = _safe_revision(revision)
        seen_key = f"{arm}:{safe_id}:{safe_revision}"
        if seen_key in state.get("seen_candidates", []):
            return dict(state["arms"][arm]["champion"])
        root = store.job_dir(job_id).resolve()
        source_root = candidate_root.resolve()
        if not source_root.is_relative_to(root):
            raise ValueError("experiment candidate must be inside its job root")
        relative = (
            f"{EXPERIMENT_ROOT}/{state['experiment_id']}/candidates/"
            f"{arm}-{safe_id}-{safe_revision[:12]}"
        )
        destination = (root / relative).resolve()
        allowed = (
            root / EXPERIMENT_ROOT / state["experiment_id"] / "candidates"
        ).resolve()
        if not destination.is_relative_to(allowed):
            raise ValueError("experiment bundle escapes its candidate root")
        _copy_bundle(source_root, destination)
        previous = dict(state["arms"][arm]["champion"])
        previous["retired_at"] = current.isoformat()
        state["arms"][arm]["history"].append(previous)
        champion = _champion(
            arm=arm,
            candidate_id=safe_id,
            revision=safe_revision,
            bundle=relative,
            admitted_at=current.isoformat(),
            source=source,
        )
        state["arms"][arm]["champion"] = champion
        state.setdefault("seen_candidates", []).append(seen_key)
        state["admissions"][arm] = int(state["admissions"].get(arm) or 0) + 1
        window_state[arm].append(seen_key)
        store.write_json(job_id, EXPERIMENT_STATE_PATH, state)
        record_audit(
            store,
            job_id,
            arm=arm,
            candidate_id=safe_id,
            revision=safe_revision,
            admitted=True,
            evidence={
                **(evidence or {}),
                "token_usage": (evidence or {}).get("token_usage")
                or current_job_token_usage(store, job_id, state=state),
            },
        )
        store.append_journal(
            job_id,
            {
                "type": "evolution_experiment_candidate_admitted",
                "arm": arm,
                "candidate_id": safe_id,
                "revision": safe_revision,
                "paper_only": True,
            },
        )
        return champion


def record_audit(
    store: JobStore,
    job_id: str,
    *,
    arm: Arm,
    candidate_id: str,
    revision: str,
    admitted: bool,
    evidence: dict[str, Any],
) -> None:
    with job_state_lock(store.repo_root, job_id, name="evolution_audit_ledger"):
        _record_audit(
            store,
            job_id,
            arm=arm,
            candidate_id=candidate_id,
            revision=revision,
            admitted=admitted,
            evidence=evidence,
        )


def _record_audit(
    store: JobStore,
    job_id: str,
    *,
    arm: Arm,
    candidate_id: str,
    revision: str,
    admitted: bool,
    evidence: dict[str, Any],
) -> None:
    """Append one bounded candidate audit row for cross-arm accounting."""
    delta = evidence.get("paired_incumbent_delta") or {}
    token_usage = evidence.get("token_usage") or current_job_token_usage(store, job_id)
    token_total = int(token_usage.get("tokens_in") or 0) + int(
        token_usage.get("tokens_out") or 0
    )
    prior_token_total = max(
        (int(item.get("token_total") or 0) for item in _audit_rows(store, job_id)),
        default=0,
    )
    row = {
        "ts": utc_now_iso(),
        "arm": arm,
        "candidate_id": candidate_id,
        "revision": revision,
        "admitted": admitted,
        "paired_days": delta.get("paired_days"),
        "estimate": delta.get("estimate"),
        "lcb": delta.get("lcb"),
        "p_value": delta.get("p_value"),
        "token_usage": token_usage,
        "token_total": token_total,
        "token_delta": max(0, token_total - prior_token_total),
        "sim_wall_seconds": evidence.get("sim_wall_seconds"),
    }
    path = _audit_ledger_dir(store, job_id) / EXPERIMENT_AUDIT_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    from wayfinder_paths.jobs.research import bh_qvalues

    rows = _audit_rows(store, job_id)
    indexed = [
        (index, float(item["p_value"]))
        for index, item in enumerate(rows)
        if item.get("p_value") is not None
    ]
    q_by_index = {
        index: q
        for (index, _), q in zip(
            indexed,
            bh_qvalues([p_value for _, p_value in indexed]),
            strict=True,
        )
    }
    atomic_write_json(
        _audit_ledger_dir(store, job_id) / EXPERIMENT_AUDIT_SUMMARY,
        {
            "schema_version": "1.0",
            "method": "benjamini_hochberg",
            "hypotheses": [
                {**item, "q_value": q_by_index.get(index)}
                for index, item in enumerate(rows)
            ],
            "updated_at": utc_now_iso(),
        },
    )


def current_job_token_usage(
    store: JobStore, job_id: str, *, state: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Meter stable worker sessions from the experiment's fixed start."""
    from wayfinder_paths.jobs.benchmarks.agent_adapter import meter_session_ids

    session_ids: list[str] = []
    reports = store.job_dir(job_id) / "reports"
    for path in reports.glob("*/session.json"):
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if session.get("session_id"):
            session_ids.append(str(session["session_id"]))
    experiment = state or experiment_status(store, job_id)
    started = _parse(experiment["started_at"])
    return meter_session_ids(session_ids, since_ms=int(started.timestamp() * 1000))


def harvest_hourly_control_candidates(
    store: JobStore, job_id: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Mirror green hourly-funnel outputs without changing their lifecycle."""
    state = ensure_paper_experiment(store, job_id, now=now)
    if not state or state.get("status") != "active":
        return None
    from wayfinder_paths.jobs.application import _candidate_dir_from_proposal

    candidates: list[tuple[str, dict[str, Any]]] = []
    seen = set(state.get("seen_candidates") or [])
    for proposal in store.proposals(job_id):
        report = proposal.get("candidate_report") or {}
        revision = str(report.get("revision") or "")
        candidate_id = str(proposal.get("proposal_id") or "")
        if not candidate_id or not revision:
            continue
        if f"control:{_safe_candidate_id(candidate_id)}:{revision}" in seen:
            continue
        if (
            (report.get("validation_summary") or {}).get("status") != "passed"
            or (report.get("gate") or {}).get("live_ready") is not True
            or (report.get("economic") or {}).get("ready") is not True
        ):
            continue
        candidates.append((str(proposal.get("created_at") or ""), proposal))
    if not candidates:
        return None
    _, proposal = max(candidates, key=lambda item: item[0])
    report = proposal["candidate_report"]
    candidate_root = _candidate_dir_from_proposal(store, job_id, proposal)
    if not candidate_root.exists():
        return None
    return admit_paper_candidate(
        store,
        job_id,
        arm="control",
        candidate_id=str(proposal["proposal_id"]),
        candidate_root=candidate_root,
        revision=str(report["revision"]),
        source="hourly_funnel",
        evidence=report.get("economic") or {},
        now=now,
    )


def maybe_finalize_experiment(
    store: JobStore, job_id: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Freeze the pre-registered 14-day verdict; never changes live state."""
    with job_state_lock(store.repo_root, job_id, name="evolution_shadow_runner"):
        return _maybe_finalize_experiment(store, job_id, now=now)


def _maybe_finalize_experiment(
    store: JobStore, job_id: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    current = _aware(now or datetime.now(UTC))
    with job_state_lock(store.repo_root, job_id, name="evolution_experiment"):
        state = experiment_status(store, job_id)
        if state.get("status") != "active" or current < _parse(state.get("ends_at")):
            return None
        report = _verdict_report(store, job_id, state)
        state.update(
            {
                "status": "complete",
                "completed_at": current.isoformat(),
                "verdict": report,
            }
        )
        store.write_json(job_id, EXPERIMENT_STATE_PATH, state)
        store.write_json(job_id, "results/research/evolution_experiment.json", report)
        store.append_journal(
            job_id,
            {
                "type": "evolution_experiment_completed",
                "verdict": report["verdict"],
                "paper_only": True,
            },
        )
        return report


def resolve_experiment_bundle(
    store: JobStore, job_id: str, state: dict[str, Any], champion: dict[str, Any]
) -> Path:
    relative = str(champion.get("bundle") or "").strip()
    if not relative or Path(relative).is_absolute():
        raise ValueError("experiment bundle must be a non-empty relative path")
    root = store.job_dir(job_id).resolve()
    allowed = (root / EXPERIMENT_ROOT / state["experiment_id"] / "candidates").resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(allowed) or candidate.parent != allowed:
        raise ValueError("experiment bundle escapes its candidate root")
    return candidate


def _champion(
    *,
    arm: Arm,
    candidate_id: str,
    revision: str,
    bundle: str,
    admitted_at: str,
    source: str,
) -> dict[str, Any]:
    return {
        "arm": arm,
        "candidate_id": candidate_id,
        "revision": revision,
        "bundle": bundle,
        "admitted_at": admitted_at,
        "source": source,
        "stream": f"{EXPERIMENT_FORWARD_ROOT}/{arm}/{revision}",
    }


def _retire_legacy_evolution_legs(store: JobStore, job_id: str) -> None:
    """One-time migration: release paper legs created by the old shared rail."""
    from wayfinder_paths.jobs.probation import load_probation, update_probation_leg

    for leg in load_probation(store, job_id).get("legs") or []:
        if (
            leg.get("status") == "active"
            and leg.get("tier") == "paper"
            and leg.get("candidate_bundle_id")
            and leg.get("campaign_id")
        ):
            update_probation_leg(
                store,
                job_id,
                str(leg["name"]),
                status="killed",
                progress="migrated to the isolated paper A/B rail",
            )


def _copy_bundle(source: Path, destination: Path) -> None:
    if destination.exists():
        if (destination / "workspace").is_dir() and (destination / "job.yaml").is_file():
            return
        raise FileExistsError(f"incomplete paper bundle exists at {destination}")
    workspace = source / "workspace"
    job_yaml = source / "job.yaml"
    if not workspace.is_dir() or not job_yaml.is_file():
        raise FileNotFoundError("paper candidate requires workspace/ and job.yaml")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        shutil.copytree(workspace, temporary / "workspace")
        shutil.copy2(job_yaml, temporary / "job.yaml")
        search = source / "search_space.json"
        if search.is_file():
            shutil.copy2(search, temporary / search.name)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _safe_candidate_id(value: str) -> str:
    raw = str(value or "").strip()
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in raw)
    if not safe:
        raise ValueError("candidate id is required")
    return f"{safe[:48]}-{hashlib.sha256(raw.encode()).hexdigest()[:8]}"


def _safe_revision(value: str) -> str:
    revision = str(value or "").strip()
    if not revision or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for char in revision
    ):
        raise ValueError("candidate revision must be a non-empty path-safe token")
    return revision


def _verdict_report(
    store: JobStore, job_id: str, state: dict[str, Any]
) -> dict[str, Any]:
    daily = {arm: _daily_pnl(store, job_id, state, arm) for arm in EXPERIMENT_ARMS}
    days = sorted(set(daily["control"]) & set(daily["evolution"]))
    capital = 10_000.0
    deltas = [
        math.log1p(daily["evolution"][day] / capital)
        - math.log1p(daily["control"][day] / capital)
        for day in days
        if daily["evolution"][day] > -capital and daily["control"][day] > -capital
    ]
    confidence = float(state.get("confidence") or 0.90)
    lcb = block_bootstrap_lcb(
        deltas, block_len=5, iterations=500, confidence=confidence
    )
    negative_lcb = block_bootstrap_lcb(
        [-value for value in deltas],
        block_len=5,
        iterations=500,
        confidence=confidence,
    )
    ucb = -negative_lcb if negative_lcb is not None else None
    control_admissions = int((state.get("admissions") or {}).get("control") or 0)
    evolution_admissions = int((state.get("admissions") or {}).get("evolution") or 0)
    from wayfinder_paths.jobs.constitution import load_constitution

    max_drawdown = float(
        load_constitution(store.job_dir(job_id))["hard_constraints"]["max_drawdown_pct"]
    )
    hard_breach = _max_drawdown(daily["evolution"], capital) > max_drawdown
    false_promotions = {
        arm: _false_promotion_rate(store, job_id, state, arm) for arm in EXPERIMENT_ARMS
    }
    false_promotion_safe = (
        false_promotions["evolution"]["rate"] <= false_promotions["control"]["rate"]
    )
    resource_cost = _resource_cost(store, job_id, state)
    budget_balance = _budget_balance(
        resource_cost,
        tolerance=float(
            (state.get("protocol") or {}).get("resource_budget_tolerance") or 0.20
        ),
    )
    if (
        lcb is not None
        and lcb > 0
        and not hard_breach
        and false_promotion_safe
        and evolution_admissions >= control_admissions
        and budget_balance["matched"]
    ):
        verdict = "accrete"
    elif (ucb is not None and ucb < 0) or hard_breach:
        verdict = "kill"
    else:
        verdict = "inconclusive"
    return {
        "schema_version": "1.0",
        "experiment_id": state["experiment_id"],
        "verdict": verdict,
        "paper_only": True,
        "paired_days": len(deltas),
        "paired_delta_estimate": sum(deltas),
        "paired_delta_lcb": lcb,
        "paired_delta_ucb": ucb,
        "confidence": confidence,
        "hard_constraint_breach": hard_breach,
        "admissions": dict(state.get("admissions") or {}),
        "false_promotions": false_promotions,
        "resource_cost": resource_cost,
        "resource_budget_balance": budget_balance,
        "generated_at": utc_now_iso(),
    }


def _daily_pnl(
    store: JobStore, job_id: str, state: dict[str, Any], arm: str
) -> dict[str, float]:
    champions = [
        *(state["arms"][arm].get("history") or []),
        state["arms"][arm]["champion"],
    ]
    totals: dict[str, float] = {}
    for champion in champions:
        stream = store.job_dir(job_id) / str(champion["stream"])
        for day, pnl in _daily_pnl_for_stream(stream).items():
            totals[day] = totals.get(day, 0.0) + pnl
    return totals


def _max_drawdown(daily: dict[str, float], capital: float) -> float:
    equity = capital
    peak = capital
    drawdown = 0.0
    for day in sorted(daily):
        equity += daily[day]
        peak = max(peak, equity)
        if peak > 0:
            drawdown = max(drawdown, (peak - equity) / peak)
    return drawdown


def _false_promotion_rate(
    store: JobStore, job_id: str, state: dict[str, Any], arm: str
) -> dict[str, Any]:
    current_end = _parse(state.get("completed_at") or state["ends_at"])
    candidates = [
        *(state["arms"][arm].get("history") or []),
        state["arms"][arm]["champion"],
    ]
    matured = 0
    false = 0
    for candidate in candidates:
        if candidate.get("source") == "incumbent":
            continue
        start = _parse(candidate["admitted_at"])
        end = (
            _parse(candidate.get("retired_at"))
            if candidate.get("retired_at")
            else current_end
        )
        if end - start < timedelta(days=7):
            continue
        matured += 1
        stream = store.job_dir(job_id) / str(candidate["stream"])
        pnl = sum(_daily_pnl_for_stream(stream).values())
        false += int(pnl < 0)
    return {
        "matured": matured,
        "false": false,
        "rate": false / matured if matured else 0.0,
    }


def _daily_pnl_for_stream(stream: Path) -> dict[str, float]:
    totals: dict[str, float] = {}
    ticks = stream / "ticks.jsonl"
    if ticks.exists():
        for line in ticks.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
                day = str(pd.Timestamp(row.get("bar_ts") or row.get("ts")).date())
            except (TypeError, ValueError):
                continue
            totals.setdefault(day, 0.0)
    path = stream / "trades.jsonl"
    if not path.exists():
        return totals
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
            day = str(pd.Timestamp(row.get("ts") or row.get("timestamp")).date())
            pnl = float(row.get("net_pnl") or row.get("realized_pnl_delta") or 0.0)
        except (TypeError, ValueError):
            continue
        totals[day] = totals.get(day, 0.0) + pnl
    return totals


def _resource_cost(
    store: JobStore, job_id: str, state: dict[str, Any]
) -> dict[str, Any]:
    rows = _audit_rows(store, job_id)
    output: dict[str, Any] = {}
    for arm in EXPERIMENT_ARMS:
        selected = [row for row in rows if row.get("arm") == arm]
        tokens = sum(int(row.get("token_delta") or 0) for row in selected)
        sim_seconds = sum(float(row.get("sim_wall_seconds") or 0.0) for row in selected)
        if arm == "evolution":
            compute = (
                store.read_json(
                    job_id, "state/evolution_compute_budget.json", default={}
                )
                or {}
            )
            baseline = float(
                (state.get("resource_baseline") or {}).get(
                    "evolution_compute_wall_seconds"
                )
                or 0.0
            )
            sim_seconds = max(
                sim_seconds,
                float(compute.get("total_wall_seconds") or 0.0) - baseline,
            )
        admissions = int((state.get("admissions") or {}).get(arm) or 0)
        candidates = [
            *(state["arms"][arm].get("history") or []),
            state["arms"][arm]["champion"],
        ]
        admitted = [
            _parse(candidate["admitted_at"])
            for candidate in candidates
            if candidate.get("source") != "incumbent"
        ]
        output[arm] = {
            "tokens": tokens,
            "sim_wall_seconds": round(sim_seconds, 3),
            "time_to_first_admission_hours": (
                round(
                    (min(admitted) - _parse(state["started_at"])).total_seconds()
                    / 3600,
                    3,
                )
                if admitted
                else None
            ),
            "tokens_per_admission": tokens / admissions if admissions else None,
            "sim_hours_per_admission": (
                sim_seconds / 3600 / admissions if admissions else None
            ),
        }
    return output


def _budget_balance(
    costs: dict[str, Any], *, tolerance: float
) -> dict[str, Any]:
    ratios: dict[str, float | None] = {}
    matched = True
    for metric in ("tokens", "sim_wall_seconds"):
        control = float((costs.get("control") or {}).get(metric) or 0.0)
        evolution = float((costs.get("evolution") or {}).get(metric) or 0.0)
        if control == evolution == 0.0:
            ratios[metric] = None
            continue
        if min(control, evolution) <= 0.0:
            ratios[metric] = None
            matched = False
            continue
        ratio = max(control, evolution) / min(control, evolution)
        ratios[metric] = round(ratio, 4)
        matched = matched and ratio <= 1.0 + tolerance
    return {"matched": matched, "tolerance": tolerance, "ratios": ratios}


def _parse(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return _aware(parsed)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _window_key(state: dict[str, Any], now: datetime) -> str:
    elapsed = max(0.0, (now - _parse(state["started_at"])).total_seconds())
    return str(int(elapsed // (12 * 3600)))


def _audit_ledger_dir(store: JobStore, job_id: str) -> Path:
    return store.repo_root / "audit" / job_id / "evolution_experiment"


def _audit_rows(store: JobStore, job_id: str) -> list[dict[str, Any]]:
    path = _audit_ledger_dir(store, job_id) / EXPERIMENT_AUDIT_LEDGER
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows
