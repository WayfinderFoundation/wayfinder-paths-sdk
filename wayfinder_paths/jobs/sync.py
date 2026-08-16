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
            out["agent_mode"] = str(aenv["WAYFINDER_JOB_AGENT_MODE"])
    return out


def snapshot_job(job_id: str, *, store: JobStore | None = None) -> dict[str, Any]:
    store = store or JobStore()
    job = store.load(job_id)
    scorecard = store.read_json(job_id, "scorecard.json", default={}) or {}
    # Reflect the live runner/engine state, not the declared job.yaml: mode
    # (paper/live), agent_mode, active_revision, paused, and scheduling/health
    # metrics all come from the runner where it is the source of truth. See
    # _runtime_reconciliation. Degrades to the declared scorecard on a down
    # runner, so a sync never breaks.
    runtime = _runtime_reconciliation(job, store)
    if runtime:
        scorecard = {**scorecard, **runtime}
    runner_links = store.read_json(job_id, "runner_links.json", default={}) or {}
    latest_monitor = _report_with_session(store, job_id, "monitor")
    latest_intervene = _report_with_session(store, job_id, "intervene", "improve")
    latest_auto = _report_with_session(store, job_id, "auto", "decide")
    latest_apply = _report_with_session(store, job_id, "apply")
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
        "post_apply_shadow": _shadow_topline(store, job_id),
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
        "gate": evaluate_live_gate(job_id, store=store),
        # Manual kill-switch detail (contract C4): scorecard already reports
        # live_execution_status="halted" while set; this carries reason/ts.
        "halt": read_halt(store.job_dir(job_id)),
        "features": features,
    }


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
