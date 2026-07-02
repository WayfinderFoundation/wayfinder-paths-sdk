from __future__ import annotations

from typing import Any, Literal

from wayfinder_paths.jobs.application import (
    claim_application,
    complete_application,
    ensure_jobs_v1_contract,
    validate_application_candidate,
)
from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.execution.job import backtest_execution_job, validate_job
from wayfinder_paths.jobs.models import (
    WayfinderJob,
    infer_job_kind,
    normalize_agent_mode,
)
from wayfinder_paths.jobs.runner_bridge import RunnerBridge
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import snapshot_job, sync_all_jobs
from wayfinder_paths.jobs.worker import run_job_worker
from wayfinder_paths.mcp.utils import catch_errors, err, ok

JobAction = Literal[
    "list",
    "create",
    "status",
    "report",
    "set_agent_mode",
    "review_now",
    "validate_job",
    "backtest_job",
    "proposals",
    "propose",
    "approve_proposal",
    "reject_proposal",
    "apply_proposal",
    "claim_application",
    "validate_application",
    "complete_application",
    "pause",
    "resume",
    "halt",
    "resume_from_halt",
    "delete",
    "sync",
]


@catch_errors
async def core_jobs(
    action: JobAction,
    *,
    job_id: str | None = None,
    name: str | None = None,
    goal: str | None = None,
    script: str | None = None,
    interval_seconds: int | None = None,
    cron_expr: str | None = None,
    timezone: str | None = None,
    timeout_seconds: int | None = None,
    agent_mode: Literal["off", "monitor", "intervene", "auto", "improve", "decide"]
    | None = None,
    agent_wake_seconds: int | None = None,
    auto_limits: dict[str, Any] | None = None,
    proposal_id: str | None = None,
    application_status: Literal["applied", "failed"] | None = None,
    changed_files: list[str] | None = None,
    validation: dict[str, Any] | None = None,
    error: str | None = None,
    reason: str | None = None,
    flatten: bool = False,
    kind: str | None = None,
    summary: str | None = None,
    intent_contract: dict[str, Any] | None = None,
    execution_params: dict[str, Any] | None = None,
    candidate_dir: str | None = None,
    scenario_plan: dict[str, Any] | None = None,
    memo: str | None = None,
    strict: bool = False,
    grid_path: str | None = None,
    workers: int = 1,
    parallel: Literal["serial", "thread", "process"] = "serial",
    compile: bool = True,  # noqa: A002
) -> dict[str, Any]:
    """Manage high-level Wayfinder jobs.

    A Wayfinder job is a versioned local job bundle with an optional deterministic
    script loop and optional headless OpenCode worker loop. This tool is the
    user-facing control layer; recurring execution is still delegated to
    `core_runner`.

    Typical flow:
      - `create` with `script` + `interval_seconds` for script-only jobs.
      - `create` with `agent_mode="monitor"` or `"intervene"` for supervised jobs.
      - `create` with `agent_mode="auto"` and `auto_limits` for agent-only auto jobs.
      - `review_now` to queue an immediate worker wakeup.
      - `approve_proposal` / `reject_proposal` after the worker creates proposals.
      - `claim_application` / `validate_application` / `complete_application`
        from an apply worker.
      - `validate_job` / `backtest_job` for execution-spec jobs.
    """

    store = JobStore()

    if action == "list":
        return ok([snapshot_job(job.id, store=store) for job in store.list_jobs()])

    if action == "sync":
        sync_all_jobs(store=store)
        return ok({"synced": True})

    if not job_id:
        return err("invalid_request", "job_id is required")

    if action == "create":
        mode = normalize_agent_mode(agent_mode)
        if not script and mode != "auto":
            return err(
                "invalid_request",
                "create requires script, or agent_mode auto for agent-only jobs",
            )
        if script and not interval_seconds and not cron_expr:
            return err(
                "invalid_request", "script jobs require interval_seconds or cron_expr"
            )
        job = WayfinderJob.new(
            job_id,
            name=name,
            goal=goal or "",
            script=script,
            interval_seconds=interval_seconds,
            cron_expr=cron_expr,
            timezone=timezone or "UTC",
            timeout_seconds=timeout_seconds or 120,
            agent_mode=mode,
            agent_wake_seconds=agent_wake_seconds,
            auto_limits=auto_limits,
        )
        job_path = store.save(job)
        result: dict[str, Any] = {"job": job.to_dict(), "job_yaml": str(job_path)}
        if compile:
            result["compile"] = JobCompiler(store=store).compile(job)
            sync_all_jobs(store=store)
        return ok(result)

    if action in {"status", "report"}:
        return ok(snapshot_job(job_id, store=store))

    if action == "set_agent_mode":
        mode = normalize_agent_mode(agent_mode or "monitor")
        job = store.load(job_id)
        job.agent_loop.mode = mode
        job.agent_loop.enabled = mode != "off"
        job.job_kind = infer_job_kind(job.script_loop.enabled, mode)
        if agent_wake_seconds is not None:
            job.agent_loop.wake_interval_seconds = agent_wake_seconds
        store.save(job)
        result = JobCompiler(store=store).compile(job)
        sync_all_jobs(store=store)
        return ok(result)

    if action == "review_now":
        mode = normalize_agent_mode(agent_mode or "monitor")
        if mode == "off":
            mode = "monitor"
        return ok(run_job_worker(job_id, mode=mode, apply_proposal_id=proposal_id))

    if action == "validate_job":
        return ok(validate_job(job_id, strict=strict, store=store))

    if action == "backtest_job":
        return ok(
            backtest_execution_job(
                job_id,
                grid_path=grid_path,
                workers=workers,
                parallel=parallel,
                store=store,
            )
        )

    if action == "proposals":
        return ok(store.proposals(job_id))

    if action == "propose":
        from wayfinder_paths.jobs.proposals import propose_change

        if not kind or not summary or not intent_contract:
            return err(
                "invalid_request",
                "propose requires kind, summary, and intent_contract",
            )
        return ok(
            propose_change(
                store,
                job_id,
                kind=kind,
                summary=summary,
                intent_contract=intent_contract,
                params=execution_params,
                candidate_source=candidate_dir,
                scenario_plan=scenario_plan,
                proposal_id=proposal_id,
                memo=memo,
            )
        )

    if action in {
        "approve_proposal",
        "reject_proposal",
        "apply_proposal",
        "claim_application",
        "validate_application",
        "complete_application",
    }:
        if not proposal_id:
            return err("invalid_request", "proposal_id is required")
        if action in {"approve_proposal", "apply_proposal"}:
            # Same gate as the CLI. MCP deliberately exposes no override, so
            # an agent cannot route a legacy job around the contract check.
            try:
                ensure_jobs_v1_contract(store, job_id)
            except ValueError as exc:
                return err("legacy_contract", str(exc))
            proposal = (
                store.approve_proposal(job_id, proposal_id)
                if action == "approve_proposal"
                else store.queue_proposal_application(job_id, proposal_id)
            )
            wakeup = run_job_worker(
                job_id, mode="intervene", apply_proposal_id=proposal_id
            )
            sync_all_jobs(store=store)
            return ok({"proposal": proposal, "wakeup": wakeup})
        if action == "reject_proposal":
            proposal = store.reject_proposal(job_id, proposal_id)
            sync_all_jobs(store=store)
            return ok(proposal)
        if action == "claim_application":
            return ok(claim_application(store, job_id, proposal_id))
        if action == "validate_application":
            return ok(validate_application_candidate(store, job_id, proposal_id))
        if action == "complete_application":
            if application_status not in {"applied", "failed"}:
                return err(
                    "invalid_request",
                    "application_status must be applied or failed",
                )
            return ok(
                complete_application(
                    store,
                    job_id,
                    proposal_id,
                    status=application_status,
                    changed_files=changed_files,
                    validation=validation,
                    error=error,
                )
            )

    if action in {"pause", "resume", "delete"}:
        job = store.load(job_id)
        bridge = RunnerBridge(repo_root=store.repo_root)
        runner_action = getattr(bridge, action)
        responses = [
            runner_action(loop.runner_job_name)
            for loop in (job.script_loop, job.agent_loop)
            if loop.runner_job_name
        ]
        sync_all_jobs(store=store)
        return ok(responses)

    if action == "halt":
        from wayfinder_paths.jobs.halt import request_halt

        payload = request_halt(
            store, job_id, reason=reason, flatten=bool(flatten), source="mcp"
        )
        sync_all_jobs(store=store)
        return ok(payload)

    if action == "resume_from_halt":
        from wayfinder_paths.jobs.halt import clear_halt

        payload = clear_halt(store, job_id)
        sync_all_jobs(store=store)
        return ok(payload)

    return err("invalid_request", f"unknown action: {action}")
