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

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.application import (
    _prepare_candidate_workspace,
    ensure_jobs_v1_contract,
    validate_candidate_bundle,
)
from wayfinder_paths.jobs.execution.job import (
    backtest_execution_job,
    synthesize_scenario_plan,
)
from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
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
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import sync_all_jobs
from wayfinder_paths.jobs.validation import validation_summary
from wayfinder_paths.jobs.worker import JOB_RESULT_MARKER

PROPOSAL_KINDS = {"code_change", "params_update", "model_update", "improver_change"}


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
    if memo:
        memo_path = store.job_dir(job_id) / "proposals" / f"{pid}.md"
        memo_path.parent.mkdir(parents=True, exist_ok=True)
        memo_path.write_text(memo.rstrip() + "\n", encoding="utf-8")

    validation, candidate_report = _generate_candidate_report(
        store, job_id, proposal, candidate_dir, base_revision=base_revision, pid=pid
    )
    proposal["candidate_report"] = candidate_report

    store.write_proposal(job_id, proposal)
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
) -> tuple[dict[str, Any], dict[str, Any]]:
    validation = validate_candidate_bundle(store, job_id, proposal, candidate_dir)
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
            economic = evaluate_economic_gate(
                job_id,
                candidate_dir=candidate_dir,
                store=store,
                probation=probation,
            )
        except Exception as exc:  # noqa: BLE001 — a crashed economic eval must
            # not break propose; the record carries the REAL enforcement so
            # the approval gate can fail closed on live-capable jobs
            # (previously the crash forced "advisory" — the review's central
            # fail-open finding).
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
        "generated_at": utc_now_iso(),
    }
    return validation, candidate_report


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
