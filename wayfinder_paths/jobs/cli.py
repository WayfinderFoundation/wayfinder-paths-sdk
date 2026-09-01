from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal

import click

from wayfinder_paths.jobs.application import (
    claim_application,
    complete_application,
    ensure_jobs_v1_contract,
    rollback_application,
    validate_application_candidate,
)
from wayfinder_paths.jobs.apply_launcher import launch_application
from wayfinder_paths.jobs.background import spawn_detached_op
from wayfinder_paths.jobs.backtest_artifacts import (
    diagnose_backtest,
    load_backtest_view,
)
from wayfinder_paths.jobs.compiler import JobCompiler, compile_job
from wayfinder_paths.jobs.counterfactual import counterfactual_job
from wayfinder_paths.jobs.decision_gates import (
    load_decision_gates,
    register_decision_gate,
    reopen_decision_gate,
    resolve_decision_gate,
)
from wayfinder_paths.jobs.decision_log import build_decision_log
from wayfinder_paths.jobs.evolution_campaign import (
    campaign_status,
    prepare_candidate,
    start_campaign,
    submit_research_seed,
)
from wayfinder_paths.jobs.execution.driver import tick_job
from wayfinder_paths.jobs.execution.experiments import (
    list_experiments,
    promote_params,
    run_experiment,
)
from wayfinder_paths.jobs.execution.job import (
    backtest_execution_job,
    summarize_backtest_payload,
)
from wayfinder_paths.jobs.execution.preflight import (
    build_live_dataset,
    fetch_funding_features,
    run_preflight,
)
from wayfinder_paths.jobs.execution.reconcile import reconcile_job
from wayfinder_paths.jobs.execution.validation import validate_execution_job
from wayfinder_paths.jobs.execution.walk_forward import format_fold_table
from wayfinder_paths.jobs.exhaustion import (
    CLAIM_PROVENANCES,
    CLAIM_STATUSES,
    adjudicate_exhaustion_claim,
    audit_and_adjudicate_exhaustion_claim,
    file_exhaustion_claim,
    list_exhaustion_claims,
    reopen_exhaustion_claim,
)
from wayfinder_paths.jobs.features import append_feature, list_features
from wayfinder_paths.jobs.forward_artifacts import load_forward_view
from wayfinder_paths.jobs.gating import evaluate_live_gate
from wayfinder_paths.jobs.halt import clear_halt, request_halt
from wayfinder_paths.jobs.ledger import append_ledger_row, tail_ledger
from wayfinder_paths.jobs.lifecycle import lifecycle_sweep
from wayfinder_paths.jobs.models import (
    AgentMode,
    WayfinderJob,
    default_wake_seconds,
    infer_job_kind,
    normalize_agent_mode,
)
from wayfinder_paths.jobs.pattern_match_universe import (
    create_pattern_match_universe_job,
)
from wayfinder_paths.jobs.probation import open_paper_probation_leg
from wayfinder_paths.jobs.proposals import (
    propose_change,
    restage_proposal,
    revalidate_proposal,
)
from wayfinder_paths.jobs.regime_health import regime_health_job
from wayfinder_paths.jobs.replication import replication_job
from wayfinder_paths.jobs.research import (
    holdout_check_job,
    pair_check_job,
    rank_check_job,
    signal_check_job,
    signal_scan_job,
)
from wayfinder_paths.jobs.risk_overrides import risk_block_symbol, risk_unblock_symbol
from wayfinder_paths.jobs.robustness import robustness_check_job
from wayfinder_paths.jobs.runner_bridge import RunnerBridge
from wayfinder_paths.jobs.starters import create_starter_job, starter_catalog
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.strategies import library_catalog
from wayfinder_paths.jobs.sync import (
    apply_execution_leverage,
    apply_initial_capital,
    apply_script_mode,
    apply_wallet_label,
    snapshot_job,
    sync_all_jobs,
    venue_deposit,
    venue_withdraw,
)
from wayfinder_paths.jobs.universe import universe_scan_job
from wayfinder_paths.jobs.worker import nudge_evolution_session, run_job_worker


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

    auto_limits: dict[str, Any] = {}
    if auto_venues:
        auto_limits["enabled_venues"] = list(auto_venues)
    if auto_symbols:
        auto_limits["allowed_symbols"] = list(auto_symbols)
    if auto_markets:
        auto_limits["allowed_markets"] = list(auto_markets)
    if max_notional_per_decision is not None:
        auto_limits["max_notional_per_decision"] = max_notional_per_decision
    if max_daily_notional is not None:
        auto_limits["max_daily_notional"] = max_daily_notional
    if max_open_positions is not None:
        auto_limits["max_open_positions"] = max_open_positions
    if max_open_orders is not None:
        auto_limits["max_open_orders"] = max_open_orders

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
        auto_limits=auto_limits,
    )
    if initial_capital is not None:
        job.execution_params["initial_capital"] = float(initial_capital)
    path = store.create_job(job)
    result: dict[str, Any] = {"job": job.to_dict(), "job_yaml": str(path)}
    entrypoint = store.resolve_script_entrypoint(job.id, job.to_dict())
    if entrypoint is not None:
        result["script_entrypoint"] = str(entrypoint)
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


@job_cli.command(
    name="starter-strategies",
    help="List the selectable, paper-only jobs_v1 starter strategies.",
)
def starter_strategies_cmd() -> None:
    _echo_json({"ok": True, "result": starter_catalog()})


@job_cli.command(
    name="create-starter",
    help="Create a paper jobs_v1 job from a selectable starter definition.",
)
@click.argument("starter_id")
@click.option("--job-id", default=None, help="Override the generated job id.")
@click.option(
    "--initializer-session-id",
    default=None,
    help="Strategy Lab session that initiated this job.",
)
@click.option(
    "--leverage",
    type=click.IntRange(1, 5),
    default=None,
    help="Initial paper leverage (1-5x; defaults to 1x).",
)
@click.option(
    "--agent-mode",
    "agent_mode",
    type=click.Choice(["off", "monitor", "intervene", "auto", "improve", "decide"]),
    default=None,
    help="Override the catalog launch default (intervene).",
)
@click.option("--no-compile", is_flag=True, default=False)
def create_starter_cmd(
    starter_id: str,
    job_id: str | None,
    initializer_session_id: str | None,
    leverage: int | None,
    agent_mode: str | None,
    no_compile: bool,
) -> None:
    result = create_starter_job(
        starter_id,
        job_id=job_id,
        initializer_session_id=initializer_session_id,
        leverage=leverage,
        agent_mode=agent_mode,
        compile_job=not no_compile,
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="create-pattern-universe",
    help=(
        "Create the shadow-first 15m Pattern Match job over liquid native "
        "and HIP-3 perps."
    ),
)
@click.option("--job-id", default="pattern-match-universe-15m", show_default=True)
@click.option(
    "--minimum-volume-usd", type=float, default=5_000_000.0, show_default=True
)
@click.option(
    "--agent-mode",
    type=click.Choice(["off", "monitor", "intervene", "auto", "improve", "decide"]),
    default="intervene",
    show_default=True,
)
@click.option("--no-compile", is_flag=True, default=False)
def create_pattern_universe_cmd(
    job_id: str,
    minimum_volume_usd: float,
    agent_mode: str,
    no_compile: bool,
) -> None:
    result = create_pattern_match_universe_job(
        job_id=job_id,
        minimum_volume_usd=minimum_volume_usd,
        agent_mode=agent_mode,
        compile_job=not no_compile,
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="compile",
    help="Recompile runner wrappers/links from job.yaml (source of truth). "
    "Run after editing script_loop schedule, mode, or execution_contract.",
)
@click.argument("job_id")
def compile_cmd(job_id: str) -> None:
    _echo_json({"ok": True, "result": compile_job(job_id)})


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
    result = validate_execution_job(job_id, strict=strict, store=store)
    _echo_json({"ok": result["status"] == "passed", "result": result})
    if strict and result["status"] != "passed":
        raise click.ClickException("job validation failed")


@job_cli.command(name="backtest", help="Run an execution-contract backtest for a job.")
@click.argument("job_id")
@click.option("--grid", "grid_path", default=None)
@click.option(
    "--workers",
    type=int,
    default=0,
    show_default=True,
    help="Parallel backtest workers for a grid. 0 = use all available cores. "
    "Always clamped to the box's core count — never oversubscribes.",
)
@click.option(
    "--parallel",
    type=click.Choice(["serial", "thread", "process"]),
    default="serial",
    show_default=True,
    help="Grid parallelism. `process` uses multiple cores (bounded by "
    "--workers/CPU count); `thread` won't speed up CPU-bound backtests.",
)
@click.option(
    "--quick",
    "quick_bars",
    type=int,
    default=None,
    help="Backtest only the last N bars — fast iteration / parameter sweeps "
    "before the full-history confirmation run.",
)
@click.option(
    "--full",
    is_flag=True,
    default=False,
    help="Print the full payload (equity curve, trades, positions, trace, "
    "visualization) instead of the compact stats summary. The full result is "
    "always written to results/backtest/ regardless.",
)
def backtest_cmd(
    job_id: str,
    grid_path: str | None,
    workers: int,
    parallel: str,
    quick_bars: int | None,
    full: bool,
) -> None:
    store = JobStore()
    result = backtest_execution_job(
        job_id,
        grid_path=grid_path,
        workers=workers,
        parallel=parallel,
        quick_bars=quick_bars,
        store=store,
    )
    # Default to the ~2 KB summary — the full payload is ~8 MB and lives on disk
    # (browse it with `job backtest-view`). `--full` restores the old dump.
    _echo_json(
        {"ok": True, "result": result if full else summarize_backtest_payload(result)}
    )


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
    report = validate_execution_job(job_id, store=store)
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
    store = JobStore()
    job = store.load(job_id)
    if job.execution_contract != "jobs_v1":
        raise click.ClickException(
            "tick requires a jobs_v1 job; run migrate-contract first"
        )
    effective_mode = "paper" if dry_run else (mode or job.script_loop.mode or "paper")
    root = Path(store.job_dir(job_id))
    payload = asyncio.run(tick_job(job, root, effective_mode, store=store))
    _echo_json(payload)


@job_cli.command(
    name="experiments",
    help="Run a parameter-grid experiment (or list recorded experiments).",
)
@click.argument("job_id")
@click.option("--grid", "grid_path", default=None, help="Path to a grid JSON file.")
@click.option("--rank-by", default="net_return", show_default=True)
@click.option(
    "--workers",
    type=int,
    default=0,
    show_default=True,
    help="Grid workers; 0 = all cores (cgroup-clamped).",
)
@click.option(
    "--parallel",
    type=click.Choice(["serial", "thread", "process"]),
    default="process",
    show_default=True,
)
@click.option(
    "--quick",
    "quick_bars",
    type=int,
    default=None,
    help="Run the whole experiment (grid + walk-forward) on only the last N "
    "bars — iteration-speed sweeps; omit for the final validation.",
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
@click.option(
    "--objectives",
    default=None,
    help=(
        "Comma-separated grid metrics for multi-objective search (NSGA-II "
        "Pareto front), e.g. net_return,max_drawdown_pct. Requires "
        "--optimizer optuna; --rank-by becomes the tie-break."
    ),
)
def experiments_cmd(
    job_id: str,
    grid_path: str | None,
    rank_by: str,
    workers: int,
    parallel: str,
    quick_bars: int | None,
    list_only: bool,
    wf_test_bars: int | None,
    wf_train_bars: int | None,
    wf_folds: int,
    wf_warmup_bars: int,
    wf_anchored: bool,
    optimizer: str,
    n_trials: int,
    seed: int,
    objectives: str | None,
) -> None:
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
            # Anchor (expanding train window) ONLY when explicitly asked. Default
            # (no --wf-train-bars) → run_walk_forward's bounded rolling window,
            # which is ~4x faster than expanding on a full dataset.
            "anchored": wf_anchored,
        }
    optuna_options = (
        {
            "n_trials": n_trials,
            "seed": seed,
            **(
                {"objectives": [x.strip() for x in objectives.split(",") if x.strip()]}
                if objectives
                else {}
            ),
        }
        if optimizer == "optuna"
        else None
    )
    if objectives and optimizer != "optuna":
        raise click.UsageError("--objectives requires --optimizer optuna")
    result = run_experiment(
        job_id,
        grid_path,
        rank_by=rank_by,
        workers=workers,
        parallel=parallel,
        walk_forward=walk_forward,
        optimizer=optimizer,
        optuna_options=optuna_options,
        quick_bars=quick_bars,
        store=store,
    )
    wf_report = (result.get("backtest") or {}).get("walk_forward")
    if wf_report:
        click.echo(format_fold_table(wf_report), err=True)
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="promote-params",
    help="Promote winning experiment params into the job (direct pre-live, or "
    "as a proposal for live jobs).",
)
@click.argument("job_id")
@click.option(
    "--grid", "grid_id", default=None, help="Grid id under results/backtest/grids/."
)
@click.option(
    "--run", "run_id", default=None, help="Specific run id (default: best ranked)."
)
@click.option("--params", "params_json", default=None, help="Explicit params JSON.")
@click.option("--via-proposal", is_flag=True, default=False)
def promote_params_cmd(
    job_id: str,
    grid_id: str | None,
    run_id: str | None,
    params_json: str | None,
    via_proposal: bool,
) -> None:
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
@click.option(
    "--quote",
    default=None,
    help="Quote currency (default: USDC on hyperliquid, USDT elsewhere).",
)
@click.option(
    "--include-funding",
    is_flag=True,
    default=False,
    help="Fetch same-window historical funding in the same isolated operation.",
)
@click.option(
    "--full",
    is_flag=True,
    default=False,
    help="Force a full refetch instead of the default incremental tail merge.",
)
def fetch_dataset_cmd(
    job_id: str,
    days: int,
    source: str,
    exchange: str,
    market_type: str,
    quote: str | None,
    include_funding: bool,
    full: bool,
) -> None:
    store = JobStore()
    result = build_live_dataset(
        job_id,
        days=days,
        store=store,
        source=source,
        exchange=exchange,
        market_type=market_type,
        quote=quote,
        incremental=not full,
    )
    if include_funding:
        result["funding"] = fetch_funding_features(
            job_id,
            days=days,
            exchange=exchange,
            quote=quote,
            store=store,
        )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="pair-check",
    help="Pre-trade pair admission gate: cointegration both directions, "
    "rolling stability, half-life, cost hurdle, funding carry — PASS/REJECT "
    "with numbers. Run BEFORE building any pair/spread strategy.",
)
@click.argument("job_id")
@click.option("--symbols", default=None, help="Comma-separated pair, e.g. ETH,SOL.")
@click.option("--days", type=int, default=720, show_default=True)
@click.option("--bar", "bar_interval", default=None, help="Bar interval override.")
@click.option("--exchange", default="binance", show_default=True)
@click.option("--fee-bps", "fee_bps", type=float, default=None)
@click.option("--slippage-bps", "slippage_bps", type=float, default=None)
def pair_check_cmd(
    job_id: str,
    symbols: str | None,
    days: int,
    bar_interval: str | None,
    exchange: str,
    fee_bps: float | None,
    slippage_bps: float | None,
) -> None:
    store = JobStore()
    result = pair_check_job(
        job_id,
        symbols=[s.strip() for s in symbols.split(",")] if symbols else None,
        days=days,
        bar_interval=bar_interval,
        exchange=exchange,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        store=store,
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="signal-check",
    help="Event-study a strategy's precomputed signal column against forward "
    "returns (vs the series' own drift) — run BEFORE building a strategy "
    "around the signal.",
)
@click.argument("job_id")
@click.option("--column", required=True, help="Precomputed signal column name.")
@click.option(
    "--horizons", default=None, help="Comma-separated forward horizons in bars."
)
@click.option(
    "--direction",
    type=click.Choice(["long", "short", "auto"]),
    default="auto",
    show_default=True,
    help="Trade side under test. A genuine short edge has NEGATIVE forward "
    "returns; auto reads the side from the t-stat sign (counts as 2 trials).",
)
def signal_check_cmd(
    job_id: str, column: str, horizons: str | None, direction: str
) -> None:
    store = JobStore()
    result = signal_check_job(
        job_id,
        column=column,
        horizons=[int(h) for h in horizons.split(",")] if horizons else None,
        direction=direction,
        store=store,
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="signal-scan",
    help="Event-study the ENTIRE canonical trigger library against the job's "
    "dataset in one call (both directions, multi-timeframe, BH q-values, "
    "4-fold stability, reserved holdout tail) — run BEFORE hand-writing "
    "trigger variants into precompute(). Needs no strategy script.",
)
@click.argument("job_id")
@click.option("--symbols", default=None, help="Comma-separated symbols (default: all).")
@click.option(
    "--horizons", default=None, help="Comma-separated forward horizons in bars."
)
@click.option(
    "--timeframes",
    default=None,
    help="Comma-separated resample timeframes, e.g. 1h,4h,1d "
    "(default: the job's base bar interval only).",
)
@click.option(
    "--holdout-fraction",
    type=float,
    default=0.15,
    show_default=True,
    help="Final fraction of history the scan NEVER sees — reserved for one "
    "holdout-check per frozen candidate. 0 disables (exploratory only).",
)
@click.option(
    "--no-workspace-signals",
    is_flag=True,
    default=False,
    help="Skip the job's workspace/src/signals.py defs and sweep the "
    "canonical library only.",
)
@click.option(
    "--campaign",
    default=None,
    help="Declared campaign name. Families below 50 cells are pooled with "
    "the canonical library; larger families scan workspace defs plus the "
    "incumbent controls. Recorded in the trial ledger.",
)
@click.option(
    "--condition-regime",
    "condition_regime",
    is_flag=True,
    default=False,
    help="Also compute per-regime rows (trend x vol 2x2) and report the "
    "CURRENT regime — regime-conditional edges in the current regime are "
    "probation-eligible.",
)
@click.option(
    "--window-days",
    "window_days",
    type=int,
    default=None,
    help="Declared recent-window family: scan only the trailing N days. "
    "Survivors cap at PROBATION (forward paper adjudicates).",
)
def signal_scan_cmd(
    job_id: str,
    symbols: str | None,
    horizons: str | None,
    timeframes: str | None,
    holdout_fraction: float,
    no_workspace_signals: bool,
    campaign: str | None,
    condition_regime: bool,
    window_days: int | None,
) -> None:
    store = JobStore()
    result = signal_scan_job(
        job_id,
        campaign=campaign,
        condition_regime=condition_regime,
        window_days=window_days,
        symbols=[s.strip() for s in symbols.split(",")] if symbols else None,
        horizons=[int(h) for h in horizons.split(",")] if horizons else None,
        timeframes=[t.strip() for t in timeframes.split(",")] if timeframes else None,
        holdout_fraction=holdout_fraction,
        include_workspace=not no_workspace_signals,
        store=store,
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="chart",
    help="Text chart lens: per-bar OHLCV + requested indicator columns with "
    "forward trades annotated inline and a regime header. The numeric "
    "equivalent of dragging indicators onto the chart and zooming into the "
    "hours around a trade.",
)
@click.argument("job_id")
@click.option("--symbol", default=None, help="Symbol (default: first in dataset).")
@click.option("--timeframe", default=None, help="Resample: 5m/15m/30m/1h/4h/1d.")
@click.option("--bars", type=int, default=96, show_default=True)
@click.option(
    "--indicators",
    default=None,
    help="Comma-separated specs, e.g. ema:9,ema:50,rsi:14,bb:20:2,atr:14,"
    "macd:12:26:9,don:20,vwap,volpct:14 (default ema:9,ema:50).",
)
@click.option(
    "--around-trade",
    "around_trade",
    default=None,
    help="'last' or a close timestamp — center the window on that forward trade.",
)
def chart_cmd(
    job_id: str,
    symbol: str | None,
    timeframe: str | None,
    bars: int,
    indicators: str | None,
    around_trade: str | None,
) -> None:
    from wayfinder_paths.jobs.chart import chart_job

    result = chart_job(
        job_id,
        symbol=symbol,
        timeframe=timeframe,
        bars=bars,
        indicators=[i.strip() for i in indicators.split(",")] if indicators else None,
        around_trade=around_trade,
        store=JobStore(),
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="analogs",
    help="Historical analog search: z-score a recent close window, find the "
    "nearest non-overlapping analogs in history, report the forward-outcome "
    "distribution. EXPLORATORY — hypothesis fuel for the scan pipeline.",
)
@click.argument("job_id")
@click.option("--symbol", default=None)
@click.option("--timeframe", default=None)
@click.option("--window", type=int, default=24, show_default=True)
@click.option("--at", default=None, help="Query window end (default: latest bar).")
@click.option("--top", type=int, default=15, show_default=True)
@click.option("--horizon", type=int, default=12, show_default=True)
def analogs_cmd(
    job_id: str,
    symbol: str | None,
    timeframe: str | None,
    window: int,
    at: str | None,
    top: int,
    horizon: int,
) -> None:
    from wayfinder_paths.jobs.chart import analogs_job

    result = analogs_job(
        job_id,
        symbol=symbol,
        timeframe=timeframe,
        window=window,
        at=at,
        top=top,
        horizon=horizon,
        store=JobStore(),
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="derive-features",
    help="Append cross-symbol/exogenous/venue derived feature rows to the "
    "feature store (research-side; coarse cadence; execution contract "
    "untouched). Sets: cross, exog, venue.",
)
@click.argument("job_id")
@click.option("--sets", default="cross,exog", show_default=True)
@click.option("--exog-symbols", default="BTC", show_default=True)
@click.option("--every-bars", type=int, default=12, show_default=True)
def derive_features_cmd(
    job_id: str, sets: str, exog_symbols: str, every_bars: int
) -> None:
    from wayfinder_paths.jobs.derived_features import derive_features_job

    result = derive_features_job(
        job_id,
        sets=tuple(s.strip() for s in sets.split(",") if s.strip()),
        exog_symbols=tuple(s.strip() for s in exog_symbols.split(",") if s.strip()),
        every_bars=every_bars,
        store=JobStore(),
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="attribution",
    help="PnL attribution: backtest + forward decomposed by symbol/reason/"
    "session/regime/archetype/hold-bucket, with forward-vs-backtest "
    "expectation deltas ranked by anomaly size. The diagnosis artifact.",
)
@click.argument("job_id")
def attribution_cmd(job_id: str) -> None:
    from wayfinder_paths.jobs.attribution import attribution_job

    _echo_json({"ok": True, "result": attribution_job(job_id, store=JobStore())})


@job_cli.command(
    name="holdout-check",
    help="One-shot confirmation of a FROZEN scan candidate (signal + "
    "timeframe + horizon + direction) on the reserved holdout tail. Spend "
    "it once per candidate — the trial ledger remembers repeat looks.",
)
@click.argument("job_id")
@click.option(
    "--signal",
    required=True,
    help="Canonical library or workspace (workspace/src/signals.py) signal name.",
)
@click.option("--horizon", type=int, required=True, help="Forward horizon in bars.")
@click.option(
    "--direction",
    type=click.Choice(["long", "short"]),
    required=True,
    help="The frozen candidate's trade side.",
)
@click.option(
    "--timeframe", default=None, help="Resample timeframe (default: base interval)."
)
@click.option("--symbols", default=None, help="Comma-separated symbols (default: all).")
def holdout_check_cmd(
    job_id: str,
    signal: str,
    horizon: int,
    direction: str,
    timeframe: str | None,
    symbols: str | None,
) -> None:
    store = JobStore()
    result = holdout_check_job(
        job_id,
        signal=signal,
        horizon=horizon,
        direction=direction,
        timeframe=timeframe,
        symbols=[s.strip() for s in symbols.split(",")] if symbols else None,
        store=store,
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="strategy-library",
    help="List the shipped reference strategies (verbatim ports of audited "
    "live scripts) with their import lines and default params — when the "
    "user references a known/live strategy, start here instead of "
    "transcribing it from prose.",
)
def strategy_library_cmd() -> None:
    _echo_json({"ok": True, "result": library_catalog()})


@job_cli.command(
    name="rank-check",
    help="Rank-IC study of a precomputed cross-sectional ranking column vs "
    "relative forward returns — run BEFORE building a long/short basket "
    "on that ranking.",
)
@click.argument("job_id")
@click.option("--column", required=True, help="Precomputed ranking column name.")
@click.option(
    "--horizons", default=None, help="Comma-separated forward horizons in bars."
)
def rank_check_cmd(job_id: str, column: str, horizons: str | None) -> None:
    store = JobStore()
    result = rank_check_job(
        job_id,
        column=column,
        horizons=[int(h) for h in horizons.split(",")] if horizons else None,
        store=store,
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="fetch-funding",
    help="Fetch historical funding rates into the job's feature store "
    "(state/features.jsonl) and declare the 'funding' feature — first-class "
    "carry data, as-of merged onto the bars in backtest AND live.",
)
@click.argument("job_id")
@click.option("--days", type=int, default=30, show_default=True)
@click.option("--exchange", default="binance", show_default=True)
@click.option(
    "--quote",
    default=None,
    help="Quote currency (default: USDC on hyperliquid, USDT elsewhere).",
)
def fetch_funding_cmd(job_id: str, days: int, exchange: str, quote: str | None) -> None:
    store = JobStore()
    result = fetch_funding_features(
        job_id, days=days, exchange=exchange, quote=quote, store=store
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="robustness-check",
    help="Run advisory neighbor/phase/leverage/walk-forward/scenario evidence.",
)
@click.argument("job_id")
@click.option("--candidate-dir", type=click.Path(path_type=Path), default=None)
@click.option(
    "--plan",
    "plan_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional plan JSON; defaults to validation.robustness_plan.",
)
@click.option(
    "--foreground",
    is_flag=True,
    default=False,
    help="Wait for completion instead of starting the isolated background op.",
)
def robustness_check_cmd(
    job_id: str,
    candidate_dir: Path | None,
    plan_path: Path | None,
    foreground: bool,
) -> None:
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path else None
    if foreground:
        result = robustness_check_job(
            job_id,
            candidate_dir=candidate_dir,
            robustness_plan=plan,
            store=JobStore(),
        )
    else:
        result = spawn_detached_op(
            JobStore(),
            job_id,
            "robustness_check",
            {
                "job_id": job_id,
                "candidate_dir": str(candidate_dir) if candidate_dir else None,
                "robustness_plan": plan,
            },
        )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="evolution-start",
    help="Start the gated, isolated open-ended research campaign for a job.",
)
@click.argument("job_id")
@click.option("--force", is_flag=True, help="Replace cooldown/completed state.")
def evolution_start_cmd(job_id: str, force: bool) -> None:
    store = JobStore()
    try:
        result = start_campaign(store, job_id, force=force)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    # The op-runner path nudges on op completion; an inline CLI start must
    # nudge itself or the fresh campaign idles until the next hourly wake.
    nudge = nudge_evolution_session(store, job_id)
    _echo_json({"ok": True, "result": result, "nudge": nudge})


@job_cli.command(name="evolution-status", help="Show the current evolution campaign.")
@click.argument("job_id")
def evolution_status_cmd(job_id: str) -> None:
    _echo_json({"ok": True, "result": campaign_status(JobStore(), job_id)})


@job_cli.command(
    name="evolution-prepare",
    help="Create one isolated candidate bundle for the code-mutation worker.",
)
@click.argument("job_id")
@click.option("--family", required=True)
@click.option("--summary", required=True)
@click.option(
    "--mutation-kind", type=click.Choice(["structural", "parameter"]), default=None
)
def evolution_prepare_cmd(
    job_id: str, family: str, summary: str, mutation_kind: str | None
) -> None:
    try:
        result = prepare_candidate(
            JobStore(),
            job_id,
            family=family,
            summary=summary,
            mutation_kind=mutation_kind,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="evolution-submit-seed",
    help="Freeze a sensor-authored executable candidate for the next campaign.",
)
@click.argument("job_id")
@click.argument("candidate_dir", type=click.Path(path_type=Path, exists=True))
@click.option("--family", required=True)
@click.option("--hypothesis", required=True)
@click.option("--base-revision", required=True)
@click.option("--evidence-ref", "evidence_refs", multiple=True)
def evolution_submit_seed_cmd(
    job_id: str,
    candidate_dir: Path,
    family: str,
    hypothesis: str,
    base_revision: str,
    evidence_refs: tuple[str, ...],
) -> None:
    try:
        result = submit_research_seed(
            JobStore(),
            job_id,
            candidate_root=candidate_dir,
            family=family,
            hypothesis=hypothesis,
            base_revision=base_revision,
            evidence_refs=list(evidence_refs),
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": result})


@job_cli.command(name="risk-block-symbol", help="Block new entries for one symbol.")
@click.argument("job_id")
@click.argument("symbol")
@click.option("--reason", required=True)
@click.option("--evidence-ref", "evidence_refs", multiple=True, required=True)
@click.option("--wake-id", default=None)
def risk_block_symbol_cmd(
    job_id: str,
    symbol: str,
    reason: str,
    evidence_refs: tuple[str, ...],
    wake_id: str | None,
) -> None:
    try:
        result = risk_block_symbol(
            JobStore(),
            job_id,
            symbol=symbol,
            reason=reason,
            evidence_refs=list(evidence_refs),
            wake_id=wake_id,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": result})


@job_cli.command(name="risk-unblock-symbol", help="Owner-only symbol re-arm.")
@click.argument("job_id")
@click.argument("symbol")
@click.option("--by", type=click.Choice(["owner"]), required=True)
@click.option("--reason", default=None)
def risk_unblock_symbol_cmd(
    job_id: str, symbol: str, by: str, reason: str | None
) -> None:
    try:
        result = risk_unblock_symbol(
            JobStore(), job_id, symbol=symbol, by=by, reason=reason
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="evolution-evaluate",
    help="Run a candidate's static and low-fidelity screen in isolation.",
)
@click.argument("job_id")
@click.argument("candidate_id")
@click.option("--foreground", is_flag=True)
def evolution_evaluate_cmd(job_id: str, candidate_id: str, foreground: bool) -> None:
    if foreground:
        from wayfinder_paths.jobs.evolution_campaign import evaluate_candidate

        result = evaluate_candidate(JobStore(), job_id, candidate_id)
    else:
        result = spawn_detached_op(
            JobStore(),
            job_id,
            "evolution_evaluate",
            {"job_id": job_id, "candidate_id": candidate_id},
        )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="evolution-finalize",
    help="Run bounded full-dev and stage one causal paper proposal.",
)
@click.argument("job_id")
@click.option("--foreground", is_flag=True)
def evolution_finalize_cmd(job_id: str, foreground: bool) -> None:
    if foreground:
        from wayfinder_paths.jobs.evolution_campaign import finalize_campaign

        result = finalize_campaign(JobStore(), job_id)
    else:
        result = spawn_detached_op(
            JobStore(), job_id, "evolution_finalize", {"job_id": job_id}
        )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="forward-experience",
    help="Refresh owner-scoped live execution calibration and paper priors.",
)
@click.argument("job_id")
@click.option("--foreground", is_flag=True)
def forward_experience_cmd(job_id: str, foreground: bool) -> None:
    if foreground:
        from wayfinder_paths.jobs.forward_experience import build_forward_experience

        result = build_forward_experience(JobStore(), job_id)
    else:
        result = spawn_detached_op(
            JobStore(), job_id, "forward_experience", {"job_id": job_id}
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
    store = JobStore()
    gate = evaluate_live_gate(job_id, store=store)
    _echo_json({"ok": gate["live_ready"], "result": gate})


@job_cli.command(
    name="evolution",
    help="Full update path + promotion reliability (evolution ledger).",
)
@click.argument("job_id")
def evolution_cmd(job_id: str) -> None:
    from wayfinder_paths.jobs.evolution_ledger import build_evolution_report

    _echo_json({"ok": True, "result": build_evolution_report(JobStore(), job_id)})


@job_cli.command(
    name="archive",
    help="Candidate archive: Pareto frontier, branches, refuted lineage.",
)
@click.argument("job_id")
def archive_cmd(job_id: str) -> None:
    from wayfinder_paths.jobs.archive import load_archive

    _echo_json({"ok": True, "result": load_archive(JobStore(), job_id)})


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
@click.option(
    "--proposal",
    "proposal_id",
    default=None,
    help="Read the proposal's CANDIDATE backtest run instead of the active one.",
)
def backtest_view_cmd(
    job_id: str,
    view: str,
    series_names: tuple[str, ...],
    from_ts: str | None,
    to_ts: str | None,
    max_points: int,
    proposal_id: str | None,
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
        proposal_id=proposal_id,
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="forward-view",
    help="Bounded forward (paper/live) visualization payload: price series, "
    "PnL curve, and entry/exit markers tagged with the mode they executed in.",
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
@click.option(
    "--no-prices",
    is_flag=True,
    default=False,
    help="Skip the on-demand candle fetch (markers + PnL curve only).",
)
def forward_view_cmd(
    job_id: str,
    view: str,
    series_names: tuple[str, ...],
    from_ts: str | None,
    to_ts: str | None,
    max_points: int,
    no_prices: bool,
) -> None:
    store = JobStore()
    result = load_forward_view(
        job_id,
        store=store,
        view=view,
        series_names=list(series_names),
        from_ts=from_ts,
        to_ts=to_ts,
        max_points=max_points,
        include_prices=not no_prices,
    )
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="counterfactual",
    help="Post-apply shadow A/B: replay the PRE-apply strategy (rollback "
    "backup) over the forward bars since apply and diff it against the "
    "actual book — skipped/added entries and net-PnL delta. This is how "
    "entry-gating changes are adjudicated; their cost never prints in the "
    "live book.",
)
@click.argument("job_id")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Recompute even if the cached artifact is fresh.",
)
def counterfactual_cmd(job_id: str, force: bool) -> None:
    _echo_json(
        {
            "ok": True,
            "result": counterfactual_job(job_id, store=JobStore(), force=force),
        }
    )


@job_cli.command(
    name="decision-log",
    help="Threaded narrative feed of what the job's agent tried and why "
    "(proposal generations with rejection reasons, research verdicts, "
    "discoveries, shadow A/B checkpoints) — assembled from recorded events.",
)
@click.argument("job_id")
@click.option("--limit", type=int, default=50, show_default=True)
def decision_log_cmd(job_id: str, limit: int) -> None:
    _echo_json(
        {"ok": True, "result": build_decision_log(JobStore(), job_id, limit=limit)}
    )


@job_cli.command(
    name="replication",
    help="Backtest replication monitor: re-run the ACTIVE strategy on the "
    "refreshed dataset and compare against this revision's first run — "
    "decayed=true means the deploy-time edge is not reproducing (selection "
    "on window-local noise).",
)
@click.argument("job_id")
@click.option("--force", is_flag=True, default=False)
def replication_cmd(job_id: str, force: bool) -> None:
    _echo_json(
        {"ok": True, "result": replication_job(job_id, store=JobStore(), force=force)}
    )


@job_cli.command(
    name="regime-health",
    help="Portfolio-level 7/14/30d incumbent-health monitor: recent drawdown/"
    "edge shape plus volatility, correlation, liquidity, regime-mix and "
    "funding drift. Alerts refresh attribution before treatment design; "
    "automatic responses are owner-governed and default to alert-only.",
)
@click.argument("job_id")
@click.option("--force", is_flag=True, default=False)
def regime_health_cmd(job_id: str, force: bool) -> None:
    _echo_json(
        {"ok": True, "result": regime_health_job(job_id, store=JobStore(), force=force)}
    )


@job_cli.command(
    name="universe-scan",
    help="Screen the venue perp universe for candidate symbols: filter by "
    "24h volume, run the canonical signal library with regime conditioning "
    "over each candidate's recent bars, pool ALL rows into one BH family. "
    "Output is a SHORTLIST for symbol-swap proposals — admitted symbols "
    "must still earn deployment via their own on-job scans and probation.",
)
@click.argument("job_id")
@click.option("--top", type=int, default=10, show_default=True)
@click.option("--min-volume-usd", type=float, default=5_000_000, show_default=True)
@click.option("--days", type=int, default=14, show_default=True)
@click.option("--min-events", type=int, default=20, show_default=True)
def universe_scan_cmd(
    job_id: str, top: int, min_volume_usd: float, days: int, min_events: int
) -> None:
    _echo_json(
        {
            "ok": True,
            "result": universe_scan_job(
                job_id,
                top=top,
                min_volume_usd=min_volume_usd,
                days=days,
                min_events=min_events,
                store=JobStore(),
            ),
        }
    )


@job_cli.command(
    name="backtest-diagnose",
    help="Framework-computed breakdown of the latest backtest (win rate + PnL "
    "by exit reason / close hour / side, best & worst trades). Read this to find "
    "a strategy's strong/weak spots — do NOT recompute PnL by hand; that drifts "
    "from the backtest's own numbers.",
)
@click.argument("job_id")
@click.option(
    "--proposal", "proposal_id", default=None, help="Diagnose a candidate run."
)
def backtest_diagnose_cmd(job_id: str, proposal_id: str | None) -> None:
    _echo_json(
        {
            "ok": True,
            "result": diagnose_backtest(job_id, proposal_id=proposal_id),
        }
    )


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
    click.echo(f"Health: {scorecard['health']}")
    click.echo(f"Script loop: {'on' if job['script_loop']['enabled'] else 'off'}")
    click.echo(f"Agent loop: {job['agent_loop']['mode']}")
    click.echo(
        f"Pending proposals: {sum(1 for p in proposals if p['status'] == 'pending')}"
    )
    efficiency = scorecard.get("process_efficiency") or {}
    if efficiency:
        ratio = efficiency.get("wakes_per_valid_learning")
        click.echo(
            "Research efficiency: "
            f"{efficiency.get('wakes_with_valid_learning', 0)}/"
            f"{efficiency.get('wakes_total', 0)} learning wakes; "
            f"{efficiency.get('activity_only_wakes', 0)} activity-only; "
            f"{ratio if ratio is not None else 'n/a'} wakes/valid-learning"
        )
    latest_summary = scorecard.get("last_agent_summary")
    if latest_summary:
        click.echo("")
        click.echo(f"Latest agent check: {latest_summary}")


@job_cli.command(
    name="migrate-governance",
    help="Split the legacy job-root constitution into the protected "
    "governance/<job_id>/ namespace (outside the agent-writable tree) and "
    "commit epoch 0 of the tamper-evidence chain.",
)
@click.argument("job_id")
def migrate_governance_cmd(job_id: str) -> None:
    from wayfinder_paths.jobs.governance import migrate_from_constitution

    store = JobStore()
    result = migrate_from_constitution(store.repo_root, job_id, store.job_dir(job_id))
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="governance-commit",
    help="Record the current governance file hashes as a new tamper-evidence "
    "chain epoch. Run after every deliberate owner edit — uncommitted drift "
    "loads as chain_status=tampered.",
)
@click.argument("job_id")
@click.option("--note", default="", help="Why this epoch exists.")
def governance_commit_cmd(job_id: str, note: str) -> None:
    from wayfinder_paths.jobs.governance import commit_epoch, governance_dir

    store = JobStore()
    gov_dir = governance_dir(store.repo_root, job_id)
    if not gov_dir.is_dir():
        raise click.ClickException(
            f"no governance dir for {job_id} — run migrate-governance first"
        )
    row = commit_epoch(gov_dir, note=note)
    _echo_json({"ok": True, "result": row})


@job_cli.command(
    name="set-script-mode",
    help="Flip the script loop between paper and live (recompiles the runner). "
    "Going live requires a passing live gate and a wallet_label.",
)
@click.argument("job_id")
@click.argument("mode", type=click.Choice(["paper", "live"]))
@click.option(
    "--by",
    "set_by",
    type=click.Choice(["owner", "agent"]),
    default="owner",
    help="Who is making this change (recorded in state/operator.json).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Leave live even if the engine holds open positions (orphans them).",
)
def set_script_mode_cmd(job_id: str, mode: str, set_by: str, force: bool) -> None:
    try:
        result = apply_script_mode(job_id, mode, set_by=set_by, force=force)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="set-leverage",
    help="Operator sizing knob: set execution_params.leverage (takes effect "
    "next tick, no recompile).",
)
@click.argument("job_id")
@click.argument("leverage", type=float)
def set_leverage_cmd(job_id: str, leverage: float) -> None:
    try:
        result = apply_execution_leverage(job_id, leverage)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="set-wallet-label",
    help="Bind the funded wallet a live job trades from "
    "(execution_params.wallet_label). Takes effect next tick, no recompile.",
)
@click.argument("job_id")
@click.argument("label")
def set_wallet_label_cmd(job_id: str, label: str) -> None:
    try:
        result = apply_wallet_label(job_id, label)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="set-initial-capital",
    help="Operator accounting knob: set execution_params.initial_capital to "
    "what the strategy wallet actually holds (USD). Takes effect next tick.",
)
@click.argument("job_id")
@click.argument("amount", type=float)
def set_initial_capital_cmd(job_id: str, amount: float) -> None:
    try:
        result = apply_initial_capital(job_id, amount)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="venue-deposit",
    help="Bridge USDC (>= 5) from the job's bound wallet into Hyperliquid "
    "and grow initial_capital by the same amount. Waits for the credit; "
    "an unconfirmed credit still counts (the deposit is en route).",
)
@click.argument("job_id")
@click.argument("amount", type=float)
def venue_deposit_cmd(job_id: str, amount: float) -> None:
    try:
        result = asyncio.run(venue_deposit(job_id, amount))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": result})


@job_cli.command(
    name="venue-withdraw",
    help="Withdraw USDC (>= 2 gross; Bridge2 nets $1 off) from Hyperliquid "
    "to the job's bound wallet and shrink initial_capital by the gross "
    "amount, floored at zero.",
)
@click.argument("job_id")
@click.argument("amount", type=float)
@click.option(
    "--destination",
    default=None,
    help="Arbitrum address receiving the USDC (defaults to the job's wallet).",
)
def venue_withdraw_cmd(job_id: str, amount: float, destination: str | None) -> None:
    try:
        result = asyncio.run(venue_withdraw(job_id, amount, destination=destination))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": result})


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
    previous_mode = job.agent_loop.mode
    job.agent_loop.mode = normalized_mode
    job.agent_loop.enabled = normalized_mode != "off"
    job.job_kind = infer_job_kind(job.script_loop.enabled, normalized_mode)
    if wake_seconds is not None:
        job.agent_loop.wake_interval_seconds = wake_seconds
    elif (
        job.agent_loop.enabled
        and not job.agent_loop.wake_interval_seconds
        and not job.agent_loop.cron_expr
    ):
        # Jobs created with mode off carry wake_interval_seconds=null; an
        # enabled loop with no schedule crashes the recompile below.
        job.agent_loop.wake_interval_seconds = default_wake_seconds(normalized_mode)
    store.save(job)
    # Journal the operator's selection so a later revert (e.g. a stale
    # candidate promotion) is diagnosable from the job dir alone.
    store.append_journal(
        job_id,
        {
            "type": "operator_agent_mode_set",
            "from": previous_mode,
            "to": normalized_mode,
            "wake_seconds": wake_seconds,
            "via": "cli",
        },
    )
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


def _launch_with_proposal(
    store: JobStore, job_id: str, proposal_id: str, proposal: dict[str, Any]
) -> None:
    # Deterministic apply for gated proposals (claim + detached completer);
    # ungated proposals fall back to an agent wake that claims for itself.
    application = launch_application(store, job_id, proposal_id)
    sync_all_jobs(store=store)
    _echo_json(
        {
            "ok": True,
            "result": {
                "proposal": proposal,
                "application": application,
                "wakeup": application.get("wakeup"),
            },
        }
    )


@job_cli.command(name="approve", help="Approve a pending proposal.")
@click.argument("job_id")
@click.argument("proposal_id")
@click.option(
    "--skip-gate",
    is_flag=True,
    default=False,
    help="Skip the legacy-contract gate check (not recommended).",
)
@click.option(
    "--allow-ungated",
    is_flag=True,
    default=False,
    help=(
        "Approve a jobs_v1 proposal without a green candidate_report "
        "(human escape hatch for hand-written proposals)."
    ),
)
def approve_cmd(
    job_id: str, proposal_id: str, skip_gate: bool, allow_ungated: bool
) -> None:
    store = JobStore()
    # The SDK is the authoritative gate even when the backend is bypassed:
    # legacy jobs cannot pass the versioned-change flow.
    try:
        ensure_jobs_v1_contract(store, job_id, allow_legacy=skip_gate)
    except ValueError as exc:
        _echo_json({"ok": False, "error": str(exc)})
        raise click.ClickException(
            "legacy jobs cannot enter the versioned-change flow"
        ) from exc
    _launch_with_proposal(
        store,
        job_id,
        proposal_id,
        store.approve_proposal(job_id, proposal_id, allow_ungated=allow_ungated),
    )


@job_cli.command(
    name="propose",
    help="Create a proposal with a validated pre-approval candidate "
    "(candidate_report + baseline-vs-candidate comparison).",
)
@click.argument("job_id")
@click.option(
    "--kind",
    type=click.Choice(["code_change", "params_update", "model_update"]),
    required=True,
)
@click.option("--summary", required=True, help="One-line change summary.")
@click.option(
    "--intent-json",
    required=True,
    help="Intent contract JSON (all seven required fields).",
)
@click.option(
    "--params-json",
    default=None,
    help="Execution params to merge into the candidate job.yaml.",
)
@click.option(
    "--candidate-dir",
    default=None,
    help="Pre-edited candidate (bundle with workspace/ or bare workspace).",
)
@click.option("--scenario-json", default=None, help="Explicit scenario plan JSON.")
@click.option(
    "--improver-json",
    default=None,
    help="Full proposed improver spec JSON (kind=improver_change only).",
)
@click.option("--proposal-id", default=None)
@click.option(
    "--ack-robustness-warning",
    "robustness_warnings_acknowledged",
    multiple=True,
    help="Acknowledge an exact robustness warning code (repeatable).",
)
@click.option(
    "--acceptance-policy",
    type=click.Choice(["economic_improvement", "behavior_equivalence"]),
    default="economic_improvement",
    show_default=True,
    help="Use behavior_equivalence only for implementation-only code refactors.",
)
@click.option("--memo", default=None, help="Markdown proposal memo (inline).")
@click.option(
    "--memo-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Markdown proposal memo (file path).",
)
def propose_cmd(
    job_id: str,
    kind: str,
    summary: str,
    intent_json: str,
    params_json: str | None,
    candidate_dir: str | None,
    scenario_json: str | None,
    improver_json: str | None,
    proposal_id: str | None,
    robustness_warnings_acknowledged: tuple[str, ...],
    acceptance_policy: str,
    memo: str | None,
    memo_file: str | None,
) -> None:
    if memo_file:
        memo = Path(memo_file).read_text(encoding="utf-8")
    store = JobStore()
    proposal = propose_change(
        store,
        job_id,
        kind=kind,
        summary=summary,
        intent_contract=json.loads(intent_json),
        params=json.loads(params_json) if params_json else None,
        candidate_source=candidate_dir,
        scenario_plan=json.loads(scenario_json) if scenario_json else None,
        improver=json.loads(improver_json) if improver_json else None,
        proposal_id=proposal_id,
        memo=memo,
        robustness_warnings_acknowledged=list(robustness_warnings_acknowledged),
        acceptance_policy=acceptance_policy,
    )
    _echo_json({"ok": True, "result": proposal})


@job_cli.command(
    name="restage",
    help="Re-stage an approved proposal whose candidate went stale under an "
    "intervening apply; re-runs the propose-time gates and auto-queues the "
    "apply (approval carryover).",
)
@click.argument("job_id")
@click.argument("proposal_id")
@click.option(
    "--candidate-dir",
    default=None,
    help="Re-authored change against the CURRENT workspace (bundle with "
    "workspace/ or bare workspace). Required for code changes; params "
    "updates re-stage mechanically.",
)
def restage_cmd(job_id: str, proposal_id: str, candidate_dir: str | None) -> None:
    store = JobStore()
    proposal = restage_proposal(
        store, job_id, proposal_id, candidate_source=candidate_dir
    )
    _echo_json({"ok": True, "result": proposal})


@job_cli.command(
    name="revalidate",
    help="Re-run validation/comparison for a PENDING proposal against its "
    "same staged candidate and base revision, replacing the embedded "
    "candidate_report (recovery for reports frozen by a transient "
    "infrastructure failure).",
)
@click.argument("job_id")
@click.argument("proposal_id")
def revalidate_cmd(job_id: str, proposal_id: str) -> None:
    store = JobStore()
    proposal = revalidate_proposal(store, job_id, proposal_id)
    _echo_json({"ok": True, "result": proposal})


@job_cli.command(name="reject", help="Reject a pending proposal.")
@click.argument("job_id")
@click.argument("proposal_id")
@click.option("--reason", default=None, help="Why (recorded on the proposal).")
@click.option(
    "--by",
    "rejected_by",
    type=click.Choice(["owner", "agent"]),
    default="owner",
    show_default=True,
    help="Who is rejecting: owner vetoes bind the worker; agent = housekeeping.",
)
@click.option(
    "--kind",
    type=click.Choice(["process", "substantive"]),
    default=None,
    help="process = mechanics (superseded/re-stage), successor expected; "
    "substantive = verdict on the change. Default: inferred from --reason.",
)
def reject_cmd(
    job_id: str,
    proposal_id: str,
    reason: str | None,
    rejected_by: str,
    kind: str | None,
) -> None:
    store = JobStore()
    proposal = store.reject_proposal(
        job_id, proposal_id, reason=reason, rejected_by=rejected_by, kind=kind
    )
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": proposal})


@job_cli.group(
    name="exhaustion",
    help="Evidence-adjudicated exhaustion claims: agents FILE; the watchdog "
    "audits structured coverage and applies pass/narrow/reject. Manual "
    "acceptance and audit reopen remain owner-only.",
)
def exhaustion_group() -> None:
    pass


@exhaustion_group.command(name="file", help="File an exhaustion claim (agent-legal).")
@click.argument("job_id")
@click.option("--lane", required=True, help="Lane/region claimed exhausted.")
@click.option("--evidence", required=True, help="Evidence summary (test counts, refs).")
@click.option(
    "--provenance",
    type=click.Choice(sorted(CLAIM_PROVENANCES)),
    required=True,
    help="What closed the lane. agent-self-rejected can never settle a lane.",
)
@click.option(
    "--next-region",
    required=True,
    help="Proposed next region to open if the claim is accepted.",
)
@click.option("--ref", "refs", multiple=True, help="Artifact refs (repeatable).")
def exhaustion_file_cmd(
    job_id: str,
    lane: str,
    evidence: str,
    provenance: str,
    next_region: str,
    refs: tuple[str, ...],
) -> None:
    store = JobStore()
    claim = file_exhaustion_claim(
        store,
        job_id,
        lane=lane,
        evidence=evidence,
        provenance=provenance,
        next_region=next_region,
        refs=list(refs),
    )
    _echo_json({"ok": True, "result": claim})


@exhaustion_group.command(
    name="adjudicate", help="Accept or reject a pending claim (accept is owner-only)."
)
@click.argument("job_id")
@click.argument("claim_id")
@click.option("--status", type=click.Choice(["accepted", "rejected"]), required=True)
@click.option(
    "--by",
    "adjudicated_by",
    type=click.Choice(["owner", "agent"]),
    default="owner",
    show_default=True,
    help="Who is adjudicating: accepting a claim requires by='owner'.",
)
@click.option("--note", default=None, help="Adjudication note.")
def exhaustion_adjudicate_cmd(
    job_id: str, claim_id: str, status: str, adjudicated_by: str, note: str | None
) -> None:
    store = JobStore()
    try:
        claim = adjudicate_exhaustion_claim(
            store, job_id, claim_id, status=status, by=adjudicated_by, note=note
        )
    except PermissionError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": claim})


@exhaustion_group.command(
    name="audit", help="Run and apply the mechanical coverage audit immediately."
)
@click.argument("job_id")
@click.argument("claim_id")
def exhaustion_audit_cmd(job_id: str, claim_id: str) -> None:
    store = JobStore()
    claim = audit_and_adjudicate_exhaustion_claim(store, job_id, claim_id)
    _echo_json({"ok": True, "result": claim})


@exhaustion_group.command(
    name="reopen", help="Owner override of an audit-settled claim within 48 hours."
)
@click.argument("job_id")
@click.argument("claim_id")
@click.option(
    "--by",
    "reopened_by",
    type=click.Choice(["owner", "agent"]),
    default="owner",
    show_default=True,
)
@click.option("--reason", required=True, help="Why the audited closure is reversed.")
def exhaustion_reopen_cmd(
    job_id: str, claim_id: str, reopened_by: str, reason: str
) -> None:
    store = JobStore()
    try:
        claim = reopen_exhaustion_claim(
            store, job_id, claim_id, by=reopened_by, reason=reason
        )
    except PermissionError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": claim})


@exhaustion_group.command(name="list", help="List exhaustion claims.")
@click.argument("job_id")
@click.option(
    "--status",
    type=click.Choice(sorted(CLAIM_STATUSES)),
    default=None,
    help="Filter by status.",
)
def exhaustion_list_cmd(job_id: str, status: str | None) -> None:
    store = JobStore()
    _echo_json(
        {"ok": True, "result": list_exhaustion_claims(store, job_id, status=status)}
    )


@job_cli.command(
    name="probation-open-paper",
    help="Open a PAPER probation leg for a candidate that is not clearly "
    "worse than baseline (regression budget + trade floor checked "
    "mechanically). Paper only — never live sizing; graduation to live "
    "keeps the full strict gate + owner approval.",
)
@click.argument("job_id")
@click.option("--name", required=True, help="Leg name (unique per job).")
@click.option("--symbol", required=True)
@click.option(
    "--proposal-id",
    default=None,
    help="Source candidate/baseline net_return from this proposal's "
    "propose-time comparison.",
)
@click.option("--candidate-net", type=float, default=None)
@click.option("--baseline-net", type=float, default=None)
@click.option("--backtest-trades", type=int, default=None)
@click.option(
    "--kill-criterion",
    default="registered kill predicates + mechanical flat-zero floor",
    show_default=True,
)
@click.option("--kill-rules-json", default=None, help="Typed kill predicates as JSON.")
@click.option("--notes", default=None)
def probation_open_paper_cmd(
    job_id: str,
    name: str,
    symbol: str,
    proposal_id: str | None,
    candidate_net: float | None,
    baseline_net: float | None,
    backtest_trades: int | None,
    kill_criterion: str,
    kill_rules_json: str | None,
    notes: str | None,
) -> None:
    store = JobStore()
    try:
        leg = open_paper_probation_leg(
            store,
            job_id,
            name=name,
            symbol=symbol,
            kill_criterion=kill_criterion,
            kill_rules=json.loads(kill_rules_json) if kill_rules_json else None,
            proposal_id=proposal_id,
            candidate_net=candidate_net,
            baseline_net=baseline_net,
            backtest_trades=backtest_trades,
            notes=notes,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": leg})


@job_cli.command(name="apply-proposal", help="Queue apply for an approved proposal.")
@click.argument("job_id")
@click.argument("proposal_id")
def apply_proposal_cmd(job_id: str, proposal_id: str) -> None:
    store = JobStore()
    _launch_with_proposal(
        store,
        job_id,
        proposal_id,
        store.queue_proposal_application(job_id, proposal_id),
    )


@job_cli.command(
    name="rollback-apply",
    help="Owner undo for an applied proposal: restore the pre-apply snapshot "
    "from the promotion backup. Refused if the workspace has moved since.",
)
@click.argument("job_id")
@click.argument("proposal_id")
@click.option("--by", "rolled_back_by", default="owner", show_default=True)
def rollback_apply_cmd(job_id: str, proposal_id: str, rolled_back_by: str) -> None:
    store = JobStore()
    try:
        result = rollback_application(store, job_id, proposal_id, by=rolled_back_by)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": result})


@job_cli.group(
    name="decision-gate",
    help="Pre-registered rework-vs-retire gates: register criteria + a scoped "
    "successor up front; the watchdog auto-resolves PAPER jobs mechanically "
    "when the criteria are met, live-capable jobs escalate to the owner. "
    "(`gate` was taken by live-gate evaluation, hence the longer name.)",
)
def decision_gate_group() -> None:
    pass


@decision_gate_group.command(
    name="register", help="Pre-register a decision gate (criteria + successor)."
)
@click.argument("job_id")
@click.option(
    "--criteria",
    required=True,
    help='JSON criteria, e.g. \'{"min_trades": 20, "max_win_rate": 0.4, '
    '"max_net_pnl": 0}\'.',
)
@click.option(
    "--successor-ref",
    required=True,
    help="Scoped successor the pivot opens (lane/region/proposal ref).",
)
@click.option("--gate-id", default=None, help="Explicit gate id (default: random).")
@click.option("--by", "registered_by", default="improver", show_default=True)
def decision_gate_register_cmd(
    job_id: str,
    criteria: str,
    successor_ref: str,
    gate_id: str | None,
    registered_by: str,
) -> None:
    store = JobStore()
    try:
        gate = register_decision_gate(
            store,
            job_id,
            criteria=json.loads(criteria),
            successor_ref=successor_ref,
            gate_id=gate_id,
            registered_by=registered_by,
        )
    except (ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": gate})


@decision_gate_group.command(
    name="resolve",
    help="Owner resolution of a tripped/armed gate; --execute runs the same "
    "bounded retire flow the paper auto path uses (also on a gate previously "
    "resolved as acknowledge-only). Retries of a settled gate are a no-op.",
)
@click.argument("job_id")
@click.argument("gate_id")
@click.option("--by", "resolved_by", default="owner", show_default=True)
@click.option("--note", default=None)
@click.option("--execute", is_flag=True, default=False)
def decision_gate_resolve_cmd(
    job_id: str, gate_id: str, resolved_by: str, note: str | None, execute: bool
) -> None:
    store = JobStore()
    try:
        gate = resolve_decision_gate(
            store, job_id, gate_id, by=resolved_by, note=note, execute=execute
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "noop": bool(gate.pop("noop", False)), "result": gate})


@decision_gate_group.command(
    name="reopen",
    help="Undo an auto-resolution (re-enables the script loop) or dismiss a "
    "trip. The gate stays reopened until explicitly re-registered; reopening "
    "an already-reopened gate is a no-op.",
)
@click.argument("job_id")
@click.argument("gate_id")
@click.option("--by", "reopened_by", default="owner", show_default=True)
def decision_gate_reopen_cmd(job_id: str, gate_id: str, reopened_by: str) -> None:
    store = JobStore()
    try:
        gate = reopen_decision_gate(store, job_id, gate_id, by=reopened_by)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "noop": bool(gate.pop("noop", False)), "result": gate})


@decision_gate_group.command(name="list", help="List registered decision gates.")
@click.argument("job_id")
def decision_gate_list_cmd(job_id: str) -> None:
    store = JobStore()
    _echo_json({"ok": True, "result": load_decision_gates(store, job_id)})


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


@job_cli.command(
    name="recover-stalled-applications",
    help="Scan every job for stalled proposal applications (applying with a "
    "dead completer, or approved+queued with no spawn) and drive them to a "
    "terminal status, resuming paused runner loops. Same primitive the "
    "runner watchdog job runs every few minutes.",
)
def recover_stalled_applications_cmd() -> None:
    from wayfinder_paths.jobs.watchdog import recover_stalled_applications

    _echo_json({"ok": True, "result": recover_stalled_applications()})


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
    # The synced flag is what buckets the job into the UI's paused section —
    # runner-link state alone never reaches the frontend.
    store.refresh_scorecard(job_id, {"paused": action == "pause"})
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": responses})


@job_cli.command(
    name="pause",
    help="Pause a job's runner loops (script + agent). Refuses while the job "
    "is LIVE with declared capital — a paused live job leaves venue money "
    "with nothing managing it; withdraw first (or --force).",
)
@click.argument("job_id")
@click.option("--force", is_flag=True, default=False)
def pause_cmd(job_id: str, force: bool) -> None:
    store = JobStore()
    job = store.load(job_id)
    capital = float(job.execution_params.get("initial_capital") or 0.0)
    if not force and job.script_loop.mode == "live" and capital > 0:
        raise click.ClickException(
            f"job is live with declared capital ${capital:g} — withdraw the "
            "bankroll first so no venue money sits unmanaged, or pass --force"
        )
    _pause_resume_loops(job_id, "pause")


@job_cli.command(name="resume", help="Resume a job's runner loops.")
@click.argument("job_id")
def resume_cmd(job_id: str) -> None:
    _pause_resume_loops(job_id, "resume")


@job_cli.command(
    name="lifecycle-sweep",
    help="Run the fleet lifecycle sweep now: bootstrap nudge/park for "
    "never-operational jobs plus monitor-decay park. The watchdog runs this "
    "daily; --force bypasses the throttle.",
)
@click.option("--force", is_flag=True, default=False, help="Bypass the daily throttle.")
def lifecycle_sweep_cmd(force: bool) -> None:
    _echo_json({"ok": True, "result": lifecycle_sweep(JobStore(), force=force)})


@job_cli.group(
    name="feature",
    help="Exogenous feature rows consumed by decide() via ctx.view.feature().",
)
def feature_group() -> None:
    pass


@feature_group.command(name="append", help="Append a feature row.")
@click.argument("job_id")
@click.option("--name", required=True)
@click.option("--value", required=True)
@click.option("--timestamp", default=None, help="ISO8601; defaults to now.")
@click.option("--symbol", default=None, help="Per-symbol feature rows.")
def feature_append_cmd(
    job_id: str,
    name: str,
    value: str,
    timestamp: str | None,
    symbol: str | None,
) -> None:
    coerced: Any = value
    try:
        coerced = float(value)
    except ValueError:
        pass
    store = JobStore()
    row = append_feature(
        store, job_id, name=name, value=coerced, timestamp=timestamp, symbol=symbol
    )
    _echo_json({"ok": True, "result": row})


@feature_group.command(name="list", help="List recent feature rows.")
@click.argument("job_id")
@click.option("--name", default=None)
@click.option("--limit", type=int, default=50, show_default=True)
def feature_list_cmd(job_id: str, name: str | None, limit: int) -> None:
    store = JobStore()
    _echo_json(
        {"ok": True, "result": list_features(store, job_id, name=name, limit=limit)}
    )


@job_cli.group(
    name="ledger",
    help="Append-only job ledgers (candidates, decisions) for agent loops.",
)
def ledger_group() -> None:
    pass


@ledger_group.command(name="append", help="Append a row to a job ledger.")
@click.argument("job_id")
@click.argument("name")
@click.option("--json", "row_json", required=True, help="Row object as JSON.")
def ledger_append_cmd(job_id: str, name: str, row_json: str) -> None:
    store = JobStore()
    row = json.loads(row_json)
    result = append_ledger_row(store, job_id, name, row)
    _echo_json({"ok": True, "result": result})


@ledger_group.command(name="tail", help="Read the most recent ledger rows.")
@click.argument("job_id")
@click.argument("name")
@click.option("--limit", type=int, default=20, show_default=True)
def ledger_tail_cmd(job_id: str, name: str, limit: int) -> None:
    store = JobStore()
    _echo_json({"ok": True, "result": tail_ledger(store, job_id, name, limit=limit)})


@job_cli.command(
    name="halt",
    help="Kill switch: force reduce-only immediately (optionally flatten).",
)
@click.argument("job_id")
@click.option("--reason", default=None, help="Why the job is being halted.")
@click.option(
    "--flatten",
    is_flag=True,
    default=False,
    help="Also market-close all open positions on the next tick.",
)
def halt_cmd(job_id: str, reason: str | None, flatten: bool) -> None:
    store = JobStore()
    payload = request_halt(store, job_id, reason=reason, flatten=flatten)
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": payload})


@job_cli.command(name="resume-from-halt", help="Clear a manual halt.")
@click.argument("job_id")
@click.option(
    "--by",
    "cleared_by",
    type=click.Choice(["owner", "agent"]),
    default="owner",
    show_default=True,
    help="Who is clearing: risk/protection-latched halts are owner-only.",
)
def resume_from_halt_cmd(job_id: str, cleared_by: str) -> None:
    store = JobStore()
    try:
        payload = clear_halt(store, job_id, by=cleared_by)
    except PermissionError as exc:
        raise click.ClickException(str(exc)) from exc
    sync_all_jobs(store=store)
    _echo_json({"ok": True, "result": payload})


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
