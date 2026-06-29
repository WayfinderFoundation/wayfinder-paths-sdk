from __future__ import annotations

from typing import Any, Literal

from wayfinder_paths.jobs.compiler import JobCompiler
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
    "proposals",
    "approve_proposal",
    "reject_proposal",
    "pause",
    "resume",
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
    agent_mode: Literal["off", "monitor", "intervene", "auto", "improve", "decide"] | None = None,
    agent_wake_seconds: int | None = None,
    auto_limits: dict[str, Any] | None = None,
    proposal_id: str | None = None,
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
            return err("invalid_request", "script jobs require interval_seconds or cron_expr")
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
            job.agent_loop.wake_interval_seconds = int(agent_wake_seconds)
        store.save(job)
        result = JobCompiler(store=store).compile(job)
        sync_all_jobs(store=store)
        return ok(result)

    if action == "review_now":
        mode = normalize_agent_mode(agent_mode or "monitor")
        if mode == "off":
            mode = "monitor"
        return ok(run_job_worker(job_id, mode=mode))

    if action == "proposals":
        return ok(store.proposals(job_id))

    if action in {"approve_proposal", "reject_proposal"}:
        if not proposal_id:
            return err("invalid_request", "proposal_id is required")
        status = "approved" if action == "approve_proposal" else "rejected"
        proposal = store.set_proposal_status(job_id, proposal_id, status)
        store.append_journal(
            job_id,
            {"type": action, "proposal_id": proposal_id, "status": status},
        )
        sync_all_jobs(store=store)
        return ok(proposal)

    if action in {"pause", "resume", "delete"}:
        job = store.load(job_id)
        bridge = RunnerBridge(repo_root=store.repo_root)
        responses: list[dict[str, Any]] = []
        runner_action = getattr(bridge, action)
        if job.script_loop.runner_job_name:
            responses.append(runner_action(job.script_loop.runner_job_name))
        if job.agent_loop.runner_job_name:
            responses.append(runner_action(job.agent_loop.runner_job_name))
        sync_all_jobs(store=store)
        return ok(responses)

    return err("invalid_request", f"unknown action: {action}")
