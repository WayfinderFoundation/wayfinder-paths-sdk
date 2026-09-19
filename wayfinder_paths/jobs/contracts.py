"""Execution-contract dispatch for the launch lifecycle.

Three contracts enter the validate -> launch -> live-gate flow: ``jobs_v1``
(the SDK driver runs ``decide()``), ``freestyle_v1`` (the freestyle runtime
runs ``tick(ctx)``) and ``path_v1`` (an installed Path component). Each has
its own validation ladder and live-readiness rule; everything that reads
``reports/validation/latest.json`` or asks "may this go live" goes through
the two dispatchers here so the jobs_v1 path stays byte-identical.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.execution.validation import validate_execution_job
from wayfinder_paths.jobs.gating import evaluate_live_gate
from wayfinder_paths.jobs.models import LIFECYCLE_CONTRACTS, coerce_execution_contract
from wayfinder_paths.jobs.store import JobStore


def job_contract(root: Path) -> str:
    """The execution contract declared in a job (or candidate) directory."""
    path = Path(root) / "job.yaml"
    if not path.exists():
        return "legacy"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    match loaded:
        case dict():
            return coerce_execution_contract(loaded.get("execution_contract"))
        case _:
            return "legacy"


def is_lifecycle_contract(contract: str | None) -> bool:
    return str(contract or "legacy") in LIFECYCLE_CONTRACTS


def validate_job_for_kind(
    job_id: str,
    *,
    strict: bool = False,
    candidate_dir: str | Path | None = None,
    store: JobStore | None = None,
) -> dict[str, Any]:
    """Write ``reports/validation/latest.json`` (or return the candidate's
    report) using the ladder that matches the job's contract."""
    store = store or JobStore()
    root = Path(candidate_dir) if candidate_dir else store.job_dir(job_id)
    contract = job_contract(root)
    if contract == "freestyle_v1":
        from wayfinder_paths.jobs.freestyle.validate import validate_freestyle_job

        return validate_freestyle_job(job_id, candidate_dir=candidate_dir, store=store)
    if contract == "path_v1":
        from wayfinder_paths.jobs.paths_runtime import validate_path_job

        return validate_path_job(job_id, candidate_dir=candidate_dir, store=store)
    return validate_execution_job(
        job_id, strict=strict, candidate_dir=candidate_dir, store=store
    )


def evaluate_live_readiness(
    job_id: str, *, store: JobStore | None = None
) -> dict[str, Any]:
    """May this job trade live? jobs_v1 keeps ``evaluate_live_gate``
    unchanged; freestyle and path jobs answer through the launch checklist
    (validation at the current revision, a dry run, enough paper runs, risk
    limits, a wallet, and every warn-level risk flag acknowledged)."""
    store = store or JobStore()
    contract = job_contract(store.job_dir(job_id))
    if contract in {"freestyle_v1", "path_v1"}:
        from wayfinder_paths.jobs.launch import evaluate_launch_checklist

        checklist = evaluate_launch_checklist(job_id, store=store, target="live")
        return {
            "live_ready": bool(checklist["ready_live"]),
            "revision": checklist["revision"],
            "reasons": list(checklist["reasons"]),
            "checklist": checklist,
            "checked_at": checklist["checked_at"],
        }
    return evaluate_live_gate(job_id, store=store)
