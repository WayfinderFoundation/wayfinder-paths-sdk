from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger

from wayfinder_paths.core.config import (
    get_api_base_url,
    get_api_key,
    get_opencode_instance_id,
    is_opencode_instance,
)
from wayfinder_paths.jobs.background import op_status_summary
from wayfinder_paths.jobs.backtest_artifacts import summarize_backtest_artifacts
from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.execution.features import summarize_features
from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.forward import load_forward_snapshot
from wayfinder_paths.jobs.gating import evaluate_live_gate
from wayfinder_paths.jobs.halt import read_halt
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.runner_bridge import RunnerBridge
from wayfinder_paths.jobs.store import JobStore

SCRIPT_MODES = ("paper", "live")


class WayfinderJobsClient:
    def __init__(self) -> None:
        self._client = httpx.Client(timeout=httpx.Timeout(10), follow_redirects=True)

    def _base_url(self) -> str | None:
        if not is_opencode_instance():
            return None
        instance_id = get_opencode_instance_id()
        if not instance_id:
            return None
        return f"{get_api_base_url()}/opencode/instances/{instance_id}/wayfinder-jobs"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = get_api_key()
        if api_key:
            headers["X-API-KEY"] = api_key
        return headers

    def sync(self, jobs: list[dict[str, Any]]) -> None:
        base_url = self._base_url()
        if not base_url:
            return
        try:
            resp = self._client.post(
                f"{base_url}/sync/",
                json={"jobs": jobs},
                headers=self._headers(),
            )
            resp.raise_for_status()
        except Exception:
            logger.opt(exception=True).warning(
                "Failed to sync Wayfinder jobs to backend"
            )


WAYFINDER_JOBS_CLIENT = WayfinderJobsClient()


def _report_with_session(
    store: JobStore, job_id: str, *dir_names: str
) -> dict[str, Any] | None:
    """Latest report for a mode, with session_id/created_at backfilled from
    the durable sidecar. The wake agent overwrites latest.json with its own
    finding and drops those keys; without the backfill the frontend's per-job
    Conversations list can't link the wake session. `dir_names` allows the
    legacy fallback (intervene->improve, auto->decide)."""
    for dir_name in dir_names:
        report = store.read_json(
            job_id, f"reports/{dir_name}/latest.json", default=None
        )
        if not isinstance(report, dict):
            continue
        if not report.get("session_id") or not report.get("created_at"):
            sidecar = store.read_json(
                job_id, f"reports/{dir_name}/session.json", default=None
            )
            if isinstance(sidecar, dict):
                report = {
                    **report,
                    "session_id": report.get("session_id") or sidecar.get("session_id"),
                    "created_at": report.get("created_at") or sidecar.get("created_at"),
                }
        return report
    return None


def _unix_to_iso(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _engine_mode(store: JobStore, job_id: str) -> str | None:
    state = store.read_json(job_id, "state/engine_state.json", default=None)
    if isinstance(state, dict) and state.get("mode"):
        return str(state["mode"])
    return None


def _dataset_fetch_state(store: JobStore, job_id: str) -> dict[str, Any] | None:
    """Dataset-provisioning state for the UI: starters self-provision their
    bars via a detached fetch_dataset op (~minutes), during which charts are
    empty and backtests gate — without this key the FE can't tell "data on
    the way" from an ordinary job. Reports the op status file when one exists
    (running/done/failed); `needed` marks a starter (evidence file present)
    whose bars never arrived and has no op recorded. None — the common case,
    bars present and no op — omits the key so existing job snapshots stay
    byte-identical."""
    job_dir = store.job_dir(job_id)
    summary = op_status_summary(job_dir, "fetch_dataset")
    if summary is not None:
        return summary
    backtest_dir = job_dir / "results" / "backtest"
    if (
        not (backtest_dir / "input_bars.json").exists()
        and (backtest_dir / "starter_evidence.json").exists()
    ):
        return {"status": "needed"}
    return None


def _runtime_reconciliation(job: Any, store: JobStore) -> dict[str, Any]:
    """Overlay the live runner/engine truth onto the scorecard so the UI shows
    what's ACTUALLY running, not the declared job.yaml. The driver executes the
    mode baked into the runner env (WAYFINDER_JOB_MODE), which an agent can flip
    without touching job.yaml — that split-brain once left a job live-trading
    while the UI read "paper". This extends the original paused reconciliation
    to mode, agent_mode, active_revision, and the runner's scheduling/health
    metrics. Degrades to {} (keep declared values) when the runner is down."""
    script = job.script_loop
    agent = job.agent_loop
    loop_names = [
        loop.runner_job_name
        for loop in (script, agent)
        if loop.enabled and loop.runner_job_name
    ]
    if not loop_names:
        return {}
    states = RunnerBridge(repo_root=store.repo_root).job_states()
    if not states:
        return {}
    out: dict[str, Any] = {
        "paused": any(states.get(n, {}).get("status") == "PAUSED" for n in loop_names)
    }
    script_state = states.get(script.runner_job_name or "") if script.enabled else None
    if script_state:
        env = (script_state.get("payload") or {}).get("env") or {}
        declared_mode = str(script.mode or "paper")
        runtime_mode = str(
            env.get("WAYFINDER_JOB_MODE")
            or _engine_mode(store, job.id)
            or declared_mode
        )
        out["mode"] = runtime_mode
        out["mode_mismatch"] = runtime_mode != declared_mode
        if env.get("WAYFINDER_JOB_REVISION"):
            out["active_revision"] = str(env["WAYFINDER_JOB_REVISION"])
        out["runner_status"] = str(script_state.get("status") or "")
        for src, iso in (
            ("last_run_at", True),
            ("last_ok_at", True),
            ("next_run_at", True),
        ):
            value = script_state.get(src)
            if value is not None:
                out[src] = _unix_to_iso(value) if iso else value
        out["consecutive_failures"] = int(script_state.get("consecutive_failures") or 0)
        if script_state.get("last_error"):
            out["last_error"] = str(script_state["last_error"])
    agent_state = states.get(agent.runner_job_name or "") if agent.enabled else None
    if agent_state:
        aenv = (agent_state.get("payload") or {}).get("env") or {}
        if aenv.get("WAYFINDER_JOB_AGENT_MODE"):
            runtime_agent_mode = str(aenv["WAYFINDER_JOB_AGENT_MODE"])
            out["agent_mode"] = runtime_agent_mode
            # Box-internal split-brain tripwire, mirroring script mode_mismatch:
            # the runner wakes under the env baked at last compile, so a failed
            # or skipped recompile after a job.yaml edit leaves the two
            # disagreeing. This does NOT compare against what the DB/UI think
            # the mode is — the box never sees that (see snapshot_job).
            out["agent_mode_mismatch"] = runtime_agent_mode != str(agent.mode)
    return out


def snapshot_job(job_id: str, *, store: JobStore | None = None) -> dict[str, Any]:
    store = store or JobStore()
    job = store.load(job_id)
    scorecard = store.read_json(job_id, "scorecard.json", default={}) or {}
    from wayfinder_paths.jobs.evolution_ledger import build_process_efficiency

    scorecard = {
        **scorecard,
        "process_efficiency": build_process_efficiency(store, job_id),
    }
    # Reflect the live runner/engine state, not the declared job.yaml: mode
    # (paper/live), agent_mode, active_revision, paused, and scheduling/health
    # metrics all come from the runner where it is the source of truth. See
    # _runtime_reconciliation. Degrades to the declared scorecard on a down
    # runner, so a sync never breaks.
    runtime = _runtime_reconciliation(job, store)
    if runtime:
        scorecard = {**scorecard, **runtime}
    # The box's authoritative agent mode, shipped unconditionally: job.yaml's
    # agent_loop block is what the compiler bakes into the runner, so it is
    # the runner truth regardless of daemon reachability. The sync channel is
    # push-only — the box never learns what the DB/UI believe the mode is (a
    # UI mode selection once got dropped before reaching job.yaml, and the DB
    # showed autonomous while the box ran monitor, silently, for days) — so
    # the backend-declared vs actual comparison must live backend-side, keyed
    # on this field. agent_mode_source pins provenance for that comparison.
    scorecard = {
        **scorecard,
        "agent_mode_actual": str(job.agent_loop.mode),
        "agent_mode_source": "job_yaml",
    }
    from wayfinder_paths.jobs.compute_lock import evolution_compute_budget_status
    from wayfinder_paths.jobs.risk_overrides import (
        active_symbol_blocks,
        risk_overrides_snapshot,
    )

    scorecard["evolution_compute"] = evolution_compute_budget_status(store.repo_root)
    blocks = active_symbol_blocks(store, job_id)
    scorecard["risk_symbol_blocks"] = sorted(blocks)
    dataset_fetch = _dataset_fetch_state(store, job_id)
    if dataset_fetch is not None:
        scorecard = {**scorecard, "dataset_fetch": dataset_fetch}
    runner_links = store.read_json(job_id, "runner_links.json", default={}) or {}
    latest_monitor = _report_with_session(store, job_id, "monitor")
    latest_intervene = _report_with_session(store, job_id, "intervene", "improve")
    latest_auto = _report_with_session(store, job_id, "auto", "decide")
    latest_apply = _report_with_session(store, job_id, "apply")
    from wayfinder_paths.jobs.regime_contract import REGIME_HEALTH_PATH

    regime_health = store.read_json(job_id, REGIME_HEALTH_PATH, default=None)
    if isinstance(regime_health, dict):
        from wayfinder_paths.jobs.regime_health import compact_regime_health

        regime_health = compact_regime_health(regime_health)
    validation = (
        store.read_json(job_id, "reports/validation/latest.json", default={}) or {}
    )
    try:
        features = summarize_features(
            store.job_dir(job_id),
            ExecutionSpec.from_dict(dict(job.execution_spec or {})),
        )
    except Exception:
        features = None
    return {
        "job": job.to_dict(),
        "scorecard": scorecard,
        "backtest": summarize_backtest_artifacts(job_id, store=store),
        "forward": load_forward_snapshot(job_id, store=store, limit=25),
        "runner_links": runner_links,
        "proposals": store.proposals(job_id),
        "probation": store.read_json(job_id, "probation.json", default={"legs": []}),
        "risk_overrides": risk_overrides_snapshot(store, job_id),
        "post_apply_shadow": _shadow_topline(store, job_id),
        "regime_health": regime_health,
        "decision_log": _decision_log(store, job_id),
        "proposal_queue": store.proposal_queue(job_id),
        "reports": {
            "monitor": latest_monitor,
            "intervene": latest_intervene,
            "auto": latest_auto,
            "apply": latest_apply,
            "reconcile": store.read_json(
                job_id, "reports/reconcile/latest.json", default=None
            ),
        },
        "execution_contract": job.execution_contract,
        "validation": (
            {
                "status": validation.get("status"),
                "revision": validation.get("revision"),
                "failed_checks": [
                    check.get("name")
                    for check in validation.get("checks") or []
                    if not check.get("passed")
                ],
            }
            if validation
            else {}
        ),
        "gate": _gate_with_restamp(job_id, store),
        # Manual kill-switch detail (contract C4): scorecard already reports
        # live_execution_status="halted" while set; this carries reason/ts.
        "halt": read_halt(store.job_dir(job_id)),
        "features": features,
        # Two-zone attention split (owner doctrine): needs_you = owner-blocking
        # live-capital/governance items; decided_autonomously = the last 7d of
        # mechanical decisions with evidence + bounded undo. Top-level (like
        # scorecard) so backend/FE consume it without SDK round-trips.
        "owner_attention": _owner_attention(store, job_id, job),
    }


def _owner_attention(store: JobStore, job_id: str, job: Any) -> dict[str, Any]:
    from wayfinder_paths.jobs.owner_attention import build_owner_attention

    try:
        return build_owner_attention(store, job_id, job=job)
    except Exception:  # noqa: BLE001 — sync must never die on a feed
        return {"needs_you": [], "decided_autonomously": []}


def sync_all_jobs(*, store: JobStore | None = None) -> None:
    store = store or JobStore()
    snapshots = [snapshot_job(job.id, store=store) for job in store.list_jobs()]
    WAYFINDER_JOBS_CLIENT.sync(snapshots)


OPERATOR_STATE_PATH = "state/operator.json"


def apply_script_mode(
    job_id: str,
    mode: str,
    *,
    store: JobStore | None = None,
    set_by: str = "owner",
    force: bool = False,
) -> dict[str, Any]:
    """Flip a job's script-loop mode (paper<->live) the compiler-safe way.

    Mirrors set_agent_mode: edits ``job.yaml`` (`script_loop.mode`), saves,
    recompiles — which re-bakes ``WAYFINDER_JOB_MODE`` into the runner env — and
    re-syncs. This is the ONLY supported way to change execution mode. The env is
    derived from job.yaml at compile time, so hand-patching the runner env
    creates a paper/live split-brain that the next recompile silently reverts.

    Going live is gated: the job must pass ``evaluate_live_gate`` (``live_ready``)
    and declare ``execution_params.wallet_label``. A blocked gate raises
    ``ValueError`` naming the blocker and writes nothing.

    Leaving live is guarded: if the live engine state holds open positions,
    the flip is REFUSED unless ``force=True`` — a live->paper flip resets the
    engine state, which orphans real venue positions with no stop and no
    manager (observed live: a reverted canary left a HYPE short unmanaged
    for 26 hours). Flatten first (halt --flatten), or force explicitly.

    Every flip records WHO made it in ``state/operator.json`` — the wake
    prompt renders it, so agents can distinguish an operator decision from
    the unexplained-flip incidents their halt discipline was built on.
    """
    if mode not in SCRIPT_MODES:
        raise ValueError(f"script mode must be one of {SCRIPT_MODES}, got {mode!r}")
    store = store or JobStore()
    job = store.load(job_id)

    if mode == "live":
        if not job.execution_params.get("wallet_label"):
            raise ValueError(
                "cannot go live: execution_params.wallet_label is not set — a "
                "live job needs a funded wallet to trade from (set it via the "
                "job's execution params, then retry)"
            )
        gate = evaluate_live_gate(job_id, store=store)
        if not gate["live_ready"]:
            reasons = "; ".join(gate["reasons"]) or "live gate not ready"
            raise ValueError(f"cannot go live: {reasons}")

    if mode == "paper" and str(job.script_loop.mode) == "live" and not force:
        engine = store.read_json(job_id, "state/engine_state.json") or {}
        open_positions = {
            symbol: position
            for symbol, position in (engine.get("positions") or {}).items()
            if position
        }
        if str(engine.get("mode")) == "live" and open_positions:
            raise ValueError(
                "cannot leave live: the live engine holds open positions "
                f"({', '.join(sorted(open_positions))}) — flipping to paper "
                "resets the engine and orphans them on the venue with no "
                "stop and no manager. Flatten first (wayfinder job halt "
                "--flatten), or pass force=True to orphan deliberately."
            )

    job.script_loop.mode = mode
    store.save(job)
    result = JobCompiler(store=store).compile(job)
    operator_state = store.read_json(job_id, OPERATOR_STATE_PATH) or {}
    operator_state["script_mode"] = {
        "mode": mode,
        "set_by": set_by,
        "set_at": utc_now_iso(),
        "forced": bool(force),
    }
    store.write_json(job_id, OPERATOR_STATE_PATH, operator_state)
    store.append_journal(
        job_id,
        {
            "type": "script_mode_set",
            "mode": mode,
            "set_by": set_by,
            "forced": bool(force),
        },
    )
    sync_all_jobs(store=store)
    return {"job_id": job_id, "mode": mode, "set_by": set_by, "compile": result}


# Operator sizing ceiling — venue max leverage on majors is higher, but an
# operator fat-fingering 40x through a UI control is not a trade thesis.
MAX_OPERATOR_LEVERAGE = 25.0


def _gate_with_restamp(job_id: str, store: JobStore) -> dict[str, Any]:
    """Live gate + pending-not-red context: while a detached restamp is
    running the gate is transiently red by construction — surface that so
    UIs and wake agents render 'refreshing' instead of alarming. The
    authoritative live_ready stays strict."""
    gate = evaluate_live_gate(job_id, store=store)
    try:
        import json as _json
        import os

        status_path = (
            store.job_dir(job_id) / "state" / "background_ops" / "restamp.json"
        )
        status = _json.loads(status_path.read_text(encoding="utf-8"))
        pid = status.get("pid")
        alive = False
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if status.get("state") == "running" and alive:
            gate["restamp_in_progress"] = True
            gate["restamp_started_at"] = status.get("started_at")
    except (OSError, ValueError):
        pass
    return gate


def apply_execution_leverage(
    job_id: str, leverage: float, *, store: JobStore | None = None
) -> dict[str, Any]:
    """Operator-owned sizing knob: write ``execution_params.leverage``.

    Params are re-read from job.yaml every tick, so the change takes effect
    on the next tick without a recompile (unlike script mode, which is baked
    into the runner env). Journaled so wake context shows who changed sizing
    and from what."""
    value = float(leverage)
    if not value > 0 or value > MAX_OPERATOR_LEVERAGE:
        raise ValueError(
            f"leverage must be in (0, {MAX_OPERATOR_LEVERAGE:g}], got {value:g}"
        )
    store = store or JobStore()
    job = store.load(job_id)
    previous = job.execution_params.get("leverage")
    job.execution_params["leverage"] = value
    job.touch()
    store.save(job)
    store.append_journal(
        job_id,
        {"type": "operator_leverage_set", "from": previous, "to": value},
    )
    sync_all_jobs(store=store)
    restamp: dict[str, Any] | None = None
    if previous != value:
        # The params edit bumped the workspace revision, so the
        # validation/backtest/preflight stamps just went stale and the live
        # gate is red until they re-run. Kick the refresh detached — the
        # knob applies next tick regardless; op_status(op="restamp") polls.
        try:
            from wayfinder_paths.jobs.background import spawn_detached_op

            restamp = spawn_detached_op(store, job_id, "restamp", {"job_id": job_id})
            store.append_journal(
                job_id, {"type": "gate_restamp_kicked", "trigger": "set_leverage"}
            )
        except Exception as exc:  # noqa: BLE001 — knob must not fail on this
            restamp = {"error": str(exc)[:200]}
    return {
        "job_id": job_id,
        "leverage": value,
        "previous": previous,
        "restamp": restamp,
    }


def apply_wallet_label(
    job_id: str, label: str, *, store: JobStore | None = None
) -> dict[str, Any]:
    """Bind the funded wallet a live job trades from
    (``execution_params.wallet_label``). Routing, not strategy logic — the
    field is excluded from the workspace revision hash, so binding never
    orphans the gate stamps. Params are re-read every tick; no recompile."""
    cleaned = str(label).strip()
    if not cleaned:
        raise ValueError("wallet label cannot be empty")
    store = store or JobStore()
    job = store.load(job_id)
    previous = job.execution_params.get("wallet_label")
    job.execution_params["wallet_label"] = cleaned
    job.touch()
    store.save(job)
    store.append_journal(
        job_id,
        {"type": "operator_wallet_label_set", "from": previous, "to": cleaned},
    )
    sync_all_jobs(store=store)
    return {"job_id": job_id, "wallet_label": cleaned, "previous": previous}


def apply_initial_capital(
    job_id: str, amount: float, *, store: JobStore | None = None
) -> dict[str, Any]:
    """Operator accounting knob: ``execution_params.initial_capital`` — the
    equity base live sizing compounds from. Set it to what the strategy
    wallet actually holds; a mismatch makes the engine size against money
    that isn't there. Excluded from the revision hash; applies next tick.

    Zero is allowed deliberately: withdrawing the full bankroll should read
    as "unfunded", which fails validation's initial_capital_declared check
    and turns the live gate red — the honest state."""
    value = float(amount)
    if value < 0:
        raise ValueError(f"initial capital cannot be negative, got {value:g}")
    store = store or JobStore()
    job = store.load(job_id)
    previous = job.execution_params.get("initial_capital")
    job.execution_params["initial_capital"] = value
    job.touch()
    store.save(job)
    store.append_journal(
        job_id,
        {"type": "operator_initial_capital_set", "from": previous, "to": value},
    )
    sync_all_jobs(store=store)
    return {"job_id": job_id, "initial_capital": value, "previous": previous}


def _funded_wallet_label(job) -> str:
    label = str(job.execution_params.get("wallet_label") or "").strip()
    if not label:
        raise ValueError(
            "job has no bound wallet (execution_params.wallet_label) — go "
            "live first so the strategy wallet exists"
        )
    return label


def _shift_equity_recon_baseline(
    store: JobStore, job_id: str, delta: float
) -> None:
    """Fold an operator deposit/withdrawal into the drift baseline. The
    equity reconciler treats venue-vs-expected drift as its signal; without
    this, every funding action reads as permanent drift the agent has to
    re-explain each wake. Missing seed = pre-first-tick, nothing to shift."""
    recon = store.read_json(job_id, "state/equity_recon.json", default=None)
    if not recon or "venue_equity_start" not in recon:
        return
    recon["venue_equity_start"] = float(recon["venue_equity_start"]) + delta
    store.write_json(job_id, "state/equity_recon.json", recon)


async def venue_deposit(
    job_id: str, amount: float, *, store: JobStore | None = None
) -> dict[str, Any]:
    """Fund the strategy where it trades: bridge USDC from the job's bound
    wallet into Hyperliquid, then record ``initial_capital`` in lockstep. The
    FIRST venue deposit REPLACES the declared capital — the paper-mode
    default (e.g. $10k) must not leak into live sizing over a $50 bankroll —
    and later deposits add. The marker lives in state/ (not hashed, wiped
    with engine state, survives capital edits). An ``unconfirmed`` bridge
    credit still counts (the deposit is en route); only a failed send aborts
    before the capital write."""
    from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_deposit_usdc

    store = store or JobStore()
    job = store.load(job_id)
    label = _funded_wallet_label(job)
    envelope = await hyperliquid_deposit_usdc(wallet_label=label, amount_usdc=amount)
    if not envelope["ok"]:
        raise ValueError(f"venue deposit failed: {envelope}")
    outcome = envelope["result"]
    if outcome["status"] == "failed":
        raise ValueError(f"venue deposit failed: {outcome}")
    funding = store.read_json(job_id, "state/funding.json", default=None) or {}
    base = (
        float(job.execution_params.get("initial_capital") or 0.0)
        if funding.get("venue_funded")
        else 0.0
    )
    capital = apply_initial_capital(job_id, base + float(amount), store=store)
    store.write_json(job_id, "state/funding.json", {"venue_funded": True})
    _shift_equity_recon_baseline(store, job_id, float(amount))
    return {
        "job_id": job_id,
        "deposit_status": outcome["status"],
        "initial_capital": capital["initial_capital"],
    }


async def venue_withdraw(
    job_id: str,
    amount: float,
    *,
    destination: str | None = None,
    store: JobStore | None = None,
) -> dict[str, Any]:
    """Pull bankroll off the venue: withdraw USDC from Hyperliquid (Bridge2
    nets $1 off the amount) to ``destination`` — the job's bound wallet when
    omitted — and shrink ``initial_capital`` by the gross amount, floored at
    zero; a full withdrawal honestly reads as unfunded and turns the gate
    red."""
    from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_withdraw_usdc

    store = store or JobStore()
    job = store.load(job_id)
    label = _funded_wallet_label(job)
    envelope = await hyperliquid_withdraw_usdc(
        wallet_label=label, amount_usdc=amount, destination=destination
    )
    if not envelope["ok"]:
        raise ValueError(f"venue withdraw failed: {envelope}")
    outcome = envelope["result"]
    if outcome["status"] == "failed":
        raise ValueError(f"venue withdraw failed: {outcome}")
    current = float(job.execution_params.get("initial_capital") or 0.0)
    capital = apply_initial_capital(
        job_id, max(current - float(amount), 0.0), store=store
    )
    _shift_equity_recon_baseline(store, job_id, -float(amount))
    return {
        "job_id": job_id,
        "withdraw_status": outcome["status"],
        "initial_capital": capital["initial_capital"],
    }


def _shadow_topline(store: JobStore, job_id: str) -> dict[str, Any]:
    """Read-only topline of the post-apply counterfactual for the UI — the
    artifact is computed on the wake path, never during sync."""
    from wayfinder_paths.jobs.counterfactual import load_counterfactual

    doc = load_counterfactual(store, job_id)
    if not doc or not doc.get("available"):
        return {}
    keys = ("proposal_id", "applied_at", "window", "actual", "shadow", "delta_net_pnl")
    return {key: doc[key] for key in keys if key in doc}


def _decision_log(store: JobStore, job_id: str) -> dict[str, Any]:
    """Narrative feed for the UI, assembled read-only from recorded events."""
    from wayfinder_paths.jobs.decision_log import build_decision_log

    try:
        return build_decision_log(store, job_id)
    except Exception:  # noqa: BLE001 — sync must never die on a feed
        return {}
