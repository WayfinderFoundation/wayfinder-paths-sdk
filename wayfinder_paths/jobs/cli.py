from __future__ import annotations

import json
from typing import Any, Literal

import click

from wayfinder_paths.jobs.application import (
    claim_application,
    complete_application,
    validate_application_candidate,
)
from wayfinder_paths.jobs.backtest_artifacts import load_backtest_view
from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.execution.job import backtest_execution_job, validate_job
from wayfinder_paths.jobs.models import (
    AgentMode,
    WayfinderJob,
    infer_job_kind,
    normalize_agent_mode,
)
from wayfinder_paths.jobs.runner_bridge import RunnerBridge
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import snapshot_job, sync_all_jobs
from wayfinder_paths.jobs.worker import run_job_worker


def _echo_json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, default=str))


@click.group(
    name="job", help="High-level Wayfinder jobs: script loop + optional agent loop."
)
def job_cli() -> None:
    pass


@job_cli.command(name="create", help="Create or update a high-level Wayfinder job.")
@click.argument("job_id")
@click.option("--name", default=None)
@click.option("--goal", default="")
@click.option(
    "--script", default=None, help="Script entrypoint for the deterministic loop."
)
@click.option(
    "--execution-contract",
    "execution_contract",
    type=click.Choice(["jobs_v1", "legacy"]),
    default="jobs_v1",
    show_default=True,
    help=(
        "jobs_v1: SDK driver runs the strategy's decide() on schedule (script "
        "must expose build_strategy/decide, no trading main()). legacy: runpy "
        "the script's __main__ (existing free-form jobs only)."
    ),
)
@click.option("--interval", "interval_seconds", type=int, default=None)
@click.option("--cron", "cron_expr", default=None)
@click.option(
    "--initial-capital",
    "initial_capital",
    type=float,
    default=None,
    help="Starting capital in USD (execution_params.initial_capital) — the "
    "base for equity/return stats and compound sizing. Explicit beats the "
    "engine's hidden default.",
)
@click.option("--timezone", default="UTC", show_default=True)
@click.option("--timeout", "timeout_seconds", type=int, default=120, show_default=True)
@click.option(
    "--agent-mode",
    type=click.Choice(["off", "monitor", "intervene", "auto", "improve", "decide"]),
    default="off",
    show_default=True,
)
@click.option("--agent-wake", "agent_wake_seconds", type=int, default=None)
@click.option("--auto-venue", "auto_venues", multiple=True)
@click.option("--auto-symbol", "auto_symbols", multiple=True)
@click.option("--auto-market", "auto_markets", multiple=True)
@click.option("--max-notional", "max_notional_per_decision", type=float, default=None)
@click.option("--max-daily-notional", type=float, default=None)
@click.option("--max-open-positions", type=int, default=None)
@click.option("--max-open-orders", type=int, default=None)
@click.option("--no-compile", is_flag=True, default=False)
def create_cmd(
    job_id: str,
    name: str | None,
    goal: str,
    script: str | None,
    execution_contract: str,
    interval_seconds: int | None,
    cron_expr: str | None,
    initial_capital: float | None,
    timezone: str,
    timeout_seconds: int,
    agent_mode: AgentMode,
    agent_wake_seconds: int | None,
    auto_venues: tuple[str, ...],
    auto_symbols: tuple[str, ...],
    auto_markets: tuple[str, ...],
    max_notional_per_decision: float | None,
    max_daily_notional: float | None,
    max_open_positions: int | None,
    max_open_orders: int | None,
    no_compile: bool,
) -> None:
    normalized_mode = normalize_agent_mode(agent_mode)
    if not script and normalized_mode != "auto":
        raise click.UsageError(
            "Provide --script, or use --agent-mode auto for agent-only jobs"
        )
    if script and not interval_seconds and not cron_expr:
        raise click.UsageError("Script jobs require --interval or --cron")

    store = JobStore()
    job = WayfinderJob.new(
        job_id,
        name=name,
        goal=goal,
        script=script,
        execution_contract=(
            "jobs_v1" if script and execution_contract == "jobs_v1" else "legacy"
        ),
        interval_seconds=interval_seconds,
        cron_expr=cron_expr,
        timezone=timezone,
        timeout_seconds=timeout_seconds,
        agent_mode=normalized_mode,
        agent_wake_seconds=agent_wake_seconds,
        auto_limits=_auto_limits_from_options(
            venues=auto_venues,
            symbols=auto_symbols,
            markets=auto_markets,
            max_notional_per_decision=max_notional_per_decision,
            max_daily_notional=max_daily_notional,
            max_open_positions=max_open_positions,
            max_open_orders=max_open_orders,
        ),
    )
    if initial_capital is not None:
        job.execution_params["initial_capital"] = float(initial_capital)
    path = store.save(job)
    result: dict[str, Any] = {"job": job.to_dict(), "job_yaml": str(path)}
    if not no_compile:
        result["compile"] = JobCompiler(store=store).compile(job)
        sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": result})


@job_cli.command(name="list", help="List high-level Wayfinder jobs.")
def list_cmd() -> None:
    store = JobStore()
    _echo_json(
        {
            "ok": True,
            "result": [snapshot_job(job.id, store=store) for job in store.list_jobs()],
        }
    )


@job_cli.command(name="status", help="Show a high-level job snapshot.")
@click.argument("job_id")
def status_cmd(job_id: str) -> None:
    store = JobStore()
    _echo_json({"ok": True, "result": snapshot_job(job_id, store=store)})


@job_cli.command(
    name="validate", help="Validate a high-level job's execution contract."
)
@click.argument("job_id")
@click.option("--strict", is_flag=True, default=False)
def validate_cmd(job_id: str, strict: bool) -> None:
    store = JobStore()
    result = validate_job(job_id, strict=strict, store=store)
    _echo_json({"ok": result["status"] == "passed", "result": result})
    if strict and result["status"] != "passed":
        raise click.ClickException("job validation failed")


@job_cli.command(name="backtest", help="Run an execution-contract backtest for a job.")
@click.argument("job_id")
@click.option("--grid", "grid_path", default=None)
@click.option("--workers", type=int, default=1, show_default=True)
@click.option(
    "--parallel",
    type=click.Choice(["serial", "thread", "process"]),
    default="serial",
    show_default=True,
)
def backtest_cmd(
    job_id: str, grid_path: str | None, workers: int, parallel: str
) -> None:
    store = JobStore()
    result = backtest_execution_job(
        job_id,
        grid_path=grid_path,
        workers=workers,
        parallel=parallel,
        store=store,
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="migrate-contract",
    help="Flip a legacy job onto the jobs_v1 driver after validation passes.",
)
@click.argument("job_id")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Skip the validation gate (not recommended).",
)
def migrate_contract_cmd(job_id: str, force: bool) -> None:
    store = JobStore()
    job = store.load(job_id)
    if job.execution_contract == "jobs_v1":
        _echo_json({"ok": True, "result": {"already": "jobs_v1"}})
        return
    job.execution_contract = "jobs_v1"
    store.save(job)
    report = validate_job(job_id, store=store)
    if report["status"] != "passed" and not force:
        job.execution_contract = "legacy"
        store.save(job)
        _echo_json({"ok": False, "result": report})
        raise click.ClickException(
            "validation failed under jobs_v1; job left on legacy contract "
            "(fix the failures or use --force)"
        )
    compile_result = JobCompiler(store=store).compile(job)
    sync_all_jobs(store=store)
    _echo_json(
        {
            "ok": True,
            "result": {"validation": report, "compile": compile_result},
        }
    )


@job_cli.command(
    name="tick",
    help="Run one driver tick for a jobs_v1 job (debugging / manual runs).",
)
@click.argument("job_id")
@click.option(
    "--mode",
    type=click.Choice(["paper", "live"]),
    default=None,
    help="Override script_loop.mode for this tick.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Force paper brokers regardless of the job's configured mode.",
)
def tick_cmd(job_id: str, mode: str | None, dry_run: bool) -> None:
    import asyncio as _asyncio
    from pathlib import Path as _Path

    from wayfinder_paths.jobs.execution.driver import tick_job

    store = JobStore()
    job = store.load(job_id)
    if job.execution_contract != "jobs_v1":
        raise click.ClickException(
            "tick requires a jobs_v1 job; run migrate-contract first"
        )
    effective_mode = "paper" if dry_run else (mode or job.script_loop.mode or "paper")
    root = _Path(store.job_dir(job_id))
    payload = _asyncio.run(tick_job(job, root, effective_mode, store=store))
    _echo_json(payload)


@job_cli.command(
    name="experiments",
    help="Run a parameter-grid experiment (or list recorded experiments).",
)
@click.argument("job_id")
@click.option("--grid", "grid_path", default=None, help="Path to a grid JSON file.")
@click.option("--rank-by", default="net_return", show_default=True)
@click.option("--workers", type=int, default=1, show_default=True)
@click.option(
    "--parallel",
    type=click.Choice(["serial", "thread", "process"]),
    default="serial",
    show_default=True,
)
@click.option("--list", "list_only", is_flag=True, default=False)
@click.option(
    "--wf-test-bars",
    "wf_test_bars",
    type=int,
    default=None,
    help="Enable walk-forward: held-out test window size in bars per fold.",
)
@click.option("--wf-train-bars", "wf_train_bars", type=int, default=None)
@click.option("--wf-folds", "wf_folds", type=int, default=3, show_default=True)
@click.option(
    "--wf-warmup-bars", "wf_warmup_bars", type=int, default=60, show_default=True
)
@click.option("--wf-anchored", "wf_anchored", is_flag=True, default=False)
@click.option(
    "--optimizer",
    type=click.Choice(["grid", "optuna"]),
    default="grid",
    show_default=True,
    help="grid: exhaustive dict-of-lists. optuna: TPE search over a typed "
    "space (the --grid file doubles as the space; needs `poetry install "
    "--with ml`).",
)
@click.option(
    "--n-trials",
    "n_trials",
    type=int,
    default=50,
    show_default=True,
    help="Optuna trial count (ignored for --optimizer grid).",
)
@click.option(
    "--seed",
    type=int,
    default=42,
    show_default=True,
    help="Optuna sampler seed for reproducible searches.",
)
def experiments_cmd(
    job_id: str,
    grid_path: str | None,
    rank_by: str,
    workers: int,
    parallel: str,
    list_only: bool,
    wf_test_bars: int | None,
    wf_train_bars: int | None,
    wf_folds: int,
    wf_warmup_bars: int,
    wf_anchored: bool,
    optimizer: str,
    n_trials: int,
    seed: int,
) -> None:
    from wayfinder_paths.jobs.execution.experiments import (
        list_experiments,
        run_experiment,
    )

    store = JobStore()
    if list_only or not grid_path:
        _echo_json({"ok": True, "result": list_experiments(job_id, store=store)})
        return
    walk_forward = None
    if wf_test_bars is not None:
        walk_forward = {
            "test_bars": wf_test_bars,
            "train_bars": wf_train_bars,
            "folds": wf_folds,
            "warmup_bars": wf_warmup_bars,
            "anchored": wf_anchored or wf_train_bars is None,
        }
    optuna_options = (
        {"n_trials": n_trials, "seed": seed} if optimizer == "optuna" else None
    )
    result = run_experiment(
        job_id,
        grid_path,
        rank_by=rank_by,
        workers=workers,
        parallel=parallel,
        walk_forward=walk_forward,
        optimizer=optimizer,
        optuna_options=optuna_options,
        store=store,
    )
    wf_report = (result.get("backtest") or {}).get("walk_forward")
    if wf_report:
        from wayfinder_paths.jobs.execution.walk_forward import format_fold_table

        click.echo(format_fold_table(wf_report), err=True)
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="promote-params",
    help="Promote winning experiment params into the job (direct pre-live, or "
    "as a proposal for live jobs).",
)
@click.argument("job_id")
@click.option("--grid", "grid_id", default=None, help="Grid id under results/backtest/grids/.")
@click.option("--run", "run_id", default=None, help="Specific run id (default: best ranked).")
@click.option("--params", "params_json", default=None, help="Explicit params JSON.")
@click.option("--via-proposal", is_flag=True, default=False)
def promote_params_cmd(
    job_id: str,
    grid_id: str | None,
    run_id: str | None,
    params_json: str | None,
    via_proposal: bool,
) -> None:
    from wayfinder_paths.jobs.execution.experiments import promote_params

    store = JobStore()
    result = promote_params(
        job_id,
        grid_id=grid_id,
        run_id=run_id,
        params=json.loads(params_json) if params_json else None,
        via_proposal=via_proposal,
        store=store,
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="reconcile",
    help="Replay recorded ticks through the engine and diff decisions "
    "(live/backtest drift detection).",
)
@click.argument("job_id")
@click.option("--limit", type=int, default=200, show_default=True)
def reconcile_cmd(job_id: str, limit: int) -> None:
    from wayfinder_paths.jobs.execution.reconcile import reconcile_job

    store = JobStore()
    report = reconcile_job(job_id, store=store, limit=limit)
    _echo_json({"ok": True, "result": report})


@job_cli.command(
    name="fetch-dataset",
    help="Fetch real candles into input_bars.json — through the live venue "
    "feeds (default) or long-history CCXT data (backtests only).",
)
@click.argument("job_id")
@click.option("--days", type=int, default=14, show_default=True)
@click.option(
    "--source",
    type=click.Choice(["venues", "ccxt"]),
    default="venues",
    show_default=True,
)
@click.option("--exchange", default="binance", show_default=True)
@click.option(
    "--market-type",
    "market_type",
    type=click.Choice(["swap", "spot"]),
    default="swap",
    show_default=True,
)
@click.option("--quote", default="USDT", show_default=True)
def fetch_dataset_cmd(
    job_id: str,
    days: int,
    source: str,
    exchange: str,
    market_type: str,
    quote: str,
) -> None:
    from wayfinder_paths.jobs.execution.preflight import build_live_dataset

    store = JobStore()
    result = build_live_dataset(
        job_id,
        days=days,
        store=store,
        source=source,
        exchange=exchange,
        market_type=market_type,
        quote=quote,
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="preflight",
    help="Behavioral pre-live gate: drive the real driver over replayed data "
    "plus fault scenarios (stale feed, rejects, ambiguity, restart).",
)
@click.argument("job_id")
@click.option("--max-ticks", type=int, default=50, show_default=True)
def preflight_cmd(job_id: str, max_ticks: int) -> None:
    from wayfinder_paths.jobs.execution.preflight import run_preflight

    store = JobStore()
    report = run_preflight(job_id, store=store, max_ticks=max_ticks)
    _echo_json({"ok": report["status"] == "passed", "result": report})
    if report["status"] != "passed":
        raise click.ClickException("preflight failed")


@job_cli.command(
    name="gate",
    help="Evaluate the live gate (validation + backtest + preflight tied to "
    "the current revision).",
)
@click.argument("job_id")
def gate_cmd(job_id: str) -> None:
    from wayfinder_paths.jobs.gating import evaluate_live_gate

    store = JobStore()
    gate = evaluate_live_gate(job_id, store=store)
    _echo_json({"ok": gate["live_ready"], "result": gate})


@job_cli.command(
    name="backtest-view", help="Read a bounded backtest visualization payload."
)
@click.argument("job_id")
@click.option(
    "--view",
    type=click.Choice(["all", "legs", "spread", "equity", "drawdown", "performance"]),
    default="all",
    show_default=True,
)
@click.option("--series", "series_names", multiple=True)
@click.option("--from", "from_ts", default=None)
@click.option("--to", "to_ts", default=None)
@click.option("--max-points", type=int, default=1500, show_default=True)
def backtest_view_cmd(
    job_id: str,
    view: str,
    series_names: tuple[str, ...],
    from_ts: str | None,
    to_ts: str | None,
    max_points: int,
) -> None:
    store = JobStore()
    result = load_backtest_view(
        job_id,
        store=store,
        view=view,
        series_names=list(series_names),
        from_ts=from_ts,
        to_ts=to_ts,
        max_points=max_points,
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(name="report", help="Show a compact terminal report for a job.")
@click.argument("job_id")
def report_cmd(job_id: str) -> None:
    store = JobStore()
    snap = snapshot_job(job_id, store=store)
    job = snap["job"]
    scorecard = snap["scorecard"]
    proposals = snap["proposals"]
    click.echo(f"{job['name']} — {job['id']}")
    click.echo("")
    click.echo(f"Goal: {job['goal'] or 'not recorded'}")
    click.echo(f"Health: {scorecard.get('health', 'unknown')}")
    click.echo(f"Script loop: {'on' if job['script_loop']['enabled'] else 'off'}")
    click.echo(f"Agent loop: {job['agent_loop']['mode']}")
    click.echo(
        f"Pending proposals: {sum(1 for p in proposals if p['status'] == 'pending')}"
    )
    latest_summary = scorecard.get("last_agent_summary")
    if latest_summary:
        click.echo("")
        click.echo(f"Latest agent check: {latest_summary}")


@job_cli.group(name="agent", help="Control a job's agent loop.")
def agent_group() -> None:
    pass


@agent_group.command(name="set-mode", help="Set agent mode and recompile runner links.")
@click.argument("job_id")
@click.argument(
    "mode",
    type=click.Choice(["off", "monitor", "intervene", "auto", "improve", "decide"]),
)
@click.option("--wake", "wake_seconds", type=int, default=None)
def agent_set_mode_cmd(job_id: str, mode: AgentMode, wake_seconds: int | None) -> None:
    store = JobStore()
    job = store.load(job_id)
    normalized_mode = normalize_agent_mode(mode)
    job.agent_loop.mode = normalized_mode
    job.agent_loop.enabled = normalized_mode != "off"
    job.job_kind = infer_job_kind(job.script_loop.enabled, normalized_mode)
    if wake_seconds is not None:
        job.agent_loop.wake_interval_seconds = wake_seconds
    store.save(job)
    result = JobCompiler(store=store).compile(job)
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": result})


@agent_group.command(
    name="review-now", help="Run a headless worker review immediately."
)
@click.argument("job_id")
@click.option(
    "--mode",
    type=click.Choice(["monitor", "intervene", "auto", "improve", "decide"]),
    default=None,
)
@click.option("--apply-proposal-id", default=None)
def review_now_cmd(
    job_id: str, mode: str | None, apply_proposal_id: str | None
) -> None:
    result = run_job_worker(
        job_id,
        mode=normalize_agent_mode(mode or "monitor"),
        apply_proposal_id=apply_proposal_id,
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(name="proposals", help="List proposals for a job.")
@click.argument("job_id")
def proposals_cmd(job_id: str) -> None:
    store = JobStore()
    _echo_json({"ok": True, "result": store.proposals(job_id)})


def _wakeup_with_proposal(
    store: JobStore, job_id: str, proposal_id: str, proposal: dict[str, Any]
) -> None:
    wakeup = run_job_worker(job_id, mode="intervene", apply_proposal_id=proposal_id)
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": {"proposal": proposal, "wakeup": wakeup}})


@job_cli.command(name="approve", help="Approve a pending proposal.")
@click.argument("job_id")
@click.argument("proposal_id")
@click.option(
    "--skip-gate",
    is_flag=True,
    default=False,
    help="Skip the legacy-contract gate check (not recommended).",
)
def approve_cmd(job_id: str, proposal_id: str, skip_gate: bool) -> None:
    store = JobStore()
    # The SDK is the authoritative gate even when the backend is bypassed:
    # legacy jobs cannot pass the versioned-change flow.
    job = store.load(job_id)
    if job.execution_contract != "jobs_v1" and not skip_gate:
        _echo_json(
            {
                "ok": False,
                "error": (
                    "job is on the legacy execution contract; run "
                    "`wayfinder job migrate-contract` before approving proposals"
                ),
            }
        )
        raise click.ClickException("legacy jobs cannot enter the versioned-change flow")
    _wakeup_with_proposal(
        store, job_id, proposal_id, store.approve_proposal(job_id, proposal_id)
    )


@job_cli.command(name="reject", help="Reject a pending proposal.")
@click.argument("job_id")
@click.argument("proposal_id")
def reject_cmd(job_id: str, proposal_id: str) -> None:
    store = JobStore()
    proposal = store.reject_proposal(job_id, proposal_id)
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": proposal})


@job_cli.command(name="apply-proposal", help="Queue apply for an approved proposal.")
@click.argument("job_id")
@click.argument("proposal_id")
def apply_proposal_cmd(job_id: str, proposal_id: str) -> None:
    store = JobStore()
    _wakeup_with_proposal(
        store,
        job_id,
        proposal_id,
        store.queue_proposal_application(job_id, proposal_id),
    )


@job_cli.command(
    name="claim-application", help="Claim an approved proposal for application."
)
@click.argument("job_id")
@click.argument("proposal_id")
def claim_application_cmd(job_id: str, proposal_id: str) -> None:
    store = JobStore()
    _echo_json({"ok": True, "result": claim_application(store, job_id, proposal_id)})


@job_cli.command(
    name="validate-application",
    help="Validate the staged candidate for an in-progress proposal application.",
)
@click.argument("job_id")
@click.argument("proposal_id")
def validate_application_cmd(job_id: str, proposal_id: str) -> None:
    store = JobStore()
    _echo_json(
        {
            "ok": True,
            "result": validate_application_candidate(store, job_id, proposal_id),
        }
    )


@job_cli.command(name="complete-application", help="Finish a proposal application.")
@click.argument("job_id")
@click.argument("proposal_id")
@click.option("--status", type=click.Choice(["applied", "failed"]), required=True)
@click.option("--changed-file", "changed_files", multiple=True)
@click.option("--validation-json", default=None)
@click.option("--error", "error_text", default=None)
def complete_application_cmd(
    job_id: str,
    proposal_id: str,
    status: str,
    changed_files: tuple[str, ...],
    validation_json: str | None,
    error_text: str | None,
) -> None:
    store = JobStore()
    validation = json.loads(validation_json) if validation_json else {}
    _echo_json(
        {
            "ok": True,
            "result": complete_application(
                store,
                job_id,
                proposal_id,
                status=status,
                changed_files=list(changed_files),
                validation=validation,
                error=error_text,
            ),
        }
    )


def _pause_resume_loops(job_id: str, action: Literal["pause", "resume"]) -> None:
    store = JobStore()
    job = store.load(job_id)
    bridge = RunnerBridge(repo_root=store.repo_root)
    method = bridge.pause if action == "pause" else bridge.resume
    responses = [
        method(loop.runner_job_name)
        for loop in (job.script_loop, job.agent_loop)
        if loop.enabled
    ]
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": responses})


@job_cli.command(name="pause", help="Pause a job's runner loops.")
@click.argument("job_id")
def pause_cmd(job_id: str) -> None:
    _pause_resume_loops(job_id, "pause")


@job_cli.command(name="resume", help="Resume a job's runner loops.")
@click.argument("job_id")
def resume_cmd(job_id: str) -> None:
    _pause_resume_loops(job_id, "resume")


@job_cli.command(name="delete", help="Delete runner links for a high-level job.")
@click.argument("job_id")
def delete_cmd(job_id: str) -> None:
    store = JobStore()
    job = store.load(job_id)
    bridge = RunnerBridge(repo_root=store.repo_root)
    responses = [
        bridge.delete(loop.runner_job_name)
        for loop in (job.script_loop, job.agent_loop)
        if loop.runner_job_name
    ]
    store.refresh_scorecard(job_id, {"health": "unknown", "deleted": True})
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": responses})


def _auto_limits_from_options(
    *,
    venues: tuple[str, ...],
    symbols: tuple[str, ...],
    markets: tuple[str, ...],
    max_notional_per_decision: float | None,
    max_daily_notional: float | None,
    max_open_positions: int | None,
    max_open_orders: int | None,
) -> dict[str, Any]:
    limits: dict[str, Any] = {}
    if venues:
        limits["enabled_venues"] = list(venues)
    if symbols:
        limits["allowed_symbols"] = list(symbols)
    if markets:
        limits["allowed_markets"] = list(markets)
    if max_notional_per_decision is not None:
        limits["max_notional_per_decision"] = max_notional_per_decision
    if max_daily_notional is not None:
        limits["max_daily_notional"] = max_daily_notional
    if max_open_positions is not None:
        limits["max_open_positions"] = max_open_positions
    if max_open_orders is not None:
        limits["max_open_orders"] = max_open_orders
    return limits
