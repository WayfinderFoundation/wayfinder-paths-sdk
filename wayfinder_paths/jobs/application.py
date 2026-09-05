from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.compiler import JobCompiler

# Redundant alias: re-exported — #705 introduced the kill-switch here and
# callers/tests import it from this module.
from wayfinder_paths.jobs.evidence_reuse import (
    APPLY_ALWAYS_REVALIDATE_ENV as APPLY_ALWAYS_REVALIDATE_ENV,
)
from wayfinder_paths.jobs.evidence_reuse import (
    assess_evidence_reuse,
)
from wayfinder_paths.jobs.execution.validation import validate_execution_job
from wayfinder_paths.jobs.gating import compute_workspace_revision, evaluate_live_gate
from wayfinder_paths.jobs.improver.spec import (
    IMPROVER_FILENAME,
    improver_revision,
)
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.runner_bridge import RunnerBridge
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import sync_all_jobs
from wayfinder_paths.jobs.validation import (
    validate_candidate_application,
    validation_summary,
)


@dataclass
class _ApplicationOutcome:
    final_status: str
    final_error: str | None = None
    deterministic_validation: dict[str, Any] | None = None
    promoted_revision: str | None = None
    compile_result: dict[str, Any] | None = None
    rollback: dict[str, Any] | None = None
    restage_requested: bool = False


def ensure_jobs_v1_contract(
    store: JobStore, job_id: str, *, allow_legacy: bool = False
) -> None:
    """Guard for the versioned-change flow (approve/apply/propose): legacy
    jobs cannot enter it. Shared by CLI and MCP so both surfaces refuse
    identically instead of failing later at candidate validation."""
    if allow_legacy:
        return
    job = store.load(job_id)
    if job.execution_contract != "jobs_v1":
        raise ValueError(
            "job is on the legacy execution contract; run "
            "`wayfinder job migrate-contract` before approving proposals"
        )


def pause_job_loops(store: JobStore, job_id: str) -> list[dict[str, Any]]:
    job = store.load(job_id)
    bridge = RunnerBridge(repo_root=store.repo_root)
    return _apply_runner_action(bridge, job, "pause")


def resume_job_loops(store: JobStore, job_id: str) -> list[dict[str, Any]]:
    job = store.load(job_id)
    bridge = RunnerBridge(repo_root=store.repo_root)
    return _apply_runner_action(bridge, job, "resume")


def claim_application(store: JobStore, job_id: str, proposal_id: str) -> dict[str, Any]:
    from wayfinder_paths.jobs.constitution import load_constitution

    constitution = load_constitution(store.job_dir(job_id))
    if (constitution.get("governance") or {}).get("chain_status") == "tampered":
        raise ValueError(
            "ESCALATE: governance chain is tampered — no application may be "
            "claimed until the owner inspects and re-commits"
        )
    proposal = store.load_proposal(job_id, proposal_id)
    application_status = proposal["application"]["status"]
    if proposal["status"] != "approved":
        raise ValueError(f"Proposal is not approved: {proposal_id}")
    if application_status not in {"queued", "failed"}:
        raise ValueError(
            f"Proposal application is not queued: {proposal_id} ({application_status})"
        )
    # Claiming pauses the job's loops — make sure the recovery owner exists
    # before entering the risk window. Best-effort: registration failure must
    # never block an apply. Lazy import: watchdog imports this module.
    try:
        from wayfinder_paths.jobs.watchdog import ensure_application_watchdog

        ensure_application_watchdog(store=store)
    except Exception as exc:
        store.append_journal(
            job_id,
            {"type": "application_watchdog_ensure_failed", "error": str(exc)},
        )
    paused = pause_job_loops(store, job_id)
    try:
        candidate = _prepare_candidate_workspace(
            store, job_id, proposal_id, proposal=proposal
        )
        proposal = store.claim_proposal_application(
            job_id,
            proposal_id,
            paused_runner_jobs=paused,
            candidate=candidate,
        )
    except Exception:
        resume_job_loops(store, job_id)
        raise
    # Best-effort: this sync is UI telemetry, but it sits between the claim
    # (loops now paused) and the caller spawning the completer. A backend
    # hiccup here severed that chain in production (2026-07-23): the claim
    # stood, the completer never spawned, and the job sat dark until the
    # watchdog's 15-minute recovery. Telemetry must never strand a claim.
    try:
        sync_all_jobs(store=store)
    except Exception as exc:  # noqa: BLE001
        store.append_journal(
            job_id, {"type": "claim_sync_failed", "error": str(exc)[:300]}
        )
    return {"proposal": proposal, "paused_runner_jobs": paused, "candidate": candidate}


def validate_application_candidate(
    store: JobStore,
    job_id: str,
    proposal_id: str,
    *,
    require_judge: bool | None = None,
    allow_legacy: bool = False,
) -> dict[str, Any]:
    proposal = store.load_proposal(job_id, proposal_id)
    application_status = proposal["application"]["status"]
    if application_status != "applying":
        raise ValueError(
            f"Proposal application is not applying: {proposal_id} "
            f"({application_status})"
        )
    validation = validate_candidate_application(
        repo_root=store.repo_root,
        job_dir=store.job_dir(job_id),
        proposal=proposal,
        candidate_dir=_candidate_dir_from_proposal(store, job_id, proposal),
        require_judge=bool(proposal.get("judge_required"))
        if require_judge is None
        else require_judge,
        allow_legacy=allow_legacy,
    )
    validation = _with_execution_validation(
        store,
        job_id,
        proposal,
        validation,
    )
    store.record_proposal_application_validation(job_id, proposal_id, validation)
    return validation


def complete_application(
    store: JobStore,
    job_id: str,
    proposal_id: str,
    *,
    status: str,
    changed_files: list[str] | None = None,
    validation: dict[str, Any] | None = None,
    error: str | None = None,
    allow_legacy: bool = False,
) -> dict[str, Any]:
    outcome = _ApplicationOutcome(final_status=status, final_error=error)
    try:
        if status == "applied":
            outcome = _complete_applied_application(
                store,
                job_id,
                proposal_id,
                changed_files=changed_files,
                allow_legacy=allow_legacy,
            )
        elif status == "failed":
            _write_apply_report(
                store,
                job_id,
                proposal_id,
                status="red",
                summary=f"Application failed before promotion: {error or 'unspecified'}",
                changed_files=changed_files or [],
                validation=validation or {},
                error=error,
            )
    except Exception as exc:
        outcome.final_status = "failed"
        outcome.final_error = str(exc)
        outcome.deterministic_validation = outcome.deterministic_validation or {
            "status": "failed",
            "checks": [],
            "error": outcome.final_error,
        }
        try:
            _write_apply_report(
                store,
                job_id,
                proposal_id,
                status="red",
                summary=f"Failed to apply approved proposal: {outcome.final_error}",
                changed_files=changed_files or [],
                validation=outcome.deterministic_validation,
                promoted_revision=outcome.promoted_revision,
                compile_result=outcome.compile_result,
                error=outcome.final_error,
                rollback=outcome.rollback,
            )
        except Exception:
            pass
    runner_responses = resume_job_loops(store, job_id)
    validation_payload = dict(validation or {})
    if outcome.deterministic_validation is not None:
        validation_payload["deterministic_validation"] = (
            outcome.deterministic_validation
        )
    proposal = store.load_proposal(job_id, proposal_id)
    validation_attempts = proposal["application"].get("validation_attempts")
    if validation_attempts and "validation_attempts" not in validation_payload:
        validation_payload["validation_attempts"] = validation_attempts
    if outcome.promoted_revision:
        validation_payload["promoted_revision"] = outcome.promoted_revision
    if outcome.rollback:
        validation_payload["rollback"] = outcome.rollback
    proposal = store.complete_proposal_application(
        job_id,
        proposal_id,
        status=outcome.final_status,  # type: ignore[arg-type]
        changed_files=changed_files,
        validation=validation_payload,
        error=outcome.final_error,
        runner_responses=runner_responses,
        promoted_revision=outcome.promoted_revision,
        rollback=outcome.rollback,
    )
    if outcome.restage_requested:
        # Approval carryover: the proposal stays approved; the flag marks it
        # as awaiting an agent re-stage against the moved workspace. Wake the
        # agent now (after loops resumed) instead of waiting out its interval.
        proposal["application"]["restage_requested"] = True
        store.write_proposal(job_id, proposal)
        from wayfinder_paths.jobs.triggers import fire_triggers

        fire_triggers(
            store,
            store.load(job_id),
            ["proposal_restage_requested"],
            source=f"apply:{proposal_id}",
        )
    sync_all_jobs(store=store)
    return {
        "proposal": proposal,
        "compile": outcome.compile_result,
        "deterministic_validation": outcome.deterministic_validation,
        "promoted_revision": outcome.promoted_revision,
        "rollback": outcome.rollback,
        "resumed_runner_jobs": runner_responses,
    }


def assess_validation_reuse(
    store: JobStore,
    job_id: str,
    proposal: dict[str, Any],
    candidate_dir: Path,
) -> dict[str, Any]:
    """Mechanical eligibility check for reusing the propose-time validation
    evidence at apply time, instead of re-running the ~30-minute candidate
    backtest against a provably identical candidate + dataset.

    Returns `{"eligible": bool, "reason": str, "proof": dict}` — `reason`
    names the FIRST failed condition when ineligible; `proof` carries the
    content-derived identity when eligible (revisions, dataset fingerprint,
    frozen-report hash). Every condition is a hash comparison or a frozen
    field read — never trust-based:

    1. kill-switch off (`WAYFINDER_APPLY_ALWAYS_REVALIDATE=1` forces rerun)
    2. frozen candidate_report present, mode "full", with a revision
    3. active workspace revision is still base or candidate (no drift)
    4. candidate dir still hashes to the report's revision (same content
       equivalence as `store._ensure_candidate_matches_report`)
    5. dataset fingerprint recorded at propose time matches the one
       re-derived now (input bars + declared feature stores, by content)
    6. live-capable freshness bound (`evidence_reuse` module docstring)
    7. the frozen evidence is green: validation_summary passed AND
       economic.ready is True — failed/poisoned evidence is never reused

    Thin wrapper over the shared `evidence_reuse.assess_evidence_reuse`
    (phase "apply") — revalidate uses the same helper with its own phase.
    """
    return assess_evidence_reuse(store, job_id, proposal, candidate_dir, phase="apply")


def _complete_applied_application(
    store: JobStore,
    job_id: str,
    proposal_id: str,
    *,
    changed_files: list[str] | None,
    allow_legacy: bool = False,
) -> _ApplicationOutcome:
    proposal = store.load_proposal(job_id, proposal_id)
    candidate_dir = _candidate_dir_from_proposal(store, job_id, proposal)
    # A propose-time candidate is a full workspace snapshot and promotion is a
    # wholesale replace — promoting a candidate whose base is no longer the
    # active revision silently reverts every apply that landed in between.
    # active == candidate revision is allowed: that is a crash-resume after the
    # promotion itself completed, where finishing the bookkeeping is correct.
    base_revision = str(proposal.get("base_revision") or "")
    candidate_revision = str(
        (proposal.get("candidate_report") or {}).get("revision") or ""
    )
    active_revision = compute_workspace_revision(store.job_dir(job_id))
    if base_revision and active_revision not in (base_revision, candidate_revision):
        final_error = (
            f"baseline drift: candidate was staged against revision {base_revision} "
            f"but the active workspace is now {active_revision} (moved by an "
            "intervening apply). Promoting this candidate would revert those "
            "changes. Approval carried over — the agent re-stages the change "
            "against the current workspace and the apply re-queues automatically."
        )
        store.append_journal(
            job_id,
            {
                "type": "stale_baseline_promotion_refused",
                "proposal_id": proposal_id,
                "base_revision": base_revision,
                "active_revision": active_revision,
                "candidate_revision": candidate_revision,
                "restage_requested": True,
            },
        )
        _write_apply_report(
            store,
            job_id,
            proposal_id,
            status="red",
            summary=f"Apply deferred: {final_error}",
            changed_files=changed_files or [],
            validation={"status": "failed", "checks": [], "error": final_error},
            error=final_error,
        )
        return _ApplicationOutcome(
            final_status="failed",
            final_error=final_error,
            deterministic_validation={
                "status": "failed",
                "checks": [],
                "error": final_error,
            },
            restage_requested=True,
        )
    # Evidence reuse: the propose flow already ran the full validation
    # (candidate backtest + preflight + execution validation) against this
    # exact candidate content. When the candidate, dataset, and baseline are
    # PROVABLY unchanged and the frozen evidence is green, re-running the
    # ~30-minute backtest with trading loops paused adds nothing — keep only
    # the cheap invariants (compile, scenario sims, config/report reads).
    reuse = assess_validation_reuse(store, job_id, proposal, candidate_dir)
    maintenance = (proposal.get("candidate_report") or {}).get("maintenance") or {}
    if maintenance.get("ready") is True and not reuse["eligible"]:
        final_error = (
            "behavior-equivalence proof is no longer reusable "
            f"({reuse['reason']}); stage the maintenance change fresh"
        )
        store.append_journal(
            job_id,
            {
                "type": "maintenance_apply_refused_stale_proof",
                "proposal_id": proposal_id,
                "reason": reuse["reason"],
                **({"details": reuse["proof"]} if reuse["proof"] else {}),
            },
        )
        _write_apply_report(
            store,
            job_id,
            proposal_id,
            status="red",
            summary=f"Apply refused: {final_error}",
            changed_files=changed_files or [],
            validation={"status": "failed", "checks": [], "error": final_error},
            error=final_error,
        )
        return _ApplicationOutcome(
            final_status="failed",
            final_error=final_error,
            deterministic_validation={
                "status": "failed",
                "checks": [],
                "error": final_error,
            },
        )
    if reuse["reason"] == "dataset_stale":
        # Freshness bound (live-capable only): the frozen evidence and the
        # on-disk dataset are provably identical, but the bars are older
        # than the owner's evidence ceiling. Reusing would promote on stale
        # evidence; blindly re-running the full validation would VALIDATE
        # against the same stale bars — equally worthless for a job that
        # trades live. Refuse both and route to refresh + revalidate.
        proof = reuse["proof"]
        final_error = (
            "dataset stale — refresh and revalidate: the candidate's dataset "
            f"was fetched {proof.get('age_hours')}h ago (max "
            f"{proof.get('max_age_hours')}h for a live-capable job). Re-fetch "
            "the dataset, revalidate the proposal, and re-apply."
        )
        store.append_journal(
            job_id,
            {
                "type": "apply_refused_stale_dataset",
                "proposal_id": proposal_id,
                **proof,
            },
        )
        _write_apply_report(
            store,
            job_id,
            proposal_id,
            status="red",
            summary=f"Apply refused: {final_error}",
            changed_files=changed_files or [],
            validation={"status": "failed", "checks": [], "error": final_error},
            error=final_error,
        )
        return _ApplicationOutcome(
            final_status="failed",
            final_error=final_error,
            deterministic_validation={
                "status": "failed",
                "checks": [],
                "error": final_error,
            },
        )
    deterministic_validation: dict[str, Any] | None = None
    if reuse["eligible"]:
        cheap_validation = validate_candidate_application(
            repo_root=store.repo_root,
            job_dir=store.job_dir(job_id),
            proposal=proposal,
            candidate_dir=candidate_dir,
            require_judge=bool(proposal.get("judge_required")),
            allow_legacy=allow_legacy,
            skip_behavior_checks=True,
        )
        cheap_validation = _with_execution_validation(
            store,
            job_id,
            proposal,
            cheap_validation,
        )
        if cheap_validation["status"] == "passed":
            report = proposal.get("candidate_report") or {}
            deterministic_validation = {
                **cheap_validation,
                "status": "reused",
                "source": "propose-time report",
                "reused_summary": dict(report.get("validation_summary") or {}),
                "reuse_proof": reuse["proof"],
            }
            store.append_journal(
                job_id,
                {
                    "type": "apply_validation_reused",
                    "proposal_id": proposal_id,
                    "source": "propose-time report",
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
                    "type": "apply_validation_rerun",
                    "proposal_id": proposal_id,
                    "reason": "cheap_invariants_failed",
                },
            )
    else:
        store.append_journal(
            job_id,
            {
                "type": "apply_validation_rerun",
                "proposal_id": proposal_id,
                "reason": reuse["reason"],
                **({"details": reuse["proof"]} if reuse["proof"] else {}),
            },
        )
    if deterministic_validation is None:
        deterministic_validation = validate_candidate_application(
            repo_root=store.repo_root,
            job_dir=store.job_dir(job_id),
            proposal=proposal,
            candidate_dir=candidate_dir,
            require_judge=bool(proposal.get("judge_required")),
            allow_legacy=allow_legacy,
        )
        deterministic_validation = _with_execution_validation(
            store,
            job_id,
            proposal,
            deterministic_validation,
        )
    store.record_proposal_application_validation(
        job_id,
        proposal_id,
        deterministic_validation,
    )
    if deterministic_validation["status"] not in ("passed", "reused"):
        final_error = "Candidate validation failed: " + json.dumps(
            validation_summary(deterministic_validation), sort_keys=True, default=str
        )
        _write_apply_report(
            store,
            job_id,
            proposal_id,
            status="red",
            summary=f"Failed to apply approved proposal: {final_error}",
            changed_files=changed_files or [],
            validation=deterministic_validation,
            error=final_error,
        )
        return _ApplicationOutcome(
            final_status="failed",
            final_error=final_error,
            deterministic_validation=deterministic_validation,
        )

    root = store.job_dir(job_id)
    backup_dir = root / "applications" / proposal_id / "backup"
    active_workspace = root / "workspace"
    # Rebuild the backup only while the active workspace exists. After a crash
    # mid-promotion the active workspace may be gone and the existing backup is
    # the ONLY copy of the pre-apply state — rebuilding from the torn tree
    # would destroy it and leave rollback with nothing to restore.
    if backup_dir.exists() and active_workspace.exists():
        shutil.rmtree(backup_dir)
    if not backup_dir.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        if active_workspace.exists():
            shutil.copytree(active_workspace, backup_dir / "workspace")
        shutil.copy2(root / "job.yaml", backup_dir / "job.yaml")
        if (root / IMPROVER_FILENAME).exists():
            shutil.copy2(root / IMPROVER_FILENAME, backup_dir / IMPROVER_FILENAME)

    outcome = _ApplicationOutcome(
        final_status="applied",
        deterministic_validation=deterministic_validation,
    )
    post_apply_gate: dict[str, Any] | None = None
    try:
        _promote_candidate(store, job_id, candidate_dir)
        if proposal.get("kind") == "improver_change":
            _apply_improver_change(store, job_id, proposal)
        job = store.load(job_id)
        outcome.promoted_revision = _record_promoted_revision(
            store,
            job_id,
            proposal_id,
            changed_files=changed_files,
            validation=deterministic_validation,
        )
        job.versioning["active_revision"] = outcome.promoted_revision
        store.save(job)
        job = store.load(job_id)
        outcome.compile_result = JobCompiler(store=store).compile(job)
        # Observability check, not a rollback path: the candidate just passed
        # backtest+preflight+validation at this exact revision, so a red gate
        # here means artifact stamping broke — surface it in the apply report.
        post_apply_gate = evaluate_live_gate(job_id, store=store)
        sync_all_jobs(store=store)
    except Exception as exc:
        active_workspace = root / "workspace"
        if active_workspace.exists():
            shutil.rmtree(active_workspace)
        if (backup_dir / "workspace").exists():
            shutil.copytree(backup_dir / "workspace", active_workspace)
        shutil.copy2(backup_dir / "job.yaml", root / "job.yaml")
        if (backup_dir / IMPROVER_FILENAME).exists():
            shutil.copy2(backup_dir / IMPROVER_FILENAME, root / IMPROVER_FILENAME)
        elif (root / IMPROVER_FILENAME).exists():
            # This apply created the file (job previously ran on defaults) —
            # restoring the pre-apply state means removing it.
            (root / IMPROVER_FILENAME).unlink()
        outcome.rollback = {
            "restored": True,
            "backup_dir": str(backup_dir.relative_to(store.repo_root)),
        }
        outcome.final_status = "failed"
        outcome.final_error = str(exc)

    if outcome.final_status == "applied":
        _write_apply_report(
            store,
            job_id,
            proposal_id,
            status="green",
            summary="Applied approved proposal after deterministic validation.",
            changed_files=changed_files or [],
            validation=deterministic_validation,
            promoted_revision=outcome.promoted_revision,
            compile_result=outcome.compile_result,
            live_gate=post_apply_gate,
        )
    else:
        _write_apply_report(
            store,
            job_id,
            proposal_id,
            status="red",
            summary=f"Failed to apply approved proposal: {outcome.final_error}",
            changed_files=changed_files or [],
            validation=deterministic_validation,
            promoted_revision=outcome.promoted_revision,
            compile_result=outcome.compile_result,
            error=outcome.final_error,
            rollback=outcome.rollback,
        )
    return outcome


def _apply_improver_change(store: JobStore, job_id: str, proposal: dict) -> None:
    """Write the approved search-policy spec to improver.yaml — the ONLY
    write path for the file. Refuses when the spec moved since propose time
    (same stale-baseline semantics as workspace promotion; the raise lands in
    the rollback path)."""
    import yaml

    root = store.job_dir(job_id)
    payload = (proposal.get("proposed_change") or {}).get("improver")
    if not isinstance(payload, dict) or not payload:
        raise ValueError(
            "improver_change proposal carries no proposed_change.improver spec"
        )
    base = str(proposal.get("base_improver_revision") or "")
    current = improver_revision(root)
    if base and current != base:
        raise ValueError(
            f"improver spec drift: proposal was staged against revision {base} "
            f"but the active spec is now {current} — re-propose against the "
            "current spec"
        )
    (root / IMPROVER_FILENAME).write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    store.append_journal(
        job_id,
        {
            "type": "improver_spec_applied",
            "proposal_id": proposal.get("proposal_id"),
            "previous_improver_revision": current,
            "applied_improver_revision": improver_revision(root),
        },
    )


def _apply_runner_action(
    bridge: RunnerBridge, job: Any, action: str
) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    runner_action = getattr(bridge, action)
    for loop_name, loop in (("script", job.script_loop), ("agent", job.agent_loop)):
        if not (loop.enabled and loop.runner_job_name):
            continue
        try:
            response = runner_action(loop.runner_job_name)
        except Exception as exc:
            response = {"ok": False, "error": str(exc), "name": loop.runner_job_name}
        responses.append(
            {
                "loop": loop_name,
                "runner_job_name": loop.runner_job_name,
                "response": response,
            }
        )
    return responses


def _prepare_candidate_workspace(
    store: JobStore,
    job_id: str,
    proposal_id: str,
    *,
    proposal: dict[str, Any] | None = None,
    force_fresh: bool = False,
) -> dict[str, Any]:
    root = store.job_dir(job_id)
    candidate_dir = root / "applications" / proposal_id / "candidate"
    workspace_dst = candidate_dir / "workspace"
    descriptor = {
        "candidate_workspace": str(workspace_dst.relative_to(store.repo_root)),
        "candidate_job_yaml": str(
            (candidate_dir / "job.yaml").relative_to(store.repo_root)
        ),
        "candidate_dir": str(candidate_dir.relative_to(store.repo_root)),
    }
    if candidate_dir.exists():
        if not force_fresh:
            # Reuse a propose-time candidate: it carries the actual proposed
            # change, and recopying the active workspace over it would destroy
            # that change. Reuse only when the candidate still hashes to the
            # revision its candidate_report recorded (hand-edits/corruption
            # fall back to a fresh copy — the legacy prose-driven apply path).
            report = (proposal or {}).get("candidate_report") or {}
            recorded = str(report.get("revision") or "")
            if recorded and recorded == compute_workspace_revision(candidate_dir):
                store.append_journal(
                    job_id,
                    {
                        "type": "candidate_reused",
                        "proposal_id": proposal_id,
                        "revision": recorded,
                    },
                )
                base_revision = str((proposal or {}).get("base_revision") or "")
                active_revision = compute_workspace_revision(root)
                if base_revision and base_revision != active_revision:
                    # Active workspace moved since propose. The candidate is
                    # self-contained and complete_application re-validates it
                    # authoritatively, so reuse is safe — but record the drift
                    # so a reviewer can decide to re-propose.
                    store.append_journal(
                        job_id,
                        {
                            "type": "candidate_baseline_drift",
                            "proposal_id": proposal_id,
                            "base_revision": base_revision,
                            "active_revision": active_revision,
                        },
                    )
                    descriptor["stale_baseline"] = True
                return descriptor
            if recorded:
                # Candidate on disk no longer matches its report revision — it
                # was hand-edited or corrupted after propose (the D2 apply-drift
                # failure). Falling back to a fresh copy of the active workspace
                # DROPS the candidate's change, so record why it vanished rather
                # than recopying silently. (approve_proposal now rejects this
                # case up front; this journal is defensive for other callers.)
                store.append_journal(
                    job_id,
                    {
                        "type": "candidate_report_stale",
                        "proposal_id": proposal_id,
                        "recorded_revision": recorded,
                        "candidate_revision": compute_workspace_revision(candidate_dir),
                    },
                )
        shutil.rmtree(candidate_dir)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    workspace_src = root / "workspace"
    if workspace_src.exists():
        shutil.copytree(workspace_src, workspace_dst)
    else:
        workspace_dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "job.yaml", candidate_dir / "job.yaml")
    return descriptor


def _candidate_dir_from_proposal(
    store: JobStore, job_id: str, proposal: dict[str, Any]
) -> Path:
    candidate_dir = proposal["application"].get("candidate_dir")
    if candidate_dir:
        path = store.repo_root / str(candidate_dir)
        if path.exists():
            return path
    return (
        store.job_dir(job_id)
        / "applications"
        / str(proposal["proposal_id"])
        / "candidate"
    )


# Operator-owned execution_params preserved across candidate promotion. Only
# keys that compute_workspace_revision also excludes belong here — restoring
# a hashed key would make the promoted revision diverge from the candidate
# revision and orphan the candidate's gate stamps.
_OPERATOR_OWNED_EXECUTION_PARAMS = ("wallet_label", "initial_capital")


def _snapshot_operator_owned(job_yaml: Path) -> dict[str, Any]:
    """Operator dials captured from the ACTIVE job.yaml before promotion.

    The candidate's job.yaml is a snapshot from propose time; copying it
    wholesale over the root reverted any watch-mode (agent set-mode — the FE
    "Just run it"/"Watch & suggest" selector), paper/live, or wallet change
    the operator made between propose and apply. Every preserved field is
    excluded from the workspace revision hash, so restoring them keeps the
    promoted revision equal to the candidate revision and the candidate's
    gate stamps valid."""
    try:
        data = yaml.safe_load(job_yaml.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    snapshot: dict[str, Any] = {}
    if isinstance(data.get("agent_loop"), dict):
        snapshot["agent_loop"] = data["agent_loop"]
    if data.get("job_kind"):
        snapshot["job_kind"] = data["job_kind"]
    script_loop = data.get("script_loop")
    if isinstance(script_loop, dict) and "mode" in script_loop:
        snapshot["script_mode"] = script_loop["mode"]
    params = data.get("execution_params")
    if isinstance(params, dict):
        snapshot["execution_params"] = {
            key: params[key]
            for key in _OPERATOR_OWNED_EXECUTION_PARAMS
            if key in params
        }
    return snapshot


def _restore_operator_owned(job_yaml: Path, snapshot: dict[str, Any]) -> None:
    if not snapshot:
        return
    data = yaml.safe_load(job_yaml.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return
    if "agent_loop" in snapshot:
        data["agent_loop"] = snapshot["agent_loop"]
    if "job_kind" in snapshot:
        data["job_kind"] = snapshot["job_kind"]
    if "script_mode" in snapshot and isinstance(data.get("script_loop"), dict):
        data["script_loop"]["mode"] = snapshot["script_mode"]
    preserved_params = snapshot.get("execution_params") or {}
    if preserved_params and isinstance(data.get("execution_params"), dict):
        data["execution_params"].update(preserved_params)
    job_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _promote_candidate(store: JobStore, job_id: str, candidate_dir: Path) -> None:
    root = store.job_dir(job_id)
    candidate_workspace = candidate_dir / "workspace"
    candidate_job_yaml = candidate_dir / "job.yaml"
    if not candidate_workspace.exists():
        raise FileNotFoundError(f"candidate workspace missing: {candidate_workspace}")
    if not candidate_job_yaml.exists():
        raise FileNotFoundError(f"candidate job.yaml missing: {candidate_job_yaml}")
    operator_owned = _snapshot_operator_owned(root / "job.yaml")
    active_workspace = root / "workspace"
    if active_workspace.exists():
        shutil.rmtree(active_workspace)
    shutil.copytree(candidate_workspace, active_workspace)
    shutil.copy2(candidate_job_yaml, root / "job.yaml")
    _restore_operator_owned(root / "job.yaml", operator_owned)
    # Carry the candidate's revision-stamped gate artifacts (written during
    # candidate validation) into the job dirs: candidate revision equals the
    # post-promotion revision, so these keep evaluate_live_gate green after
    # the apply instead of leaving stale-revision reports behind. The
    # candidate's grids/experiments/sandboxes are deliberately NOT copied.
    for relative in (
        Path("results") / "backtest" / "latest.json",
        Path("results") / "backtest" / "visualization.json",
        Path("results") / "backtest" / "trade_forensics.json",
        Path("reports") / "preflight" / "latest.json",
        Path("reports") / "validation" / "latest.json",
    ):
        source = candidate_dir / relative
        if source.exists():
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def apply_candidate_bundle(
    store: JobStore, job_id: str, candidate_dir: Path, *, label: str
) -> dict[str, Any]:
    """Bench/lifecycle apply path under a declared owner rule: promotes a
    candidate bundle and stamps its revision, skipping proposal loading, the
    baseline-drift check, validation reuse, the live gate, sync, and compile."""
    candidate_revision = compute_workspace_revision(candidate_dir)
    root = store.job_dir(job_id)
    backup_dir = root / "applications" / label / "backup"
    active_workspace = root / "workspace"
    if backup_dir.exists() and active_workspace.exists():
        shutil.rmtree(backup_dir)
    if not backup_dir.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        if active_workspace.exists():
            shutil.copytree(active_workspace, backup_dir / "workspace")
        shutil.copy2(root / "job.yaml", backup_dir / "job.yaml")
        if (root / IMPROVER_FILENAME).exists():
            shutil.copy2(root / IMPROVER_FILENAME, backup_dir / IMPROVER_FILENAME)
    _promote_candidate(store, job_id, candidate_dir)
    promoted_revision = _record_promoted_revision(
        store,
        job_id,
        label,
        changed_files=None,
        validation={"status": "reused", "source": label},
    )
    job = store.load(job_id)
    job.versioning["active_revision"] = promoted_revision
    store.save(job)
    return {
        "promoted_revision": promoted_revision,
        "candidate_revision": candidate_revision,
        "backup_dir": str(backup_dir),
    }


def rollback_application(
    store: JobStore, job_id: str, proposal_id: str, *, by: str = "owner"
) -> dict[str, Any]:
    """Owner undo for an APPLIED proposal: restore the pre-apply snapshot.

    Reuses the promotion backup `_complete_applied_application` writes to
    ``applications/<pid>/backup`` (workspace + job.yaml + improver.yaml) —
    the same restore the in-flight failure path runs. Guarded: only the
    proposal whose promoted revision is still the ACTIVE revision can be
    rolled back; restoring an older backup would silently clobber
    intervening applies (the stale-candidate promotion incident class)."""
    proposal = store.load_proposal(job_id, proposal_id)
    application = proposal["application"]
    if application.get("status") != "applied":
        raise ValueError(
            f"only applied proposals can be rolled back: {proposal_id} is "
            f"{application.get('status')}"
        )
    root = store.job_dir(job_id)
    backup_dir = root / "applications" / proposal_id / "backup"
    if not (backup_dir / "job.yaml").exists():
        raise ValueError(f"no promotion backup exists for {proposal_id}")
    promoted = str(application.get("promoted_revision") or "")
    current = compute_workspace_revision(root)
    if promoted and current != promoted:
        raise ValueError(
            "workspace has moved since this apply (promoted "
            f"{promoted[:12]}, active {current[:12]}) — rolling back would "
            "clobber intervening work; undo the newer applies first"
        )
    active_workspace = root / "workspace"
    if active_workspace.exists():
        shutil.rmtree(active_workspace)
    if (backup_dir / "workspace").exists():
        shutil.copytree(backup_dir / "workspace", active_workspace)
    shutil.copy2(backup_dir / "job.yaml", root / "job.yaml")
    if (backup_dir / IMPROVER_FILENAME).exists():
        shutil.copy2(backup_dir / IMPROVER_FILENAME, root / IMPROVER_FILENAME)
    job = store.load(job_id)
    compile_result = JobCompiler(store=store).compile(job)
    restored_revision = compute_workspace_revision(root)
    application["rollback"] = {
        "restored": True,
        "backup_dir": str(backup_dir.relative_to(store.repo_root)),
        "by": by,
        "ts": utc_now_iso(),
        "restored_revision": restored_revision,
    }
    proposal["updated_at"] = utc_now_iso()
    store.write_proposal(job_id, proposal)
    store.append_journal(
        job_id,
        {
            "type": "application_rolled_back",
            "proposal_id": proposal_id,
            "by": by,
            "rolled_back_revision": promoted or None,
            "restored_revision": restored_revision,
        },
    )
    store.refresh_scorecard(job_id)
    sync_all_jobs(store=store)
    return {
        "job_id": job_id,
        "proposal_id": proposal_id,
        "restored_revision": restored_revision,
        "compile": compile_result,
    }


def _record_promoted_revision(
    store: JobStore,
    job_id: str,
    proposal_id: str,
    *,
    changed_files: list[str] | None,
    validation: dict[str, Any] | None,
) -> str:
    root = store.job_dir(job_id)
    revision = compute_workspace_revision(root)
    validation_status = validation["status"] if validation else None
    active = {
        "job_id": job_id,
        "active_revision": revision,
        "active_label": f"proposal/{proposal_id}",
        "proposal_id": proposal_id,
    }
    store.write_json(job_id, "versions/active.json", active)
    store.append_journal(
        job_id,
        {
            "type": "proposal_promoted",
            "proposal_id": proposal_id,
            "revision": revision,
            "changed_files": changed_files or [],
            "validation_status": validation_status,
        },
    )
    try:
        from wayfinder_paths.jobs.archive import set_incumbent

        # Content id first; set_incumbent's resolver falls back to the
        # proposal UUID for legacy entries recorded before content ids.
        set_incumbent(store, job_id, f"cand-{revision}" if revision else proposal_id)
    except Exception:  # noqa: BLE001 — archive bookkeeping never breaks promotion
        pass
    revisions_path = root / "versions" / "revisions.jsonl"
    revisions_path.parent.mkdir(parents=True, exist_ok=True)
    revisions_path.open("a", encoding="utf-8").write(
        json.dumps(
            {
                "ts": utc_now_iso(),
                "revision": revision,
                "proposal_id": proposal_id,
                "changed_files": changed_files or [],
                "validation_status": validation_status,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return revision


def _write_apply_report(
    store: JobStore,
    job_id: str,
    proposal_id: str,
    *,
    status: str,
    summary: str,
    changed_files: list[str],
    validation: dict[str, Any] | None = None,
    promoted_revision: str | None = None,
    compile_result: dict[str, Any] | None = None,
    error: str | None = None,
    rollback: dict[str, Any] | None = None,
    live_gate: dict[str, Any] | None = None,
) -> None:
    payload = {
        "job_id": job_id,
        "mode": "apply",
        "status": status,
        "apply_proposal_id": proposal_id,
        "summary": summary,
        "changed_files": changed_files,
        "validation": validation or {},
        "promoted_revision": promoted_revision,
        "compile": compile_result,
        "error": error,
        "rollback": rollback,
        "live_gate": live_gate,
    }
    store.write_json(job_id, "reports/apply/latest.json", payload)


def validate_candidate_bundle(
    store: JobStore,
    job_id: str,
    proposal: dict[str, Any],
    candidate_dir: Path,
    *,
    require_judge: bool = False,
    allow_legacy: bool = False,
    skip_behavior_checks: bool = False,
) -> dict[str, Any]:
    """The full candidate validation (deterministic checks + execution
    validation + revision-stamped artifact persistence), independent of
    application status — shared by the apply flow and the propose flow.
    `skip_behavior_checks` follows the `validate_candidate_application`
    contract: cheap invariants only, set exclusively when the propose-time
    behavioral evidence is provably reusable (`assess_evidence_reuse`)."""
    validation = validate_candidate_application(
        repo_root=store.repo_root,
        job_dir=store.job_dir(job_id),
        proposal=proposal,
        candidate_dir=candidate_dir,
        require_judge=require_judge,
        allow_legacy=allow_legacy,
        skip_behavior_checks=skip_behavior_checks,
    )
    return _with_execution_validation(
        store, job_id, proposal, validation, candidate_dir=candidate_dir
    )


def _with_execution_validation(
    store: JobStore,
    job_id: str,
    proposal: dict[str, Any],
    validation: dict[str, Any],
    *,
    candidate_dir: Path | None = None,
) -> dict[str, Any]:
    if candidate_dir is None:
        candidate_dir = _candidate_dir_from_proposal(store, job_id, proposal)
    has_spec = (candidate_dir / "execution_spec.json").exists()
    candidate_job_yaml = candidate_dir / "job.yaml"
    if candidate_job_yaml.exists():
        try:
            job_data = (
                yaml.safe_load(candidate_job_yaml.read_text(encoding="utf-8")) or {}
            )
            match job_data:
                case dict() if job_data.get("execution_spec"):
                    has_spec = True
        except Exception:
            pass
    if not has_spec:
        return validation
    execution_validation = validate_execution_job(
        job_id,
        candidate_dir=candidate_dir,
        store=store,
    )
    # Persist inside the candidate bundle: promotion copies it to the job's
    # reports/ so the live gate sees a validation report stamped at the
    # promoted (== candidate) revision.
    validation_path = candidate_dir / "reports" / "validation" / "latest.json"
    validation_path.parent.mkdir(parents=True, exist_ok=True)
    validation_path.write_text(
        json.dumps(execution_validation, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    execution_passed = execution_validation["status"] == "passed"
    checks = list(validation["checks"])
    checks.append(
        {
            "name": "execution_candidate_validation",
            "passed": execution_passed,
            "details": execution_validation,
        }
    )
    return {
        **validation,
        "status": "passed"
        if validation["status"] == "passed" and execution_passed
        else "failed",
        "checks": checks,
        "execution_validation": execution_validation,
    }
