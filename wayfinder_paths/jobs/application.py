from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.execution.validation import validate_execution_job
from wayfinder_paths.jobs.gating import compute_workspace_revision, evaluate_live_gate
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.runner_bridge import RunnerBridge
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import sync_all_jobs
from wayfinder_paths.jobs.validation import (
    compact_json,
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
    proposal = store.load_proposal(job_id, proposal_id)
    application_status = proposal["application"]["status"]
    if proposal["status"] != "approved":
        raise ValueError(f"Proposal is not approved: {proposal_id}")
    if application_status not in {"queued", "failed"}:
        raise ValueError(
            f"Proposal application is not queued: {proposal_id} ({application_status})"
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
    sync_all_jobs(store=store)
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
    sync_all_jobs(store=store)
    return {
        "proposal": proposal,
        "compile": outcome.compile_result,
        "deterministic_validation": outcome.deterministic_validation,
        "promoted_revision": outcome.promoted_revision,
        "rollback": outcome.rollback,
        "resumed_runner_jobs": runner_responses,
    }


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
    if deterministic_validation["status"] != "passed":
        final_error = "Candidate validation failed: " + compact_json(
            validation_summary(deterministic_validation)
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

    backup_dir = _backup_active_workspace(store, job_id, proposal_id)
    outcome = _ApplicationOutcome(
        final_status="applied",
        deterministic_validation=deterministic_validation,
    )
    post_apply_gate: dict[str, Any] | None = None
    try:
        _promote_candidate(store, job_id, candidate_dir)
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
        outcome.rollback = _restore_active_workspace(store, job_id, backup_dir)
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


def _apply_runner_action(
    bridge: RunnerBridge, job: Any, action: str
) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    runner_action = getattr(bridge, action)
    for loop_name, loop in (("script", job.script_loop), ("agent", job.agent_loop)):
        if loop.enabled and loop.runner_job_name:
            responses.append(
                {
                    "loop": loop_name,
                    "runner_job_name": loop.runner_job_name,
                    "response": _safe_runner_call(runner_action, loop.runner_job_name),
                }
            )
    return responses


def _safe_runner_call(action: Any, name: str) -> dict[str, Any]:
    try:
        return action(name)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "name": name}


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
                        "candidate_revision": compute_workspace_revision(
                            candidate_dir
                        ),
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


def _backup_active_workspace(store: JobStore, job_id: str, proposal_id: str) -> Path:
    root = store.job_dir(job_id)
    backup_dir = root / "applications" / proposal_id / "backup"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    if (root / "workspace").exists():
        shutil.copytree(root / "workspace", backup_dir / "workspace")
    shutil.copy2(root / "job.yaml", backup_dir / "job.yaml")
    return backup_dir


def _promote_candidate(store: JobStore, job_id: str, candidate_dir: Path) -> None:
    root = store.job_dir(job_id)
    candidate_workspace = candidate_dir / "workspace"
    candidate_job_yaml = candidate_dir / "job.yaml"
    if not candidate_workspace.exists():
        raise FileNotFoundError(f"candidate workspace missing: {candidate_workspace}")
    if not candidate_job_yaml.exists():
        raise FileNotFoundError(f"candidate job.yaml missing: {candidate_job_yaml}")
    active_workspace = root / "workspace"
    if active_workspace.exists():
        shutil.rmtree(active_workspace)
    shutil.copytree(candidate_workspace, active_workspace)
    shutil.copy2(candidate_job_yaml, root / "job.yaml")
    # Carry the candidate's revision-stamped gate artifacts (written during
    # candidate validation) into the job dirs: candidate revision equals the
    # post-promotion revision, so these keep evaluate_live_gate green after
    # the apply instead of leaving stale-revision reports behind. The
    # candidate's grids/experiments/sandboxes are deliberately NOT copied.
    for relative in (
        Path("results") / "backtest" / "latest.json",
        Path("results") / "backtest" / "visualization.json",
        Path("reports") / "preflight" / "latest.json",
        Path("reports") / "validation" / "latest.json",
    ):
        source = candidate_dir / relative
        if source.exists():
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _restore_active_workspace(
    store: JobStore, job_id: str, backup_dir: Path
) -> dict[str, Any]:
    root = store.job_dir(job_id)
    active_workspace = root / "workspace"
    if active_workspace.exists():
        shutil.rmtree(active_workspace)
    if (backup_dir / "workspace").exists():
        shutil.copytree(backup_dir / "workspace", active_workspace)
    shutil.copy2(backup_dir / "job.yaml", root / "job.yaml")
    return {
        "restored": True,
        "backup_dir": str(backup_dir.relative_to(store.repo_root)),
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
            "validation_status": (validation or {}).get("status"),
        },
    )
    revisions_path = root / "versions" / "revisions.jsonl"
    revisions_path.parent.mkdir(parents=True, exist_ok=True)
    revisions_path.open("a", encoding="utf-8").write(
        json.dumps(
            {
                "ts": utc_now_iso(),
                "revision": revision,
                "proposal_id": proposal_id,
                "changed_files": changed_files or [],
                "validation_status": (validation or {}).get("status"),
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
) -> dict[str, Any]:
    """The full candidate validation (deterministic checks + execution
    validation + revision-stamped artifact persistence), independent of
    application status — shared by the apply flow and the propose flow."""
    validation = validate_candidate_application(
        repo_root=store.repo_root,
        job_dir=store.job_dir(job_id),
        proposal=proposal,
        candidate_dir=candidate_dir,
        require_judge=require_judge,
        allow_legacy=allow_legacy,
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
        json.dumps(execution_validation, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    checks = list(validation.get("checks") or [])
    checks.append(
        {
            "name": "execution_candidate_validation",
            "passed": execution_validation.get("status") == "passed",
            "details": execution_validation,
        }
    )
    return {
        **validation,
        "status": "passed"
        if validation.get("status") == "passed"
        and execution_validation.get("status") == "passed"
        else "failed",
        "checks": checks,
        "execution_validation": execution_validation,
    }
