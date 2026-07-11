from __future__ import annotations

import asyncio
from typing import Any, Literal

from wayfinder_paths.jobs.application import (
    claim_application,
    complete_application,
    ensure_jobs_v1_contract,
    validate_application_candidate,
)
from wayfinder_paths.jobs.backtest_artifacts import diagnose_backtest
from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.execution.experiments import (
    list_experiments,
    promote_params,
    run_experiment,
)
from wayfinder_paths.jobs.execution.job import (
    backtest_execution_job,
    summarize_backtest_payload,
)
from wayfinder_paths.jobs.execution.preflight import build_live_dataset
from wayfinder_paths.jobs.execution.validation import validate_execution_job
from wayfinder_paths.jobs.halt import clear_halt, request_halt
from wayfinder_paths.jobs.models import (
    WayfinderJob,
    infer_job_kind,
    normalize_agent_mode,
)
from wayfinder_paths.jobs.proposals import propose_change
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
    "fetch_dataset",
    "backtest_job",
    "backtest_diagnose",
    "experiments",
    "promote_params",
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
    grid: dict[str, Any] | list[dict[str, Any]] | None = None,
    workers: int = 1,
    parallel: Literal["serial", "thread", "process"] = "serial",
    compile: bool = True,  # noqa: A002
    full: bool = False,
    quick_bars: int | None = None,
    days: int = 14,
    dataset_source: Literal["venues", "ccxt"] = "venues",
    exchange: str = "binance",
    market_type: Literal["swap", "spot"] = "swap",
    quote: str = "USDT",
    rank_by: str = "net_return",
    wf_test_bars: int | None = None,
    wf_train_bars: int | None = None,
    wf_folds: int = 3,
    grid_id: str | None = None,
    run_id: str | None = None,
    via_proposal: bool = False,
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
      - Strategy-development loop for execution-spec jobs: `fetch_dataset` (real
        candles into the job), `backtest_job` (use `quick_bars` while iterating),
        `backtest_diagnose` (ranked next steps), `experiments` (param grid via
        `grid` inline or `grid_path`; pass `wf_test_bars`/`wf_folds` for
        walk-forward out-of-sample validation), then `promote_params`
        (`grid_id`/`run_id`) once it survives OOS.
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
        return ok(validate_execution_job(job_id, strict=strict, store=store))

    if action == "fetch_dataset":
        # Network + disk bound and fully sync — off the MCP loop it goes.
        return ok(
            await asyncio.to_thread(
                build_live_dataset,
                job_id,
                days=days,
                store=store,
                source=dataset_source,
                exchange=exchange,
                market_type=market_type,
                quote=quote,
            )
        )

    if action == "experiments":
        chosen_grid = grid if grid is not None else grid_path
        if chosen_grid is None:
            return ok(list_experiments(job_id, store=store))
        walk_forward = None
        if wf_test_bars is not None:
            # No train_bars -> run_walk_forward's bounded rolling window (the
            # fast default); anchored/expanding stays CLI-only opt-in.
            walk_forward = {
                "test_bars": wf_test_bars,
                "train_bars": wf_train_bars,
                "folds": wf_folds,
                "anchored": False,
            }
        result = await asyncio.to_thread(
            run_experiment,
            job_id,
            chosen_grid,
            rank_by=rank_by,
            workers=workers,
            parallel=parallel,
            walk_forward=walk_forward,
            store=store,
        )
        backtest = result.get("backtest")
        if isinstance(backtest, dict) and not full:
            result["backtest"] = summarize_backtest_payload(backtest)
        return ok(result)

    if action == "promote_params":
        return ok(
            await asyncio.to_thread(
                promote_params,
                job_id,
                grid_id=grid_id,
                run_id=run_id,
                params=execution_params,
                via_proposal=via_proposal,
                store=store,
            )
        )

    if action == "backtest_job":
        # Run the (sync, CPU-bound) simulator in a worker thread. It guards
        # against running inside an event loop (simulator raises if
        # asyncio.get_running_loop() succeeds), so calling it inline here — in
        # this async tool — would fail; to_thread also keeps it from blocking
        # the MCP loop. Without this the agent has no working backtest tool and
        # falls back to fighting the CLI / raw Python.
        payload = await asyncio.to_thread(
            backtest_execution_job,
            job_id,
            grid_path=grid_path,
            workers=workers,
            parallel=parallel,
            quick_bars=quick_bars,
            store=store,
        )
        # Default to the compact summary (~2 KB) — the full payload is ~8 MB of
        # per-bar arrays already persisted to results/backtest/. Pass full=True
        # to return everything.
        return ok(payload if full else summarize_backtest_payload(payload))

    if action == "backtest_diagnose":
        return ok(diagnose_backtest(job_id, proposal_id=proposal_id, store=store))

    if action == "proposals":
        return ok(store.proposals(job_id))

    if action == "propose":
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
        payload = request_halt(
            store, job_id, reason=reason, flatten=bool(flatten), source="mcp"
        )
        sync_all_jobs(store=store)
        return ok(payload)

    if action == "resume_from_halt":
        payload = clear_halt(store, job_id)
        sync_all_jobs(store=store)
        return ok(payload)

    return err("invalid_request", f"unknown action: {action}")
