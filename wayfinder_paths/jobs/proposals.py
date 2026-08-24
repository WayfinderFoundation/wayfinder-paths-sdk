"""Structured propose flow: pre-approval candidates with evidence.

`propose_change` is the sanctioned way for an agent (or human) to put a
strategy change in front of the user: it stages the change as a candidate
workspace BEFORE approval, runs the full candidate validation (backtest +
preflight + execution validation, all revision-stamped), builds a
baseline-vs-candidate comparison, and attaches a bounded `candidate_report`
to the proposal — the exact payload the backend approve gate and the FE
review UI consume (contract C1).

The pre-approval candidate is reused at claim time (application.py verifies
its recorded revision), so the change the user approved is byte-for-byte the
change that gets promoted.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.application import (
    _candidate_dir_from_proposal,
    _prepare_candidate_workspace,
    ensure_jobs_v1_contract,
    validate_candidate_bundle,
)
from wayfinder_paths.jobs.compute_lock import ComputeLockBusy, heavy_compute_lock
from wayfinder_paths.jobs.evidence_reuse import assess_evidence_reuse
from wayfinder_paths.jobs.execution.job import (
    backtest_execution_job,
    synthesize_scenario_plan,
)
from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.failures import TransientInfrastructureError, classify_failure
from wayfinder_paths.jobs.gating import (
    compute_workspace_revision,
    evaluate_economic_gate,
    evaluate_live_gate,
)
from wayfinder_paths.jobs.improver.spec import (
    ImproverSpec,
    improver_revision,
    merge_over_defaults,
    revision_stamp,
)
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.robustness import latest_robustness_summary
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import sync_all_jobs
from wayfinder_paths.jobs.validation import (
    candidate_dataset_fingerprint,
    validation_failure_text,
    validation_summary,
)
from wayfinder_paths.jobs.worker import JOB_RESULT_MARKER

PROPOSAL_KINDS = {"code_change", "params_update", "model_update", "improver_change"}
# Bounded wait for the machine-wide heavy-compute lock before the single
# propose-time retry of an infrastructure-failed candidate validation.
PROPOSE_LOCK_WAIT_ENV = "WAYFINDER_PROPOSE_LOCK_WAIT_SECONDS"
_PROPOSE_LOCK_WAIT_DEFAULT_S = 120.0

# ── Paper auto-apply tier ────────────────────────────────────────────────
# Owner-approved doctrine: paper proposal approvals are mechanical — a
# gate-green candidate on a job that cannot touch live capital auto-applies
# with visibility (journal + owner_attention.decided_autonomously) and a
# bounded undo, instead of waiting on an owner click. Autonomy changes WHO
# clicks, not WHAT is checked: the auto path routes through the exact
# `store.approve_proposal` gate (validation + live-ready + governance +
# candidate freshness), unchanged.
PAPER_AUTO_APPLY_ENV = "WAYFINDER_PAPER_AUTO_APPLY"  # "0" disables
PAPER_AUTO_APPLY_DEFAULT_KINDS = frozenset({"params_update"})
# improver_change is a criterion/search-policy change — governance-shaped,
# owner-only regardless of any auto_limits override.
PAPER_AUTO_APPLY_ALLOWED_KINDS = frozenset(
    {"params_update", "code_change", "model_update"}
)
PAPER_AUTO_APPLY_DAILY_CAP = 3
PAPER_AUTO_APPLY_UNDO_WINDOW_HOURS = 72
# Owner-owned knobs may never ride the auto tier, whatever the params look
# like: sizing (leverage), custody (wallet), execution mode, governance.
_PAPER_AUTO_APPLY_FORBIDDEN_PARAM = re.compile(
    r"leverage|wallet|mode|governance", re.IGNORECASE
)


def paper_auto_apply_enabled() -> bool:
    return os.environ.get(PAPER_AUTO_APPLY_ENV) != "0"


def _param_keys(value: Any, prefix: str = "") -> list[str]:
    if not isinstance(value, dict):
        return []
    keys: list[str] = []
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        keys.append(path)
        keys.extend(_param_keys(child, path))
    return keys


def _auto_applies_last_day(store: JobStore, job_id: str) -> int:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    count = 0
    for event in store.read_jsonl(job_id, "journal.jsonl"):
        if event.get("type") != "proposal_auto_applied":
            continue
        try:
            ts = dt.datetime.fromisoformat(str(event.get("ts")))
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.UTC)
        if ts >= cutoff:
            count += 1
    return count


def paper_auto_apply_blockers(
    store: JobStore, job_id: str, proposal: dict[str, Any]
) -> list[str]:
    """Why this proposal must wait for an owner click. Empty = auto-eligible."""
    from wayfinder_paths.jobs.owner_attention import job_live_capital_risk

    blockers: list[str] = []
    job = store.load(job_id)
    if job.execution_contract != "jobs_v1":
        blockers.append("legacy execution contract")
    if str(job.script_loop.mode or "paper") != "paper":
        blockers.append("script mode is not paper")
    if job_live_capital_risk(job):
        blockers.append("job is live-capable (live mode or wallet bound)")
    limits = dict(job.agent_loop.auto_limits or {})
    allowed_kinds = (
        set(limits.get("auto_apply_kinds") or PAPER_AUTO_APPLY_DEFAULT_KINDS)
        & PAPER_AUTO_APPLY_ALLOWED_KINDS
    )
    kind = str(proposal.get("kind") or "")
    if kind not in allowed_kinds:
        blockers.append(f"kind {kind} not auto-appliable ({sorted(allowed_kinds)})")
    params = (proposal.get("proposed_change") or {}).get("execution_params") or {}
    forbidden = sorted(
        key
        for key in _param_keys(params)
        if _PAPER_AUTO_APPLY_FORBIDDEN_PARAM.search(key)
    )
    if forbidden:
        blockers.append(f"params touch owner-owned keys: {forbidden}")
    report = proposal.get("candidate_report") or {}
    if (report.get("validation_summary") or {}).get("status") != "passed":
        blockers.append("candidate validation not passed")
    if report.get("mode") != "validation_only":
        if (report.get("gate") or {}).get("live_ready") is not True:
            blockers.append("candidate gate not green")
        # Paper tier mirrors the paper approve gate's advisory semantics: an
        # explicit economic ready=False blocks; absent/unevaluated does not.
        if (report.get("economic") or {}).get("ready") is False:
            blockers.append("candidate not economic-ready")
    cap = int(limits.get("max_auto_applies_per_day") or PAPER_AUTO_APPLY_DAILY_CAP)
    if _auto_applies_last_day(store, job_id) >= cap:
        blockers.append(f"daily auto-apply cap reached ({cap}/day)")
    return blockers


def maybe_auto_apply_paper_proposal(
    store: JobStore, job_id: str, proposal_id: str
) -> dict[str, Any] | None:
    """Auto-approve and queue a gate-green paper proposal at staging time.

    Returns the auto-apply record when taken, None when the proposal stays on
    the owner-approval path. Reuses the restage approval-carryover machinery:
    `store.approve_proposal` (full gate, unchanged) + `launch_application`
    (detached completer, watchdog-backstopped)."""
    if not paper_auto_apply_enabled():
        return None
    proposal = store.load_proposal(job_id, proposal_id)
    if proposal.get("status") != "pending":
        return None
    if paper_auto_apply_blockers(store, job_id, proposal):
        return None
    try:
        proposal = store.approve_proposal(job_id, proposal_id)
    except ValueError:
        # The real gate said no — our eligibility precheck was optimistic.
        # The proposal stays pending on the owner path; nothing to unwind.
        return None
    proposal["approval"]["required"] = False
    proposal["approval"]["by"] = "paper-auto-apply"
    proposal["updated_at"] = utc_now_iso()
    store.write_proposal(job_id, proposal)
    report = proposal.get("candidate_report") or {}
    undo = {
        "command": f"wayfinder job rollback-apply {job_id} {proposal_id}",
        "window_expires_ts": (
            dt.datetime.now(dt.UTC)
            + dt.timedelta(hours=PAPER_AUTO_APPLY_UNDO_WINDOW_HOURS)
        ).isoformat(),
    }
    store.append_journal(
        job_id,
        {
            "type": "proposal_auto_applied",
            "proposal_id": proposal_id,
            "kind": proposal.get("kind"),
            "tier": "paper",
            "evidence": {
                "validation_status": "passed",
                "gate_live_ready": (report.get("gate") or {}).get("live_ready"),
                "economic_ready": (report.get("economic") or {}).get("ready"),
                "candidate_revision": report.get("revision"),
            },
            "undo": undo,
        },
    )
    from wayfinder_paths.jobs.apply_launcher import launch_application

    try:
        launch_application(store, job_id, proposal_id)
    except Exception as exc:  # noqa: BLE001 — the proposal is approved+queued;
        # the application watchdog backstops a failed launch (_recover_queued).
        store.append_journal(
            job_id,
            {
                "type": "proposal_auto_apply_launch_failed",
                "proposal_id": proposal_id,
                "error": str(exc)[:300],
            },
        )
    return {"proposal_id": proposal_id, "auto_applied": True, "undo": undo}


def propose_change(
    store: JobStore,
    job_id: str,
    *,
    kind: str,
    summary: str,
    intent_contract: dict[str, Any],
    params: dict[str, Any] | None = None,
    candidate_source: str | Path | None = None,
    improver: dict[str, Any] | None = None,
    scenario_plan: dict[str, Any] | None = None,
    proposal_id: str | None = None,
    memo: str | None = None,
) -> dict[str, Any]:
    """Create a pending proposal backed by a validated pre-approval candidate.

    The change is supplied either as `params` (merged into the candidate
    job.yaml execution_params) or `candidate_source` (a directory the agent
    already edited — a full bundle with `workspace/` [+ `job.yaml`] or a bare
    workspace tree). Exactly one is required for code/params changes.

    `memo` is the human-facing markdown proposal memo (status quo / what the
    data shows / proposed change / expected impact / risks / validation /
    approval requested). It is written to `proposals/<pid>.md` and carried in
    the proposal's `change_summary`, which the backend already surfaces to
    the review UI — light surfacing with zero backend/FE changes.
    """
    if kind not in PROPOSAL_KINDS:
        raise ValueError(f"kind must be one of {sorted(PROPOSAL_KINDS)}: {kind}")
    if kind == "improver_change":
        if not isinstance(improver, dict) or not improver:
            raise ValueError(
                "improver_change proposals require the full proposed spec via "
                "improver={...}"
            )
        if params is not None or candidate_source is not None:
            raise ValueError(
                "improver_change proposals change search policy only — no "
                "params or candidate_source"
            )
        _validate_improver_payload(improver)
    elif improver is not None:
        raise ValueError("improver payload is only valid for kind='improver_change'")
    elif params is None and candidate_source is None:
        raise ValueError("pass params and/or candidate_source — nothing to propose")
    ensure_jobs_v1_contract(store, job_id)

    root = store.job_dir(job_id)
    base_revision = compute_workspace_revision(root)
    pid = proposal_id or f"prop-{kind.replace('_', '-')}-{uuid.uuid4().hex[:8]}"

    candidate_descriptor = _prepare_candidate_workspace(
        store, job_id, pid, force_fresh=True
    )
    candidate_dir = store.repo_root / candidate_descriptor["candidate_dir"]
    _overlay_change(candidate_dir, candidate_source=candidate_source, params=params)
    changed_files = _diff_workspaces(root, candidate_dir)

    job = store.load(job_id)
    resolved_plan = scenario_plan
    if not resolved_plan:
        job_data = json.loads(json.dumps(job.to_dict(), default=str))
        resolved_plan = job_data.get("execution_scenario_plan") or (
            (job_data.get("execution_spec") or {}).get("validation") or {}
        ).get("execution_scenario_plan")
        if not resolved_plan and job.script_loop.enabled:
            resolved_plan = synthesize_scenario_plan(
                root,
                ExecutionSpec.from_dict(dict(job.execution_spec or {})),
                job_data,
            )

    proposal: dict[str, Any] = {
        "proposal_id": pid,
        "job_id": job_id,
        "status": "pending",
        "kind": kind,
        "proposed_change": {
            "summary": summary,
            **({"execution_params": dict(params)} if params else {}),
            **({"improver": dict(improver)} if improver else {}),
        },
        "intent_contract": dict(intent_contract),
        "scenario_plan": resolved_plan or {"scenarios": []},
        "base_revision": base_revision,
        **(
            {"base_improver_revision": improver_revision(root)}
            if kind == "improver_change"
            else {}
        ),
        **revision_stamp(root),
        "changed_files": changed_files,
        "change_summary": memo or summary,
        "application": {"status": "not_requested", **candidate_descriptor},
    }
    from wayfinder_paths.jobs.remediation import proposal_remediation_stamp

    remediation = proposal_remediation_stamp(store, job_id)
    if remediation:
        proposal["remediation"] = remediation
    if memo:
        memo_path = store.job_dir(job_id) / "proposals" / f"{pid}.md"
        memo_path.parent.mkdir(parents=True, exist_ok=True)
        memo_path.write_text(memo.rstrip() + "\n", encoding="utf-8")

    try:
        validation, candidate_report = _generate_candidate_report(
            store, job_id, proposal, candidate_dir, base_revision=base_revision, pid=pid
        )
    except TransientInfrastructureError as exc:
        # Abort BEFORE any proposal file exists: a transient box condition
        # (OOM/lock/timeout) must never freeze into an immutable
        # candidate_report that the approve gate then refuses forever.
        shutil.rmtree(candidate_dir.parent, ignore_errors=True)
        if memo:
            memo_path = store.job_dir(job_id) / "proposals" / f"{pid}.md"
            memo_path.unlink(missing_ok=True)
        store.append_journal(
            job_id,
            {
                "type": "proposal_propose_aborted",
                "proposal_id": pid,
                "failure_kind": "infrastructure",
                "error": str(exc)[:300],
            },
        )
        raise
    proposal["candidate_report"] = candidate_report

    store.write_proposal(job_id, proposal)
    from wayfinder_paths.jobs.remediation import link_remediation_proposal

    link_remediation_proposal(store, job_id, proposal)
    store.append_journal(
        job_id,
        {
            "type": "proposal_created",
            "proposal_id": pid,
            "kind": kind,
            "base_revision": base_revision,
            "candidate_revision": candidate_report["revision"],
            "validation_status": validation.get("status"),
        },
    )
    try:
        from wayfinder_paths.jobs.archive import load_archive, record_candidate
        from wayfinder_paths.jobs.execution.experiments import _behavior_descriptor

        change = proposal.get("proposed_change") or {}
        economic = candidate_report.get("economic") or {}
        candidate_revision = str(candidate_report.get("revision") or "")
        # Content-derived id: the same workspace content re-proposed lands on
        # the SAME entry (dedup + sticky refutation), while every proposal
        # UUID accumulates on it. Parent edge resolves base_revision to the
        # archived candidate it points at, falling back to the raw revision.
        candidate_id = f"cand-{candidate_revision}" if candidate_revision else pid
        parent_entry = next(
            (
                entry
                for entry in load_archive(store, job_id).get("candidates") or []
                if entry.get("revision") == base_revision
            ),
            None,
        )
        comparison = candidate_report.get("comparison") or {}
        record_candidate(
            store,
            job_id,
            candidate_id=candidate_id,
            family=(
                "probation"
                if change.get("probation")
                else "params"
                if change.get("execution_params")
                else str(kind or "code")
            ),
            summary=str(summary or ""),
            status="probation" if change.get("probation") else "archived",
            objective=(economic.get("objective") or {}).get("candidate"),
            revision=candidate_revision or None,
            parent_id=base_revision,
            parent_candidate_ids=[
                parent_entry["candidate_id"] if parent_entry else base_revision
            ],
            proposal_id=pid,
            behavior=_behavior_descriptor(comparison.get("candidate") or {}),
        )
    except Exception:  # noqa: BLE001 — archive bookkeeping never breaks propose
        pass
    try:
        maybe_auto_apply_paper_proposal(store, job_id, pid)
    except Exception as exc:  # noqa: BLE001 — the pending proposal is intact
        # and the owner-approval path unaffected; record the miss.
        store.append_journal(
            job_id,
            {
                "type": "proposal_auto_apply_error",
                "proposal_id": pid,
                "error": str(exc)[:300],
            },
        )
    store.refresh_scorecard(job_id)
    sync_all_jobs(store=store)
    # Surface a chat affordance (contract C5): the opencode harness turns this
    # marker into a job_result part; the FE renders a review deep-link chip.
    print(
        JOB_RESULT_MARKER
        + json.dumps(
            {
                "type": "job_result",
                "severity": "info",
                "summary": f"Proposal created: {summary}",
                "job_id": job_id,
                "proposal_id": pid,
            }
        )
    )
    return store.load_proposal(job_id, pid)


def _validate_improver_payload(improver: dict[str, Any]) -> None:
    """Fail at propose time, not at apply time: the merged spec must satisfy
    every typed accessor the code consumes."""
    probe = ImproverSpec(
        revision="proposed", source="proposal", policy=merge_over_defaults(improver)
    )
    if probe.staleness_experiment_days <= 0 or probe.staleness_wakes < 1:
        raise ValueError("staleness thresholds must be positive")
    if probe.ideation_due_s <= 0 or probe.ideation_overdue_s < probe.ideation_due_s:
        raise ValueError("ideation cadence must be positive with overdue >= due")
    if probe.stuck_same_family_non_wins < 1:
        raise ValueError("stuck_rule.same_family_non_wins must be >= 1")
    if probe.probation_max_active_legs < 1:
        raise ValueError("probation.max_active_legs must be >= 1")
    if not 0 < probe.probation_max_size_fraction <= 1:
        raise ValueError("probation.max_size_fraction must be in (0, 1]")
    if probe.paper_max_active_legs < 1:
        raise ValueError("probation.paper_max_active_legs must be >= 1")
    if probe.paper_regression_budget_pct < 0 or probe.paper_regression_budget_frac < 0:
        raise ValueError("paper regression budget terms must be >= 0")
    if probe.paper_min_backtest_trades < 1 or probe.paper_floor_min_trades < 1:
        raise ValueError("paper trade floors must be >= 1")
    weights = probe.island_weights
    if weights and abs(sum(weights.values()) - 1.0) > 0.01:
        raise ValueError(f"island weights must sum to 1.0, got {sum(weights.values())}")
    if not 0 <= probe.exploration_floor <= 1:
        raise ValueError("islands.exploration_floor must be in [0, 1]")


def _overlay_change(
    candidate_dir: Path,
    *,
    candidate_source: str | Path | None,
    params: dict[str, Any] | None,
) -> None:
    """Apply the proposed change to a freshly staged candidate."""
    if candidate_source is not None:
        source = Path(candidate_source)
        if not source.exists():
            raise FileNotFoundError(f"candidate_source not found: {source}")
        if (source / "workspace").exists():
            # Full bundle shape: workspace/ (+ optional job.yaml).
            _replace_tree(source / "workspace", candidate_dir / "workspace")
            if (source / "job.yaml").exists():
                shutil.copy2(source / "job.yaml", candidate_dir / "job.yaml")
        else:
            # Bare workspace tree.
            _replace_tree(source, candidate_dir / "workspace")
    if params:
        yaml_path = candidate_dir / "job.yaml"
        job_yaml = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        job_yaml["execution_params"] = {
            **(job_yaml.get("execution_params") or {}),
            **params,
        }
        yaml_path.write_text(
            yaml.safe_dump(job_yaml, sort_keys=False), encoding="utf-8"
        )


def _generate_candidate_report(
    store: JobStore,
    job_id: str,
    proposal: dict[str, Any],
    candidate_dir: Path,
    *,
    base_revision: str,
    pid: str,
    skip_behavior_checks: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """`skip_behavior_checks=True` keeps only the cheap validation invariants
    — set exclusively when the prior behavioral evidence is provably
    reusable (`assess_evidence_reuse`); the candidate backtest artifact from
    that prior run still feeds the comparison below."""
    validation = validate_candidate_bundle(
        store,
        job_id,
        proposal,
        candidate_dir,
        skip_behavior_checks=skip_behavior_checks,
    )
    validation = _retry_infrastructure_failure(
        store, job_id, proposal, candidate_dir, validation
    )
    candidate_revision = compute_workspace_revision(candidate_dir)
    comparison = _build_comparison(
        store, job_id, candidate_dir, base_revision=base_revision, pid=pid
    )
    if comparison is not None:
        gate = evaluate_live_gate(job_id, candidate_dir=candidate_dir, store=store)
        gate_payload = {
            "live_ready": gate.get("live_ready"),
            "reasons": gate.get("reasons") or [],
        }
        mode = "full"
        probation = bool((proposal.get("proposed_change") or {}).get("probation"))
        try:
            economic = _economic_gate_with_infra_retry(
                store, job_id, candidate_dir, probation=probation
            )
        except TransientInfrastructureError:
            # Box condition, not an economic verdict — propagate so the
            # caller aborts instead of freezing ready=None + a bars/lock
            # error into the immutable report that the fail-closed approve
            # gate then ESCALATEs forever (2026-08-24 production incident:
            # a transiently unlinked .wayfinder symlink made the bars file
            # unreadable for exactly the economic step).
            raise
        except Exception as exc:  # noqa: BLE001 — an evidence-class crashed
            # economic eval must not break propose; the record carries the
            # REAL enforcement so the approval gate can fail closed on
            # live-capable jobs (previously the crash forced "advisory" —
            # the review's central fail-open finding).
            from wayfinder_paths.jobs.constitution import load_constitution

            constitution = load_constitution(store.job_dir(job_id))
            economic = {
                "ready": None,
                "reasons": [f"economic evaluation failed: {exc}"[:300]],
                "enforcement": constitution.get("enforcement") or "advisory",
                "constitution_revision": constitution.get("revision"),
                "status": "error",
                "escalate": True,
            }
    else:
        # Research-only / no-dataset jobs: nothing to backtest or gate — the
        # proposal is judged on validation alone (contract C1).
        gate_payload = {
            "live_ready": None,
            "reasons": ["no execution backtest; validation-only proposal"],
        }
        mode = "validation_only"
        economic = None

    candidate_report = {
        "revision": candidate_revision,
        "base_revision": base_revision,
        "mode": mode,
        "gate": gate_payload,
        "economic": economic,
        # Content hash of the dataset (+ declared feature stores) this
        # report's backtest consumed. The apply pipeline re-derives it and
        # skips the expensive re-validation when it (and the candidate
        # revision) provably match — see application.assess_validation_reuse.
        "dataset_fingerprint": (
            candidate_dataset_fingerprint(candidate_dir, store.job_dir(job_id))
            if mode == "full"
            else None
        ),
        "validation_summary": validation_summary(validation),
        # Stats/deltas only — never point series (sync payload discipline).
        "comparison": (
            {
                "baseline": comparison["baseline"],
                "candidate": comparison["candidate"],
                "deltas": comparison["deltas"],
                "dataset": comparison["dataset"],
            }
            if comparison is not None
            else None
        ),
        "robustness": latest_robustness_summary(
            store,
            job_id,
            candidate_revision=candidate_revision,
            candidate_dir=candidate_dir,
        ),
        "generated_at": utc_now_iso(),
    }
    return validation, candidate_report


def _retry_infrastructure_failure(
    store: JobStore,
    job_id: str,
    proposal: dict[str, Any],
    candidate_dir: Path,
    validation: dict[str, Any],
) -> dict[str, Any]:
    """Infrastructure failures (ComputeLockBusy, OOM, timeouts) are box
    conditions, not evidence about the candidate — freezing one into an
    immutable candidate_report leaves a pending proposal the approve gate
    refuses forever (verified production dead end). Retry ONCE after a
    bounded wait for the heavy-compute lock; if the box is still unhealthy,
    raise so the caller aborts instead of staging a failed report.
    Evidence-class failures pass through untouched — a real verdict must
    still stage."""
    if validation["status"] != "failed":
        return validation
    failure_text = validation_failure_text(validation)
    if classify_failure(failure_text) != "infrastructure":
        return validation
    wait_s = float(
        os.environ.get(PROPOSE_LOCK_WAIT_ENV, "") or _PROPOSE_LOCK_WAIT_DEFAULT_S
    )
    if _compute_lock_freed(store, job_id, wait_s):
        validation = validate_candidate_bundle(store, job_id, proposal, candidate_dir)
        if validation["status"] != "failed":
            return validation
        failure_text = validation_failure_text(validation)
        if classify_failure(failure_text) != "infrastructure":
            return validation
    raise TransientInfrastructureError(
        "transient infrastructure failure — retry when the box is quiet: "
        f"{failure_text[:300] or 'unknown'}"
    )


def _compute_lock_freed(store: JobStore, job_id: str, wait_s: float) -> bool:
    """Bounded wait for the machine-wide heavy-compute lock to come free.
    Acquire-and-release only — the retry's own backtest re-acquires it.
    False means the box is still busy after the wait."""
    try:
        with heavy_compute_lock(
            repo_root=store.repo_root,
            label=f"propose-retry-wait:{job_id}",
            timeout_s=wait_s,
        ):
            return True
    except ComputeLockBusy:
        return False


def _economic_gate_with_infra_retry(
    store: JobStore,
    job_id: str,
    candidate_dir: Path,
    *,
    probation: bool,
) -> dict[str, Any]:
    """Economic evaluation with the same infra-vs-evidence asymmetry as
    candidate validation (`_retry_infrastructure_failure`): an
    infrastructure-class crash (missing bars dataset, lock, OOM, timeout)
    says nothing about the candidate's economics. Retry ONCE after a bounded
    wait for the heavy-compute lock; if the box is still unhealthy, raise
    TransientInfrastructureError so the propose aborts instead of staging an
    un-approvable report. Evidence-class crashes propagate untouched — the
    caller stages them with ready=None so the real enforcement rides in the
    record."""
    try:
        return evaluate_economic_gate(
            job_id, candidate_dir=candidate_dir, store=store, probation=probation
        )
    except Exception as exc:  # noqa: BLE001 — classify before deciding
        if classify_failure(str(exc)) != "infrastructure":
            raise
        failure_text = str(exc)
    wait_s = float(
        os.environ.get(PROPOSE_LOCK_WAIT_ENV, "") or _PROPOSE_LOCK_WAIT_DEFAULT_S
    )
    if _compute_lock_freed(store, job_id, wait_s):
        try:
            return evaluate_economic_gate(
                job_id, candidate_dir=candidate_dir, store=store, probation=probation
            )
        except Exception as exc:  # noqa: BLE001 — classify before deciding
            if classify_failure(str(exc)) != "infrastructure":
                raise
            failure_text = str(exc)
    raise TransientInfrastructureError(
        "transient infrastructure failure — retry when the box is quiet: "
        f"economic evaluation failed: {failure_text[:300] or 'unknown'}"
    )


def revalidate_proposal(
    store: JobStore, job_id: str, proposal_id: str
) -> dict[str, Any]:
    """Re-run the full validation/comparison/economic evaluation for a
    PENDING proposal against the SAME staged candidate and base revision,
    replacing the embedded candidate_report — including its `economic` block.

    Recovery path for reports frozen by a transient infrastructure failure —
    either validation (validation_summary.failure_kind == "infrastructure")
    or an economic evaluation that crashed on a box condition and froze
    ready=None + the error into the snapshot: the propose-time snapshot is
    immutable, so without this the only exit from an infra-poisoned pending
    proposal is reject-and-repropose. The candidate and
    base revision are NOT rebuilt — the same evidence question, asked again
    on a quiet box. If the job's active revision moved past base_revision
    the comparison baseline would drift, so refuse: the owner should reject
    and re-propose against the current workspace instead.

    Evidence reuse: only GREEN, provably-unchanged pieces skip re-running
    (`assess_evidence_reuse`, phase "revalidate") — a green validation half
    keeps its candidate backtest; the economic block always re-evaluates
    (with its own persisted-fold reuse inside `evaluate_economic_gate`).
    Failed/poisoned pieces re-run in full: that is what revalidate is for.
    """
    proposal = store.load_proposal(job_id, proposal_id)
    if proposal["status"] != "pending":
        raise ValueError(
            f"Only pending proposals can be revalidated: {proposal_id} is "
            f"{proposal['status']} (stale approved candidates use "
            "`wayfinder job restage`)"
        )
    candidate_dir = _candidate_dir_from_proposal(store, job_id, proposal)
    if not candidate_dir.exists():
        raise FileNotFoundError(
            f"candidate bundle for {proposal_id} no longer exists at "
            f"{candidate_dir} — reject the proposal and propose fresh"
        )
    root = store.job_dir(job_id)
    base_revision = str(proposal.get("base_revision") or "")
    current_revision = compute_workspace_revision(root)
    if current_revision != base_revision:
        raise ValueError(
            f"cannot revalidate {proposal_id}: the job's active revision "
            f"({current_revision[:12]}) moved past the proposal's base "
            f"revision ({base_revision[:12]}) — the baseline comparison "
            "would drift. Reject the proposal and propose fresh against the "
            "current workspace."
        )
    old_summary = (proposal.get("candidate_report") or {}).get(
        "validation_summary"
    ) or {}
    # Evidence reuse (validation half only): when the candidate, dataset and
    # baseline are PROVABLY unchanged and the frozen VALIDATION evidence is
    # green, re-running the expensive candidate backtest asks an answered
    # question. The economic block is regenerated regardless — revalidate
    # exists to CURE poisoned reports (the #700 incident shape), and reuse
    # must never short-circuit the cure: a failed validation half re-runs
    # (that is the point), and the economic evaluation below re-runs with
    # its own persisted-fold reuse deciding whether ITS green half replays.
    reuse = assess_evidence_reuse(
        store, job_id, proposal, candidate_dir, phase="revalidate"
    )
    validation: dict[str, Any] | None = None
    candidate_report: dict[str, Any] | None = None
    if reuse["eligible"]:
        validation, candidate_report = _generate_candidate_report(
            store,
            job_id,
            proposal,
            candidate_dir,
            base_revision=base_revision,
            pid=proposal_id,
            skip_behavior_checks=True,
        )
        if validation["status"] == "passed":
            candidate_report["evidence_reuse"] = dict(reuse["proof"])
            store.append_journal(
                job_id,
                {
                    "type": "revalidate_evidence_reused",
                    "proposal_id": proposal_id,
                    **reuse["proof"],
                },
            )
        else:
            # Defense in depth: a failed cheap invariant with green frozen
            # evidence means something moved outside the fingerprinted
            # surface — fall back to the authoritative full re-validation.
            store.append_journal(
                job_id,
                {
                    "type": "revalidate_evidence_rerun",
                    "proposal_id": proposal_id,
                    "reason": "cheap_invariants_failed",
                },
            )
            validation = candidate_report = None
    else:
        store.append_journal(
            job_id,
            {
                "type": "revalidate_evidence_rerun",
                "proposal_id": proposal_id,
                "reason": reuse["reason"],
                **({"details": reuse["proof"]} if reuse["proof"] else {}),
            },
        )
    if candidate_report is None or validation is None:
        validation, candidate_report = _generate_candidate_report(
            store,
            job_id,
            proposal,
            candidate_dir,
            base_revision=base_revision,
            pid=proposal_id,
        )
    proposal["candidate_report"] = candidate_report
    # Recomputed against the same base: unchanged for an intact candidate,
    # honest for one that was edited since propose (the regenerated report's
    # revision covers the current bytes either way).
    proposal["changed_files"] = _diff_workspaces(root, candidate_dir)
    proposal["updated_at"] = utc_now_iso()
    store.write_proposal(job_id, proposal)
    from wayfinder_paths.jobs.remediation import link_remediation_proposal

    link_remediation_proposal(store, job_id, proposal)
    store.append_journal(
        job_id,
        {
            "type": "proposal_revalidated",
            "proposal_id": proposal_id,
            "old_validation_status": old_summary.get("status"),
            "old_failure_kind": old_summary.get("failure_kind"),
            "new_validation_status": validation.get("status"),
            "candidate_revision": candidate_report["revision"],
        },
    )
    store.refresh_scorecard(job_id)
    sync_all_jobs(store=store)
    return store.load_proposal(job_id, proposal_id)


def restage_proposal(
    store: JobStore,
    job_id: str,
    proposal_id: str,
    *,
    candidate_source: str | Path | None = None,
) -> dict[str, Any]:
    """Re-stage an APPROVED proposal whose candidate went stale under it.

    Approval carryover: the owner approved the proposal's intent; when an
    intervening apply moves the workspace, the snapshot candidate can no
    longer promote (stale-baseline refusal). This rebuilds the candidate
    against the CURRENT workspace — mechanically for params updates, from an
    agent-re-authored tree for code changes — re-runs the full propose-time
    gates, and auto-queues the apply. The intent contract, summary, and kind
    are carried over verbatim; a different change requires a fresh proposal
    and a fresh approval.

    If the re-staged candidate fails the approve-time gate on the new base,
    the proposal is auto-rejected (housekeeping) — the world changed
    materially, so the owner must review a fresh proposal instead.
    """
    proposal = store.load_proposal(job_id, proposal_id)
    if proposal["status"] != "approved":
        raise ValueError(f"Only approved proposals can be re-staged: {proposal_id}")
    application = proposal["application"]
    if application["status"] in {"applying", "applied"}:
        raise ValueError(
            f"Proposal application is {application['status']}; nothing to re-stage"
        )
    if not application.get("restage_requested"):
        raise ValueError(
            f"Proposal has no pending re-stage request: {proposal_id} "
            "(re-stage is only for stale-baseline apply refusals)"
        )
    params = (proposal.get("proposed_change") or {}).get("execution_params")
    if candidate_source is None and not params:
        raise ValueError(
            "code-change re-stage requires candidate_source (re-author the "
            "change against the current workspace and pass the edited tree)"
        )

    root = store.job_dir(job_id)
    old_base = str(proposal.get("base_revision") or "")
    old_candidate = str((proposal.get("candidate_report") or {}).get("revision") or "")
    base_revision = compute_workspace_revision(root)

    candidate_descriptor = _prepare_candidate_workspace(
        store, job_id, proposal_id, force_fresh=True
    )
    candidate_dir = store.repo_root / candidate_descriptor["candidate_dir"]
    _overlay_change(candidate_dir, candidate_source=candidate_source, params=params)
    changed_files = _diff_workspaces(root, candidate_dir)

    proposal["base_revision"] = base_revision
    proposal["changed_files"] = changed_files
    application.update(candidate_descriptor)
    application["restage_requested"] = False
    application["error"] = None
    _, candidate_report = _generate_candidate_report(
        store,
        job_id,
        proposal,
        candidate_dir,
        base_revision=base_revision,
        pid=proposal_id,
    )
    proposal["candidate_report"] = candidate_report
    proposal["updated_at"] = utc_now_iso()
    store.write_proposal(job_id, proposal)
    from wayfinder_paths.jobs.remediation import link_remediation_proposal

    link_remediation_proposal(store, job_id, proposal)
    store.append_journal(
        job_id,
        {
            "type": "proposal_restaged",
            "proposal_id": proposal_id,
            "old_base_revision": old_base,
            "new_base_revision": base_revision,
            "old_candidate_revision": old_candidate,
            "new_candidate_revision": candidate_report["revision"],
            "changed_files": changed_files,
        },
    )

    try:
        # Exact approve-time gate semantics (live-ready + candidate freshness).
        store._ensure_candidate_report_gate(job_id, proposal, allow_ungated=False)
    except ValueError as exc:
        rejected = store.reject_proposal(
            job_id,
            proposal_id,
            reason=(
                f"re-stage gate failed on current base {base_revision}: {exc}. "
                "The workspace changed materially since approval — propose the "
                "change fresh so the owner can review it against the new base."
            ),
            rejected_by="agent",
        )
        sync_all_jobs(store=store)
        return rejected

    store.queue_proposal_application(job_id, proposal_id)
    from wayfinder_paths.jobs.apply_launcher import launch_application

    launch_application(store, job_id, proposal_id)
    sync_all_jobs(store=store)
    return store.load_proposal(job_id, proposal_id)


def _replace_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _diff_workspaces(root: Path, candidate_dir: Path) -> list[str]:
    """Repo-relative-ish (candidate-relative) list of files whose bytes differ
    between the active bundle and the candidate. Bounded to keep the synced
    proposal payload small."""
    changed: list[str] = []
    active_ws = root / "workspace"
    candidate_ws = candidate_dir / "workspace"
    seen: set[str] = set()
    for base, other, prefix in (
        (candidate_ws, active_ws, "workspace"),
        (active_ws, candidate_ws, "workspace"),
    ):
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = f"{prefix}/{path.relative_to(base)}"
            if rel in seen:
                continue
            seen.add(rel)
            counterpart = other / path.relative_to(base)
            if (
                not counterpart.exists()
                or counterpart.read_bytes() != path.read_bytes()
            ):
                changed.append(rel)
    active_yaml = root / "job.yaml"
    candidate_yaml = candidate_dir / "job.yaml"
    if (
        active_yaml.exists()
        and candidate_yaml.exists()
        and active_yaml.read_bytes() != candidate_yaml.read_bytes()
    ):
        changed.append("job.yaml")
    return sorted(changed)[:100]


def _build_comparison(
    store: JobStore,
    job_id: str,
    candidate_dir: Path,
    *,
    base_revision: str,
    pid: str,
) -> dict[str, Any] | None:
    """Baseline-vs-candidate backtest comparison on the same dataset.

    Candidate side reuses the artifact candidate validation just wrote (no
    second sim). Baseline reuses the active latest.json when it matches the
    base revision; otherwise one fresh backtest re-stamps it. Returns None
    when the job has nothing to backtest (research-only jobs)."""
    candidate_latest = _read_json(
        candidate_dir / "results" / "backtest" / "latest.json"
    )
    if not candidate_latest:
        return None
    root = store.job_dir(job_id)
    baseline_latest = _read_json(root / "results" / "backtest" / "latest.json")
    if not baseline_latest or baseline_latest.get("revision") != base_revision:
        try:
            payload = backtest_execution_job(job_id, store=store)
        except Exception:
            payload = None
        if payload:
            baseline_latest = {
                **(payload.get("result") or {}),
                "revision": payload.get("revision"),
                "dataset": payload.get("dataset"),
            }
        else:
            baseline_latest = None

    candidate_side = {
        "run_id": candidate_latest.get("run_id"),
        "revision": candidate_latest.get("revision"),
        "stats": candidate_latest.get("stats") or {},
    }
    baseline_side = (
        {
            "run_id": baseline_latest.get("run_id"),
            "revision": baseline_latest.get("revision"),
            "stats": baseline_latest.get("stats") or {},
        }
        if baseline_latest
        else None
    )
    baseline_stats = baseline_side["stats"] if baseline_side else {}
    deltas: dict[str, float] = {}
    for key, candidate_value in candidate_side["stats"].items():
        baseline_value = baseline_stats.get(key)
        match candidate_value, baseline_value:
            case (int() | float(), int() | float()):
                deltas[key] = float(candidate_value) - float(baseline_value)
    comparison = {
        "baseline": baseline_side,
        "candidate": candidate_side,
        "deltas": deltas,
        "dataset": candidate_latest.get("dataset") or {},
        "generated_at": utc_now_iso(),
    }
    comparison_path = store.job_dir(job_id) / "applications" / pid / "comparison.json"
    comparison_path.parent.mkdir(parents=True, exist_ok=True)
    comparison_path.write_text(
        json.dumps(comparison, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return comparison


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    match loaded:
        case dict():
            return loaded
        case _:
            return None
