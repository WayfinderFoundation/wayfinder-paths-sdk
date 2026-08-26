from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Literal

from wayfinder_paths.jobs.application import (
    claim_application,
    complete_application,
    ensure_jobs_v1_contract,
    validate_application_candidate,
)
from wayfinder_paths.jobs.apply_launcher import launch_application
from wayfinder_paths.jobs.backtest_artifacts import diagnose_backtest
from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.execution.experiments import list_experiments
from wayfinder_paths.jobs.execution.validation import validate_execution_job
from wayfinder_paths.jobs.halt import clear_halt, request_halt
from wayfinder_paths.jobs.models import (
    WayfinderJob,
    infer_job_kind,
    normalize_agent_mode,
    utc_now_iso,
)
from wayfinder_paths.jobs.proposals import propose_change
from wayfinder_paths.jobs.regime_health import regime_health_job
from wayfinder_paths.jobs.runner_bridge import RunnerBridge
from wayfinder_paths.jobs.starters import create_starter_job, starter_catalog
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.strategies import library_catalog
from wayfinder_paths.jobs.sync import (
    apply_script_mode,
    snapshot_job,
    sync_all_jobs,
    venue_deposit,
    venue_withdraw,
)
from wayfinder_paths.jobs.worker import run_job_worker
from wayfinder_paths.mcp.utils import catch_errors, err, ok
from wayfinder_paths.runner.monitor_state import atomic_write_json

JobAction = Literal[
    "list",
    "starter_strategies",
    "create_starter",
    "create",
    "status",
    "report",
    "regime_health",
    "remediation_progress",
    "set_agent_mode",
    "set_script_mode",
    "venue_deposit",
    "venue_withdraw",
    "review_now",
    "validate_job",
    "fetch_dataset",
    "fetch_funding",
    "pair_check",
    "signal_check",
    "signal_scan",
    "chart",
    "analogs",
    "attribution",
    "derive_features",
    "holdout_check",
    "rank_check",
    "strategy_library",
    "backtest_job",
    "op_status",
    "backtest_diagnose",
    "experiments",
    "robustness_check",
    "evolution_start",
    "evolution_status",
    "evolution_prepare",
    "evolution_evaluate",
    "evolution_finalize",
    "forward_experience",
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


async def _run_job_op(op: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Run a heavy jobs operation in an isolated child process.

    Backtests/experiments must NOT run inside this long-lived MCP server
    process: GIL contention with the event loop slows the tick loop ~28x, and
    the memory spike can get the whole server OOM-killed — which silently
    drops every wayfinder tool for the session (opencode never reconnects).
    A child process keeps the server responsive and turns a killed run into a
    clean tool error. See op_runner for the protocol.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "wayfinder_paths.jobs.execution.op_runner",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(
        json.dumps({"op": op, "kwargs": kwargs}).encode()
    )
    if proc.returncode != 0:
        lines = (stderr or b"").decode(errors="replace").strip().splitlines()
        tail = " | ".join(lines[-3:]) if lines else None
        if proc.returncode < 0:  # killed by signal — on this box, usually OOM
            return err(
                "job_op_killed",
                f"{op} process was killed (signal {-proc.returncode}) — likely "
                "out of memory; retry with fewer bars (quick_bars) or a smaller "
                "grid",
                tail,
            )
        return err("job_op_failed", f"{op} failed (exit {proc.returncode})", tail)
    return ok(json.loads(stdout.decode()))


# Reaper tasks must be referenced or the event loop may GC them mid-await.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


def _background_ops_dir(store: JobStore, job_id: str) -> Path:
    return store.job_dir(job_id) / "state" / "background_ops"


def _op_status_hint(job_id: str, op: str) -> str:
    return (
        f"core_jobs(action='op_status', job_id='{job_id}', op='{op}') — "
        "poll every ~60s (bash sleep between checks); the run survives this "
        "request ending."
    )


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


async def _start_background_op(
    store: JobStore, job_id: str, op: str, kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Detached variant of _run_job_op: spawn the same op_runner child but
    return immediately, leaving a status file for op_status to poll.

    A synchronous backtest cannot fit through the MCP request window on this
    box (observed live: client timed out at 300s, the run died with it, and
    the memory spike OOM-killed the conversation server). Detached, the run
    survives the request, the client, and even an MCP server restart."""
    ops_dir = _background_ops_dir(store, job_id)
    ops_dir.mkdir(parents=True, exist_ok=True)
    status_path = ops_dir / f"{op}.json"
    existing = _load_json_file(status_path)
    if (
        existing
        and existing.get("state") == "running"
        and _pid_alive(existing.get("pid"))
    ):
        return ok(
            {
                "already_running": True,
                **existing,
                "check": _op_status_hint(job_id, op),
            }
        )

    log_path = ops_dir / f"{op}.log"
    result_path = ops_dir / f"{op}.result.json"
    result_path.unlink(missing_ok=True)
    with log_path.open("wb") as log_handle, result_path.open("wb") as result_handle:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "wayfinder_paths.jobs.execution.op_runner",
            stdin=asyncio.subprocess.PIPE,
            stdout=result_handle,
            stderr=log_handle,
            start_new_session=True,
        )
    assert proc.stdin is not None
    proc.stdin.write(json.dumps({"op": op, "kwargs": kwargs}).encode())
    await proc.stdin.drain()
    proc.stdin.close()
    # Best-effort island stamp: which research thread started this op, so the
    # scheduler's continuation check can keep the wake rotation on it.
    scheduler_state = _load_json_file(
        store.job_dir(job_id) / "state" / "scheduler.json"
    )
    history = (scheduler_state or {}).get("history") or []
    island = (
        history[-1].get("island") if history and isinstance(history[-1], dict) else None
    )
    status = {
        "op": op,
        "job_id": job_id,
        "state": "running",
        "pid": proc.pid,
        "started_at": utc_now_iso(),
        **({"island": island} if island else {}),
    }
    atomic_write_json(status_path, status)
    task = asyncio.get_running_loop().create_task(
        _reap_background_op(proc, status_path)
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return ok(
        {
            "started": True,
            "op": op,
            "pid": proc.pid,
            "note": (
                "running detached — this request is done; results land in the "
                "job dir as usual when the run finishes"
            ),
            "check": _op_status_hint(job_id, op),
        }
    )


async def _reap_background_op(proc: Any, status_path: Path) -> None:
    code = await proc.wait()
    status = _load_json_file(status_path) or {}
    status.update(
        {
            "state": "done" if code == 0 else ("killed" if code < 0 else "failed"),
            "exit_code": code,
            "finished_at": utc_now_iso(),
        }
    )
    atomic_write_json(status_path, status)


def _background_op_status(store: JobStore, job_id: str, op: str) -> dict[str, Any]:
    ops_dir = _background_ops_dir(store, job_id)
    status = _load_json_file(ops_dir / f"{op}.json")
    if not status:
        return err("not_found", f"no background {op} run recorded for {job_id}")
    if status.get("state") == "running" and not _pid_alive(status.get("pid")):
        # The reaper lived in an MCP server that restarted mid-run. The child
        # was detached (own session), so a parseable result file means it
        # finished anyway; otherwise the run is lost.
        result = _load_json_file(ops_dir / f"{op}.result.json")
        status["state"] = "done" if result is not None else "lost"
        atomic_write_json(ops_dir / f"{op}.json", status)
    payload = dict(status)
    log_path = ops_dir / f"{op}.log"
    if log_path.exists():
        lines = (
            log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        )
        payload["log_tail"] = lines[-5:]
    if payload.get("state") == "done":
        result = _load_json_file(ops_dir / f"{op}.result.json")
        if result is not None:
            payload["result"] = result
    elif payload.get("state") in ("failed", "killed", "lost"):
        payload["hint"] = (
            "see log_tail; killed/lost usually means OOM — retry with "
            "quick_bars or a smaller grid"
        )
    return ok(payload)


@catch_errors
async def core_jobs(
    action: JobAction,
    *,
    job_id: str | None = None,
    starter_id: str | None = None,
    initializer_session_id: str | None = None,
    leverage: int | None = None,
    name: str | None = None,
    goal: str | None = None,
    script: str | None = None,
    interval_seconds: int | None = None,
    cron_expr: str | None = None,
    timezone: str | None = None,
    timeout_seconds: int | None = None,
    agent_mode: Literal["off", "monitor", "intervene", "auto", "improve", "decide"]
    | None = None,
    script_mode: Literal["live", "paper"] | None = None,
    amount: float | None = None,
    destination: str | None = None,
    agent_wake_seconds: int | None = None,
    auto_limits: dict[str, Any] | None = None,
    execution_contract: Literal["jobs_v1", "legacy"] = "jobs_v1",
    proposal_id: str | None = None,
    application_status: Literal["applied", "failed"] | None = None,
    changed_files: list[str] | None = None,
    validation: dict[str, Any] | None = None,
    error: str | None = None,
    reason: str | None = None,
    remediation_state: Literal["evaluating", "blocked"] | None = None,
    remediation_note: str | None = None,
    artifact_path: str | None = None,
    flatten: bool = False,
    kind: str | None = None,
    summary: str | None = None,
    intent_contract: dict[str, Any] | None = None,
    execution_params: dict[str, Any] | None = None,
    candidate_dir: str | None = None,
    candidate_id: str | None = None,
    family: str | None = None,
    mutation_kind: Literal["structural", "parameter"] | None = None,
    scenario_plan: dict[str, Any] | None = None,
    improver: dict[str, Any] | None = None,
    memo: str | None = None,
    strict: bool = False,
    grid_path: str | None = None,
    grid: dict[str, Any] | list[dict[str, Any]] | None = None,
    robustness_plan: dict[str, Any] | None = None,
    robustness_warnings_acknowledged: list[str] | None = None,
    workers: int = 0,
    parallel: Literal["serial", "thread", "process"] = "process",
    compile: bool = True,  # noqa: A002
    full: bool = False,
    force: bool = False,
    quick_bars: int | None = None,
    background: bool | None = None,
    op: str | None = None,
    days: int = 14,
    include_funding: bool = False,
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
    symbols: list[str] | None = None,
    column: str | None = None,
    horizons: list[int] | None = None,
    bar_interval: str | None = None,
    direction: Literal["long", "short", "auto"] | None = None,
    timeframes: list[str] | None = None,
    holdout_fraction: float = 0.15,
    signal: str | None = None,
    horizon: int | None = None,
    symbol: str | None = None,
    campaign: str | None = None,
    condition_regime: bool = False,
    window_days: int | None = None,
    timeframe: str | None = None,
    bars: int = 96,
    indicators: list[str] | None = None,
    around_trade: str | None = None,
    window: int = 24,
    at: str | None = None,
    top: int = 15,
) -> dict[str, Any]:
    """Manage high-level Wayfinder jobs.

    A Wayfinder job is a versioned local job bundle with an optional deterministic
    script loop and optional headless OpenCode worker loop. This tool is the
    user-facing control layer; recurring execution is still delegated to
    `core_runner`.

    Typical flow:
      - `create` with `script` + `interval_seconds` for script-only jobs.
        Jobs default to `execution_contract="jobs_v1"` (decide()/build_strategy
        driven by the SDK tick driver); pass `execution_contract="legacy"` only
        for a real standalone script that runs top-to-bottom.
      - `starter_strategies` lists the fixed, mixed crypto/equity paper
        starters. `create_starter` with `starter_id` materializes one as a
        normal jobs_v1 job; its own forward inception begins at selection.
        Catalog launches default to `agent_mode="intervene"` (agent loop ON,
        propose-only) — pass `agent_mode` explicitly only to deviate.
        It also spawns a detached 120-day `fetch_dataset` op — do not re-run
        `fetch_dataset` right after creating; poll `op_status` if a backtest
        reports the fetch still in progress.
      - `create` with `agent_mode="monitor"` or `"intervene"` for supervised jobs.
      - `create` with `agent_mode="auto"` and `auto_limits` for agent-only auto jobs.
      - `set_script_mode` with `script_mode="live"` / `"paper"` to flip the
        script loop between paper and live trading. This is the ONLY correct way
        to change execution mode: it edits `job.yaml` and recompiles, which
        re-bakes the runner env. Never hand-patch the runner env
        (`WAYFINDER_JOB_MODE` via `core_runner`) — the compiler owns that value
        and the next recompile silently reverts a hand-patch, leaving a
        paper/live split-brain. Going live is gated: the job must pass the live
        gate (fresh validation/backtest/preflight) and declare a
        `wallet_label`, else the call returns an error naming the blocker.
      - `set_agent_mode` to change the agent watch level (monitor/intervene/…).
      - `regime_health` returns the deterministic 7/14/30-day incumbent and
        market-state drift report. warning/critical refreshes attribution first;
        any automatic response comes only from protected owner governance.
      - `remediation_progress` records a bounded evaluation or blocker for an
        open regime-health case; it never closes the case or changes trading.
      - `review_now` to queue an immediate worker wakeup.
      - `approve_proposal` / `reject_proposal` after the worker creates proposals.
      - `claim_application` / `validate_application` / `complete_application`
        from an apply worker.
      - Strategy-development loop for execution-spec jobs: `signal_scan`
        (event-study the ENTIRE canonical trigger library — both directions,
        multi-timeframe via `timeframes=["1h","4h","1d"]`, BH q-values,
        4-fold stability, and a reserved holdout tail the scan never sees —
        in one call BEFORE hand-writing trigger variants; needs no strategy
        script), `holdout_check` (one-shot confirmation of a FROZEN scan
        candidate — signal + horizon + direction — on the reserved tail;
        spend it once per candidate, the trial ledger remembers),
        `strategy_library` (list the
        shipped reference strategies — verbatim ports of audited live
        scripts; when the user references a known/live strategy, start here
        instead of transcribing it from prose), `signal_check`
        (event-study a precomputed entry column BEFORE building — pass
        `direction` for a directional thesis, default "auto" reads the side
        from the t-stat sign; no edge at
        the signal level means the TRIGGER carries no information; a complete
        trade system can still earn its keep via gates/exits/regime — judge
        those by full backtest + walk-forward), `rank_check` (the
        basket analogue: Spearman rank IC of a cross-sectional ranking column
        vs relative forward returns — run BEFORE building any long/short
        basket on that ranking) and `pair_check` (the
        statistical admission gate for any pair/spread idea — run it FIRST; a
        REJECT saves days of tuning), `fetch_dataset` (real candles into the
        job; `dataset_source="ccxt"` + `exchange="binance"` for long history;
        use `include_funding=True` for same-window perp carry),
        `fetch_funding` (historical funding rates into the job's feature
        store — first-class carry data, as-of merged onto the bars as a
        `funding` column), `backtest_job` (runs DETACHED by
        default — it returns immediately; poll `op_status` until done, or
        pass `background=False` only for quick_bars-sized runs),
        `backtest_diagnose` (ranked next steps), `experiments` (param grid via
        `grid` inline or `grid_path`; pass `wf_test_bars`/`wf_folds` for
        walk-forward out-of-sample validation), `robustness_check` (detached
        advisory neighbor/phase/leverage/walk-forward/scenario evidence), then `promote_params`
        (`grid_id`/`run_id`) once it survives OOS.

        Funding: `venue_deposit` / `venue_withdraw` (amount, and destination
        for withdraw) are the ONLY sanctioned way to move a live job's
        bankroll — they bridge USDC via the job's bound wallet AND keep
        `execution_params.initial_capital` in lockstep, exactly like the
        owner's Fund/Withdraw buttons. Raw hyperliquid_deposit_usdc /
        hyperliquid_withdraw_usdc against a job-bound wallet moves money
        without the capital bookkeeping and de-syncs live sizing.
    """

    store = JobStore()

    if action == "list":
        return ok([snapshot_job(job.id, store=store) for job in store.list_jobs()])

    if action == "starter_strategies":
        return ok(starter_catalog())

    if action == "create_starter":
        if not starter_id:
            return err("invalid_request", "create_starter requires starter_id")
        return ok(
            create_starter_job(
                starter_id,
                job_id=job_id,
                store=store,
                compile_job=compile,
                initializer_session_id=initializer_session_id,
                leverage=leverage,
                agent_mode=agent_mode,
            )
        )

    if action == "sync":
        # Recompile runner links first: job.yaml is agent-editable (e.g.
        # flipping execution_contract), and a stale wrapper from create time
        # otherwise fails at schedule time, not edit time.
        recompiled: list[str] = []
        compile_errors: dict[str, str] = {}
        compiler = JobCompiler(store=store)
        for job in store.list_jobs():
            if not job.script_loop.enabled:
                continue
            try:
                compiler.compile(job, start_daemon=False)
                recompiled.append(job.id)
            except Exception as exc:  # one bad job must not block the sync
                compile_errors[job.id] = str(exc)
        sync_all_jobs(store=store)
        result: dict[str, Any] = {"synced": True, "recompiled": recompiled}
        if compile_errors:
            result["compile_errors"] = compile_errors
        return ok(result)

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
            execution_contract=execution_contract,
        )
        job_path = store.create_job(job)
        result: dict[str, Any] = {"job": job.to_dict(), "job_yaml": str(job_path)}
        entrypoint = store.resolve_script_entrypoint(job.id, job.to_dict())
        if entrypoint is not None:
            result["script_entrypoint"] = str(entrypoint)
            result["hint"] = (
                "write your decide()/build_strategy strategy at "
                "script_entrypoint — it always lives under the job's "
                "workspace/src/ because only workspace/ + job.yaml are "
                "revision-hashed and stageable by proposals; do not author "
                "strategy code at the job root or in .wayfinder_runs/"
            )
        if compile:
            result["compile"] = JobCompiler(store=store).compile(job)
            sync_all_jobs(store=store)
        return ok(result)

    if action in {"status", "report"}:
        return ok(snapshot_job(job_id, store=store))

    if action == "regime_health":
        return ok(regime_health_job(job_id, store=store, force=force))

    if action == "remediation_progress":
        if not remediation_state or not remediation_note:
            return err(
                "invalid_request",
                "remediation_progress requires remediation_state and remediation_note",
            )
        from wayfinder_paths.jobs.remediation import update_remediation_progress

        return ok(
            update_remediation_progress(
                store,
                job_id,
                state=remediation_state,
                note=remediation_note,
                artifact_path=artifact_path,
            )
        )

    if action == "set_agent_mode":
        mode = normalize_agent_mode(agent_mode or "monitor")
        job = store.load(job_id)
        previous_mode = job.agent_loop.mode
        job.agent_loop.mode = mode
        job.agent_loop.enabled = mode != "off"
        job.job_kind = infer_job_kind(job.script_loop.enabled, mode)
        if agent_wake_seconds is not None:
            job.agent_loop.wake_interval_seconds = agent_wake_seconds
        store.save(job)
        # Journal the operator's selection so a later revert (e.g. a stale
        # candidate promotion) is diagnosable from the job dir alone.
        store.append_journal(
            job_id,
            {
                "type": "operator_agent_mode_set",
                "from": previous_mode,
                "to": mode,
                "wake_seconds": agent_wake_seconds,
                "via": "mcp",
            },
        )
        result = JobCompiler(store=store).compile(job)
        sync_all_jobs(store=store)
        return ok(result)

    if action == "set_script_mode":
        if script_mode is None:
            return err(
                "invalid_argument",
                "set_script_mode requires script_mode='live' or 'paper'",
            )
        try:
            # MCP callers are agents (wake workers, conversation sessions);
            # the operator surfaces (CLI, backend button) default to "owner".
            return ok(
                apply_script_mode(job_id, script_mode, store=store, set_by="agent")
            )
        except ValueError as exc:
            # Live-gate / wallet blockers name the missing precondition; surface
            # them as an actionable error rather than a generic failure.
            return err("script_mode_blocked", str(exc))

    if action in {"venue_deposit", "venue_withdraw"} and not job_id:
        return err("invalid_request", f"{action} requires job_id")

    if action == "venue_deposit":
        # THE canonical way to fund a live job — the same code path the
        # owner's Fund button runs: bridges USDC from the job's bound wallet
        # into the venue AND records initial_capital in lockstep (first fund
        # replaces the paper default, later funds add). Never fund a
        # job-bound wallet with raw hyperliquid_deposit_usdc: money would
        # move without the capital/marker writes and sizing drifts.
        if amount is None or amount <= 0:
            return err("invalid_argument", "venue_deposit requires amount > 0")
        try:
            return ok(await venue_deposit(job_id, float(amount), store=store))
        except ValueError as exc:
            return err("venue_deposit_failed", str(exc))

    if action == "venue_withdraw":
        # Counterpart of venue_deposit: withdraws from the venue (to
        # `destination`, default the bound wallet) and shrinks
        # initial_capital by the gross amount, floored at zero.
        if amount is None or amount <= 0:
            return err("invalid_argument", "venue_withdraw requires amount > 0")
        try:
            return ok(
                await venue_withdraw(
                    job_id, float(amount), destination=destination, store=store
                )
            )
        except ValueError as exc:
            return err("venue_withdraw_failed", str(exc))

    if action == "review_now":
        mode = normalize_agent_mode(agent_mode or "monitor")
        if mode == "off":
            mode = "monitor"
        return ok(run_job_worker(job_id, mode=mode, apply_proposal_id=proposal_id))

    if action == "validate_job":
        return ok(validate_execution_job(job_id, strict=strict, store=store))

    if action == "fetch_dataset":
        return await _run_job_op(
            "fetch_dataset",
            {
                "job_id": job_id,
                "days": days,
                "source": dataset_source,
                "exchange": exchange,
                "market_type": market_type,
                "quote": quote,
                "include_funding": include_funding,
            },
        )

    if action == "fetch_funding":
        return await _run_job_op(
            "fetch_funding",
            {"job_id": job_id, "days": days, "exchange": exchange, "quote": quote},
        )

    if action == "pair_check":
        # Admission gate for any two-legged idea: run BEFORE building a pair/
        # spread strategy. days defaults to 14 in this signature for dataset
        # fetches; pair statistics need years, so widen unless caller set it.
        return await _run_job_op(
            "pair_check",
            {
                "job_id": job_id,
                "symbols": symbols,
                "days": days if days != 14 else 720,
                "bar_interval": bar_interval,
                "exchange": exchange,
            },
        )

    if action == "signal_check":
        if not column:
            return err("invalid_request", "signal_check requires column")
        return await _run_job_op(
            "signal_check",
            {
                "job_id": job_id,
                "column": column,
                "horizons": horizons,
                "direction": direction or "auto",
            },
        )

    if action == "signal_scan":
        return await _run_job_op(
            "signal_scan",
            {
                "job_id": job_id,
                "symbols": symbols,
                "horizons": horizons,
                "timeframes": timeframes,
                "holdout_fraction": holdout_fraction,
                "campaign": campaign,
                "condition_regime": condition_regime,
                "window_days": window_days,
            },
        )

    if action == "derive_features":
        return await _run_job_op("derive_features", {"job_id": job_id})

    if action == "attribution":
        return await _run_job_op("attribution", {"job_id": job_id})

    if action == "chart":
        return await _run_job_op(
            "chart",
            {
                "job_id": job_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "bars": bars,
                "indicators": indicators,
                "around_trade": around_trade,
            },
        )

    if action == "analogs":
        return await _run_job_op(
            "analogs",
            {
                "job_id": job_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "window": window,
                "at": at,
                "top": top,
                "horizon": horizon if horizon is not None else 12,
            },
        )

    if action == "holdout_check":
        if not signal or horizon is None or direction not in {"long", "short"}:
            return err(
                "invalid_request",
                "holdout_check requires signal, horizon, and direction "
                "long|short (a frozen candidate is directional)",
            )
        return await _run_job_op(
            "holdout_check",
            {
                "job_id": job_id,
                "signal": signal,
                "horizon": horizon,
                "direction": direction,
                "timeframe": bar_interval,
                "symbols": symbols,
                "holdout_fraction": holdout_fraction,
            },
        )

    if action == "strategy_library":
        return ok(library_catalog())

    if action == "rank_check":
        if not column:
            return err("invalid_request", "rank_check requires column")
        return await _run_job_op(
            "rank_check",
            {"job_id": job_id, "column": column, "horizons": horizons},
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
        experiments_kwargs = {
            "job_id": job_id,
            "grid": chosen_grid,
            "rank_by": rank_by,
            "workers": workers,
            "parallel": parallel,
            "walk_forward": walk_forward,
            "quick_bars": quick_bars,
            "full": full,
        }
        # Grid sweeps dwarf single backtests — same detached escape hatch,
        # opt-in here (inline quick grids stay synchronous by default).
        if background:
            if not job_id:
                return err("invalid_request", "experiments requires job_id")
            return await _start_background_op(
                store, job_id, "experiments", experiments_kwargs
            )
        return await _run_job_op("experiments", experiments_kwargs)

    if action == "robustness_check":
        if not job_id:
            return err("invalid_request", "robustness_check requires job_id")
        kwargs = {
            "job_id": job_id,
            "candidate_dir": candidate_dir,
            "robustness_plan": robustness_plan,
        }
        if background is not False:
            return await _start_background_op(store, job_id, "robustness_check", kwargs)
        return await _run_job_op("robustness_check", kwargs)

    if action == "evolution_start":
        if not job_id:
            return err("invalid_request", "evolution_start requires job_id")
        return await _run_job_op("evolution_start", {"job_id": job_id, "force": force})

    if action == "evolution_status":
        if not job_id:
            return err("invalid_request", "evolution_status requires job_id")
        from wayfinder_paths.jobs.evolution_campaign import campaign_status

        return ok(campaign_status(store, job_id))

    if action == "evolution_prepare":
        if not job_id or not family or not summary:
            return err(
                "invalid_request",
                "evolution_prepare requires job_id, family, and summary",
            )
        return await _run_job_op(
            "evolution_prepare",
            {
                "job_id": job_id,
                "family": family,
                "summary": summary,
                "mutation_kind": mutation_kind,
            },
        )

    if action == "evolution_evaluate":
        if not job_id or not candidate_id:
            return err(
                "invalid_request", "evolution_evaluate requires job_id and candidate_id"
            )
        kwargs = {"job_id": job_id, "candidate_id": candidate_id}
        if background is not False:
            return await _start_background_op(
                store, job_id, "evolution_evaluate", kwargs
            )
        return await _run_job_op("evolution_evaluate", kwargs)

    if action == "evolution_finalize":
        if not job_id:
            return err("invalid_request", "evolution_finalize requires job_id")
        kwargs = {"job_id": job_id}
        if background is not False:
            return await _start_background_op(
                store, job_id, "evolution_finalize", kwargs
            )
        return await _run_job_op("evolution_finalize", kwargs)

    if action == "forward_experience":
        if not job_id:
            return err("invalid_request", "forward_experience requires job_id")
        kwargs = {"job_id": job_id}
        if background is not False:
            return await _start_background_op(store, job_id, "forward_experience", kwargs)
        return await _run_job_op("forward_experience", kwargs)

    if action == "promote_params":
        return await _run_job_op(
            "promote_params",
            {
                "job_id": job_id,
                "grid_id": grid_id,
                "run_id": run_id,
                "params": execution_params,
                "via_proposal": via_proposal,
            },
        )

    if action == "backtest_job":
        # The child summarizes unless full=True — the compact summary is ~2 KB
        # vs ~8 MB of per-bar arrays (all persisted under results/backtest/).
        backtest_kwargs = {
            "job_id": job_id,
            "grid_path": grid_path,
            "workers": workers,
            "parallel": parallel,
            "quick_bars": quick_bars,
            "full": full,
        }
        # Detached by default: a full backtest cannot fit through the MCP
        # request window (client timeout kills the run mid-grind). Sync only
        # on explicit background=False for quick_bars-sized runs.
        if background is False:
            return await _run_job_op("backtest_job", backtest_kwargs)
        if not job_id:
            return err("invalid_request", "backtest_job requires job_id")
        return await _start_background_op(
            store, job_id, "backtest_job", backtest_kwargs
        )

    if action == "op_status":
        if not job_id:
            return err("invalid_request", "op_status requires job_id")
        return _background_op_status(store, job_id, op or "backtest_job")

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
                improver=improver,
                scenario_plan=scenario_plan,
                proposal_id=proposal_id,
                memo=memo,
                robustness_warnings_acknowledged=robustness_warnings_acknowledged,
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
            # Deterministic apply: gated proposals claim + spawn a detached
            # completer child; only ungated/legacy proposals fall back to an
            # agent wake (and that wake claims for itself).
            application = launch_application(store, job_id, proposal_id)
            sync_all_jobs(store=store)
            return ok(
                {
                    "proposal": proposal,
                    "application": application,
                    "wakeup": application.get("wakeup"),
                }
            )
        if action == "reject_proposal":
            # Attribution default: worker self-rejections are REQUIRED to
            # carry a reason memo (superseded/stale housekeeping); the owner
            # UI passes none today. A reasoned rejection without explicit
            # attribution is therefore the agent's own housekeeping, a bare
            # one is an owner veto — which binds the worker (no equivalent
            # re-proposal without named new evidence).
            # `kind` (process|substantive) rides the same param used by
            # propose: process rejections expect a successor proposal.
            proposal = store.reject_proposal(
                job_id,
                proposal_id,
                reason=reason,
                rejected_by="agent" if reason else "owner",
                kind=kind,
            )
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
        # The MCP tool is the agent surface (same convention as
        # apply_script_mode's set_by="agent"): risk/protection-latched halts
        # refuse the clear — the owner resumes via the CLI with --by owner.
        try:
            payload = clear_halt(store, job_id, by="agent")
        except PermissionError as exc:
            return err("forbidden", str(exc))
        sync_all_jobs(store=store)
        return ok(payload)

    return err("invalid_request", f"unknown action: {action}")
