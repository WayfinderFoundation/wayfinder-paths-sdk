from __future__ import annotations

import json
import os
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
from wayfinder_paths.jobs import heavy_lane
from wayfinder_paths.jobs.background import op_status_summary
from wayfinder_paths.jobs.backtest_artifacts import summarize_backtest_artifacts
from wayfinder_paths.jobs.capital import (
    FUNDING_MARKER_PATH,
    cancel_pending_withdrawal,
    capital_summary,
    capital_transfer,
    funded_capital,
    has_capital_history,
    pending_withdrawal,
    record_capital_flow,
    set_funded_capital,
    set_pending_withdrawal,
)
from wayfinder_paths.jobs.capital_transfers import (
    deposit_to_venue,
    deposit_tx_hash,
    execute_withdrawal,
    shift_equity_recon_baseline,
    venue_balance,
    venue_equity_or_none,
)
from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.execution.features import summarize_features
from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.forward import load_forward_snapshot
from wayfinder_paths.jobs.gating import evaluate_live_gate
from wayfinder_paths.jobs.halt import read_halt
from wayfinder_paths.jobs.models import LIFECYCLE_CONTRACTS, utc_now_iso
from wayfinder_paths.jobs.probation import probation_sync_payload
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
        except Exception as exc:
            logger.warning(f"Failed to sync Wayfinder jobs to backend: {exc!r}")


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


def _runner_states(store: JobStore) -> dict[str, Any]:
    return RunnerBridge(repo_root=store.repo_root).job_states()


def _runtime_reconciliation(
    job: Any, store: JobStore, *, states: dict[str, Any] | None = None
) -> dict[str, Any]:
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
    if states is None:
        states = _runner_states(store)
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
    # One runner round-trip per snapshot: the reconciliation overlay and the
    # heartbeat read the same states.
    runner_states = (
        _runner_states(store)
        if (job.script_loop.enabled or job.agent_loop.enabled)
        else {}
    )
    runtime = _runtime_reconciliation(job, store, states=runner_states)
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
    scorecard["pending_ops"] = heavy_lane.lane_view_for_job(store.repo_root, job_id)
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
    reports = {
        "monitor": latest_monitor,
        "intervene": latest_intervene,
        "auto": latest_auto,
        "apply": latest_apply,
        "reconcile": store.read_json(
            job_id, "reports/reconcile/latest.json", default=None
        ),
    }
    proposals = store.proposals(job_id)
    halt = read_halt(store.job_dir(job_id))
    gate = _gate_with_restamp(job_id, store)
    return {
        "job": job.to_dict(),
        "scorecard": scorecard,
        "backtest": summarize_backtest_artifacts(job_id, store=store),
        "forward": load_forward_snapshot(job_id, store=store, limit=25),
        "runner_links": runner_links,
        "proposals": proposals,
        # probation.json enriched with each trial's paired equity curve —
        # curve points live in per-trial sidecars, never in probation.json.
        "probation": probation_sync_payload(store, job_id),
        "risk_overrides": risk_overrides_snapshot(store, job_id),
        "post_apply_shadow": _shadow_topline(store, job_id),
        "regime_health": regime_health,
        "decision_log": _decision_log(store, job_id),
        "proposal_queue": store.proposal_queue(job_id),
        "reports": reports,
        "execution_contract": job.execution_contract,
        # Owner Fund/Withdraw context; wallet_label binds the UI buttons to the
        # wallet the job actually trades from (not always the job id).
        "capital": capital_summary(store, job_id),
        "wallet_label": job.execution_params.get("wallet_label"),
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
        "gate": gate,
        # Manual kill-switch detail (contract C4): scorecard already reports
        # live_execution_status="halted" while set; this carries reason/ts.
        "halt": halt,
        "features": features,
        # Two-zone attention split (owner doctrine): needs_you = owner-blocking
        # live-capital/governance items; decided_autonomously = the last 7d of
        # mechanical decisions with evidence + bounded undo. Top-level (like
        # scorecard) so backend/FE consume it without SDK round-trips.
        "owner_attention": _owner_attention(store, job_id, job),
        # The launch flow: the pinned launch, named risk gaps and the paper
        # checklist, so the UI reads identity and blockers without an SDK
        # round-trip. Raise-free: a feed failure must never break a sync.
        **_launch_payload(
            store,
            job_id,
            job,
            runner_states=runner_states,
            reports=reports,
            scorecard=scorecard,
            features=features,
            halt=halt,
            proposals=proposals,
            live_gate=gate,
        ),
    }


def _launch_payload(
    store: JobStore,
    job_id: str,
    job: Any,
    *,
    runner_states: dict[str, Any] | None = None,
    reports: dict[str, Any] | None = None,
    scorecard: dict[str, Any] | None = None,
    features: list[dict[str, Any]] | None = None,
    halt: dict[str, Any] | None = None,
    proposals: list[dict[str, Any]] | None = None,
    live_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from wayfinder_paths.jobs.health import health_payload
    from wayfinder_paths.jobs.launch import LAUNCH_STATE_PATH, evaluate_launch_checklist
    from wayfinder_paths.jobs.paths_runtime import UPGRADE_STATE_PATH
    from wayfinder_paths.jobs.readout import READOUT_PATH
    from wayfinder_paths.jobs.risk_flags import acknowledged_flags, risk_flags

    payload: dict[str, Any] = {
        "launch": store.read_json(job_id, LAUNCH_STATE_PATH, default=None),
        "readout": store.read_json(job_id, READOUT_PATH, default=None),
        "path_upgrade": store.read_json(job_id, UPGRADE_STATE_PATH, default=None),
        "watchdog": None,
        "evolution": None,
        "probation_summary": [],
        "research": {
            "ideation": store.read_json(
                job_id, "research/ideation/latest.json", default=None
            ),
        },
        "risk_flags": None,
        "launch_checklist": None,
        "heartbeat": None,
        "issues": [],
        "freestyle": None,
        "path": None,
    }
    try:
        from wayfinder_paths.jobs.freestyle.telemetry import (
            freestyle_snapshot,
            path_snapshot,
        )

        payload["freestyle"] = freestyle_snapshot(store, job_id, job)
        payload["path"] = path_snapshot(store, job_id, job)
    except Exception:  # noqa: BLE001
        pass
    try:
        acknowledged = acknowledged_flags(store, job_id)
        payload["risk_flags"] = [
            {
                **flag,
                "acknowledged": flag.get("severity") == "warn"
                and flag.get("code") in acknowledged,
            }
            for flag in risk_flags(job, store.job_dir(job_id))
        ]
    except Exception:  # noqa: BLE001
        pass
    try:
        from wayfinder_paths.jobs.launch import watchdog_view

        payload["watchdog"] = watchdog_view(job, store.job_dir(job_id))
    except Exception:  # noqa: BLE001
        pass
    try:
        from wayfinder_paths.jobs.evolution_view import (
            evolution_snapshot,
            probation_summary,
        )

        payload["evolution"] = evolution_snapshot(store, job_id, job)
        payload["probation_summary"] = probation_summary(store, job_id)
    except Exception:  # noqa: BLE001
        pass
    if str(job.execution_contract or "legacy") in LIFECYCLE_CONTRACTS:
        # The checklist in the job's real phase: a job the runner executes
        # live is checked against the live rule, never told to go paper.
        target = "live" if (scorecard or {}).get("mode") == "live" else "paper"
        try:
            if target == "live" and live_gate and live_gate.get("checklist"):
                # Freestyle/path readiness already ran the live checklist.
                payload["launch_checklist"] = live_gate["checklist"]
            else:
                payload["launch_checklist"] = evaluate_launch_checklist(
                    job_id, store=store, target=target, live_gate=live_gate
                )
        except Exception:  # noqa: BLE001
            pass
    checklist = payload["launch_checklist"]
    workspace_revision = (
        str(checklist.get("revision") or "") or None
        if isinstance(checklist, dict)
        else None
    )
    payload.update(
        health_payload(
            store,
            job_id,
            job,
            runner_states=runner_states or {},
            reports=reports or {},
            scorecard=scorecard,
            features=features,
            risk_flags=payload["risk_flags"],
            launch=payload["launch"],
            launch_checklist=checklist,
            halt=halt,
            proposals=proposals,
            workspace_revision=workspace_revision,
        )
    )
    return payload


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
        if str(job.execution_contract) in {"freestyle_v1", "path_v1"}:
            # Non-harnessed kinds answer through the launch checklist;
            # jobs_v1 keeps the live gate call byte-identical.
            from wayfinder_paths.jobs.contracts import evaluate_live_readiness

            gate = evaluate_live_readiness(job_id, store=store)
        else:
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
    queued or running the gate is transiently red by construction — surface
    that so UIs and wake agents render 'refreshing' instead of alarming. The
    authoritative live_ready stays strict."""
    from wayfinder_paths.jobs.contracts import evaluate_live_readiness

    # Freestyle and path jobs answer through the launch checklist; jobs_v1
    # keeps evaluate_live_gate. The backend reads this as job.live_gate.
    gate = evaluate_live_readiness(job_id, store=store)
    status_path = store.job_dir(job_id) / "state" / "background_ops" / "restamp.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return gate
    if not isinstance(status, dict):
        return gate
    state = status.get("state")
    if state == "queued" or (state == "running" and _pid_alive(status.get("pid"))):
        gate["restamp_in_progress"] = True
        gate["restamp_started_at"] = status.get("started_at")
        gate["restamp_state"] = state
    return gate


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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

            restamp = spawn_detached_op(
                store, job_id, "restamp", {"job_id": job_id}, submitted_by="sync"
            )
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
    funded capital the job's derived views (forward equity curve, regime
    health, probation) measure returns against. Live sizing does not read it:
    it sizes from the venue's marked account value each tick. Venue
    deposits/withdrawals keep it in lockstep and record the change in the
    capital ledger; setting it by hand rebases all of history. Excluded from
    the revision hash; applies next tick.

    Zero is allowed deliberately: withdrawing the full bankroll should read
    as "unfunded", which fails validation's initial_capital_declared check
    and turns the live gate red — the honest state."""
    value = float(amount)
    if value < 0:
        raise ValueError(f"initial capital cannot be negative, got {value:g}")
    store = store or JobStore()
    previous = set_funded_capital(store, job_id, value)
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


async def venue_deposit(
    job_id: str, amount: float, *, by: str = "owner", store: JobStore | None = None
) -> dict[str, Any]:
    """Owner funding: bridge USDC from the job's bound wallet into
    Hyperliquid, record the flow in the capital ledger, and grow
    ``initial_capital`` by the amount. A job that has never held real money
    (no venue funding, no live equity seed, no flows) carries a paper
    placeholder (e.g. $10k) that must not leak into live accounting, so its
    first deposit REPLACES the capital; every other deposit adds. The live
    tick rescales the risk peak for the flow; sizing follows the venue
    account value on its own. The capital lock is held from before the
    transfer until the flow is recorded (see ``capital_transfer``). An
    unconfirmed credit is recorded ``confirmed: false`` and only moves the
    risk peak once it shows in venue equity."""
    store = store or JobStore()
    job = store.load(job_id)
    label = _funded_wallet_label(job)
    with capital_transfer(store, job_id, "deposit"):
        equity_before = await venue_equity_or_none(label)
        outcome = await deposit_to_venue(label, float(amount))
        current = funded_capital(store, job_id)
        capital = (
            current + float(amount)
            if has_capital_history(store, job_id)
            else float(amount)
        )
        flow = record_capital_flow(
            store,
            job_id,
            "deposit",
            amount,
            capital_delta=capital - current,
            by=by,
            tx=deposit_tx_hash(outcome),
            equity_before=equity_before,
            confirmed=outcome["status"] == "confirmed",
        )
        store.write_json(job_id, FUNDING_MARKER_PATH, {"venue_funded": True})
        shift_equity_recon_baseline(store, job_id, float(amount))
        set_funded_capital(store, job_id, capital)
        store.append_journal(
            job_id,
            {"type": "deposit_executed", "flow": flow, "status": outcome["status"]},
        )
    sync_all_jobs(store=store)
    return {
        "job_id": job_id,
        "deposit_status": outcome["status"],
        "initial_capital": capital,
        "flow": flow,
    }


async def venue_withdraw(
    job_id: str,
    amount: float,
    *,
    destination: str | None = None,
    by: str = "owner",
    store: JobStore | None = None,
) -> dict[str, Any]:
    """Owner withdrawal from the venue (Bridge2 nets $1 off the gross amount)
    to ``destination`` — the job's bound wallet when omitted. When free
    margin covers it the transfer runs now and the flow is recorded; when
    open positions hold the margin it is QUEUED: live sizing immediately
    treats the amount as gone, and the live tick runs the transfer once
    enough margin frees up. One pending withdrawal at a time."""
    store = store or JobStore()
    job = store.load(job_id)
    label = _funded_wallet_label(job)
    with capital_transfer(store, job_id, "withdrawal"):
        if pending_withdrawal(store, job_id) is not None:
            raise ValueError(
                "a withdrawal is already pending for this job — cancel it first"
            )
        balance = await venue_balance(label)
        if float(amount) > balance.equity_usd:
            raise ValueError(
                f"withdrawal ${float(amount):g} exceeds venue equity "
                f"${balance.equity_usd:.2f}"
            )
        if balance.withdrawable_usd < float(amount):
            if job.script_loop.mode != "live":
                raise ValueError(
                    f"only ${balance.withdrawable_usd:.2f} is withdrawable and "
                    "the job is not live to free margin — close the venue "
                    "positions first"
                )
            pending = set_pending_withdrawal(
                store,
                job_id,
                float(amount),
                destination=destination,
                by=by,
                withdrawable_now=balance.withdrawable_usd,
            )
            result: dict[str, Any] = {
                "job_id": job_id,
                "pending": True,
                "amount": float(amount),
                "withdrawable_now": balance.withdrawable_usd,
                "pending_withdrawal": pending,
            }
        else:
            executed = await execute_withdrawal(
                store,
                job_id,
                float(amount),
                wallet_label=label,
                destination=destination,
                by=by,
                equity_before=balance.equity_usd,
            )
            result = {"job_id": job_id, "pending": False, **executed}
    sync_all_jobs(store=store)
    return result


def cancel_venue_withdrawal(
    job_id: str, *, by: str = "owner", store: JobStore | None = None
) -> dict[str, Any]:
    """Drop a queued withdrawal; sizing returns to the full account value on
    the next tick."""
    store = store or JobStore()
    cancelled = cancel_pending_withdrawal(store, job_id, by=by)
    sync_all_jobs(store=store)
    return {
        "job_id": job_id,
        "cancelled": cancelled is not None,
        "pending_withdrawal": cancelled,
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
