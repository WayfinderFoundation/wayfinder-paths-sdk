"""Paper-only A/B execution on the incumbent's completed-bar stream."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.compute_lock import job_state_lock
from wayfinder_paths.jobs.execution.engine import EngineState, run_tick
from wayfinder_paths.jobs.execution.features import apply_precompute
from wayfinder_paths.jobs.execution.job import _load_job_yaml
from wayfinder_paths.jobs.execution.paper import PaperBroker
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionSpec,
    StateSnapshot,
)
from wayfinder_paths.jobs.execution.simulator import (
    _load_strategy,
    _resolve_fee_bps,
    _resolve_maker_fee_bps,
)
from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
from wayfinder_paths.jobs.forward import ForwardRecorder
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.paper_experiment import (
    EXPERIMENT_ARMS,
    EXPERIMENT_FORWARD_ROOT,
    EXPERIMENT_STATE_PATH,
    EXPERIMENT_VIEW_PATH,
    enqueue_experiment_view,
    experiment_status,
    maybe_adjudicate_proposals,
    maybe_finalize_experiment,
    resolve_experiment_bundle,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.monitor_state import atomic_write_json


def active_candidate_shadows(store: JobStore, job_id: str) -> bool:
    state = experiment_status(store, job_id)
    if state.get("status") == "active":
        return True
    return state.get("status") == "qualifying" and any(
        (((state.get("proposals") or {}).get(arm) or {}).get("active"))
        for arm in EXPERIMENT_ARMS
    )


async def run_candidate_shadows(
    store: JobStore,
    job_id: str,
    *,
    view: CompletedBarsView | None = None,
    now: pd.Timestamp | None = None,
) -> list[dict[str, Any]]:
    """Catch both paper arms up to every unseen completed five-minute bar."""
    with job_state_lock(store.repo_root, job_id, name="evolution_shadow_runner"):
        return await _run_candidate_shadows(store, job_id, view=view, now=now)


async def _run_candidate_shadows(
    store: JobStore,
    job_id: str,
    *,
    view: CompletedBarsView | None,
    now: pd.Timestamp | None,
) -> list[dict[str, Any]]:
    if view is not None:
        captured_at = now or pd.Timestamp.now(tz="UTC")
        enqueue_experiment_view(
            store,
            job_id,
            rows=[
                {
                    **row,
                    "timestamp": pd.Timestamp(row["timestamp"]).isoformat(),
                }
                for row in view.to_rows()
            ],
            now=captured_at,
        )
    payload = store.read_json(job_id, EXPERIMENT_VIEW_PATH, default={}) or {}
    rows = payload.get("rows") or []
    if not rows:
        return []
    full_view = CompletedBarsView.from_rows(rows)
    state = experiment_status(store, job_id)
    if state.get("status") not in {"qualifying", "active"}:
        return []
    results: list[dict[str, Any]] = []
    for stamp in full_view.timestamps:
        loop_state = experiment_status(store, job_id)
        if loop_state.get("status") == "active" and stamp >= pd.Timestamp(
            loop_state["ends_at"]
        ):
            maybe_finalize_experiment(
                store, job_id, now=pd.Timestamp(loop_state["ends_at"]).to_pydatetime()
            )
            break
        replay_view = full_view.through(stamp)
        for target in _paper_targets(experiment_status(store, job_id)):
            state = experiment_status(store, job_id)
            last = target.get("last_processed_bar")
            if last and stamp <= pd.Timestamp(last):
                continue
            try:
                row = await _run_target(
                    store,
                    job_id,
                    state=state,
                    target=target,
                    view=replay_view,
                    timestamp=stamp,
                )
            except Exception as exc:  # noqa: BLE001 - one arm cannot stop its peer
                row = _record_error(
                    store,
                    job_id,
                    target=target,
                    timestamp=stamp,
                    error=str(exc)[:300],
                )
            results.append(row)
        maybe_adjudicate_proposals(store, job_id, now=stamp.to_pydatetime())
    maybe_finalize_experiment(store, job_id)
    return results


def _paper_targets(state: dict[str, Any]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    if state.get("status") == "active":
        for arm in EXPERIMENT_ARMS:
            champion = dict(state["arms"][arm]["champion"])
            champion.update(
                {
                    "arm": arm,
                    "role": "champion",
                    "last_processed_bar": state["arms"][arm].get("last_processed_bar"),
                }
            )
            targets.append(champion)
    if state.get("status") in {"qualifying", "active"}:
        for arm in EXPERIMENT_ARMS:
            proposal = ((state.get("proposals") or {}).get(arm) or {}).get("active")
            if not isinstance(proposal, dict):
                continue
            for key in ("candidate", "reference"):
                target = dict(proposal[key])
                target["arm"] = arm
                targets.append(target)
    return targets


async def _run_target(
    store: JobStore,
    job_id: str,
    *,
    state: dict[str, Any],
    target: dict[str, Any],
    view: CompletedBarsView,
    timestamp: pd.Timestamp,
) -> dict[str, Any]:
    root = store.job_dir(job_id).resolve()
    candidate_root = resolve_experiment_bundle(store, job_id, state, target)
    if compute_workspace_revision(candidate_root) != str(target["revision"]):
        raise ValueError("paper candidate revision changed after freeze")
    stream_root = (root / str(target["stream"])).resolve()
    allowed_streams = (root / EXPERIMENT_FORWARD_ROOT).resolve()
    if not stream_root.is_relative_to(allowed_streams):
        raise ValueError("experiment stream escapes its paper root")
    bar_iso = timestamp.isoformat()
    if _stream_has_bar(stream_root / "ticks.jsonl", bar_iso):
        _advance_cursor(store, job_id, target, bar_iso)
        return {
            "arm": target["arm"],
            "role": target["role"],
            "bar_timestamp": bar_iso,
            "reused": True,
        }

    job_data = _load_job_yaml(candidate_root)
    spec_data, _ = resolve_execution_spec(candidate_root, job_data)
    if not spec_data:
        raise FileNotFoundError("candidate execution_spec missing")
    spec = ExecutionSpec.from_dict(spec_data)
    script = store.resolve_script_entrypoint(
        job_id, job_data, candidate_dir=candidate_root
    )
    if script is None or not script.exists():
        raise FileNotFoundError("candidate execution script missing")
    params = dict(job_data.get("execution_params") or {})
    strategy = _load_strategy(script, params)
    candidate_view = apply_precompute(strategy, view)
    shadow_root = _shadow_state_root(
        root,
        arm=str(target["arm"]),
        role=str(target["role"]),
        revision=str(target["revision"]),
    )
    engine_state = EngineState.load(shadow_root / "engine_state.json")
    engine_state.mode = "paper"
    engine_state.revision = str(target["revision"])
    broker = PaperBroker(
        fee_bps=_resolve_fee_bps(params, strategy),
        maker_fee_bps=_resolve_maker_fee_bps(params, strategy),
        slippage_bps=float(params.get("slippage_bps") or 0.0),
    )
    engine_state_pre = engine_state.to_dict()
    tick = await run_tick(
        strategy,
        view=candidate_view,
        brokers={"*": broker},
        state=engine_state,
        spec=spec,
        params=params,
        timestamp=timestamp,
        snapshot=StateSnapshot(status="valid"),
        client_order_prefix=(
            f"paper-ab-{target['arm']}-{target['role']}-{target['revision'][:8]}"
        ),
    )
    engine_state.save(shadow_root / "engine_state.json")
    recorder = ForwardRecorder(
        job_id=job_id,
        forward_dir=stream_root,
        mode="paper",
        revision=engine_state.revision,
    )
    from wayfinder_paths.jobs.execution.driver import _record

    _record(
        recorder,
        tick,
        view=candidate_view,
        params=params,
        now=timestamp,
        engine_state_pre=engine_state_pre,
    )
    _advance_cursor(store, job_id, target, bar_iso)
    (shadow_root / "last_error.json").unlink(missing_ok=True)
    return {
        "arm": target["arm"],
        "role": target["role"],
        "candidate_id": target["candidate_id"],
        "bar_timestamp": bar_iso,
        "skipped": tick.skipped,
        "intents": len(tick.intents),
        "fills": len(tick.fills),
    }


def _advance_cursor(
    store: JobStore, job_id: str, target: dict[str, Any], bar_iso: str
) -> None:
    with job_state_lock(store.repo_root, job_id, name="evolution_experiment"):
        state = experiment_status(store, job_id)
        arm = str(target["arm"])
        if target["role"] == "champion":
            champion = state["arms"][arm]["champion"]
            if champion.get("revision") == target.get("revision"):
                state["arms"][arm]["last_processed_bar"] = bar_iso
        else:
            active = state["proposals"][arm].get("active")
            key = "candidate" if target["role"] == "proposal_candidate" else "reference"
            if isinstance(active, dict) and (active.get(key) or {}).get(
                "revision"
            ) == target.get("revision"):
                active[key]["last_processed_bar"] = bar_iso
        store.write_json(job_id, EXPERIMENT_STATE_PATH, state)


def _stream_has_bar(path: Path, bar_iso: str) -> bool:
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in reversed(lines[-100:]):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("bar_ts") == bar_iso:
            return True
    return False


def _record_error(
    store: JobStore,
    job_id: str,
    *,
    target: dict[str, Any],
    timestamp: pd.Timestamp,
    error: str,
) -> dict[str, Any]:
    root = store.job_dir(job_id).resolve()
    error_root = _shadow_state_root(
        root,
        arm=str(target["arm"]),
        role=str(target["role"]),
        revision=str(target.get("revision") or "invalid"),
    )
    error_path = error_root / "last_error.json"
    try:
        previous = json.loads(error_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    notify = not isinstance(previous, dict) or previous.get("error") != error
    atomic_write_json(
        error_path,
        {
            "arm": target["arm"],
            "role": target["role"],
            "candidate_id": target.get("candidate_id"),
            "error": error,
            "bar_timestamp": timestamp.isoformat(),
            "last_seen_at": utc_now_iso(),
        },
    )
    _increment_error(store, job_id, target, timestamp.isoformat())
    return {
        "arm": target["arm"],
        "role": target["role"],
        "error": error,
        "notify": notify,
    }


def _increment_error(
    store: JobStore, job_id: str, target: dict[str, Any], bar_iso: str
) -> None:
    with job_state_lock(store.repo_root, job_id, name="evolution_experiment"):
        state = experiment_status(store, job_id)
        arm = str(target["arm"])
        if target["role"] == "champion":
            if state["arms"][arm]["champion"].get("revision") == target.get("revision"):
                state["arms"][arm]["last_processed_bar"] = bar_iso
        else:
            active = state["proposals"][arm].get("active")
            key = "candidate" if target["role"] == "proposal_candidate" else "reference"
            if isinstance(active, dict):
                selected = active.get(key) or {}
                if selected.get("revision") == target.get("revision"):
                    selected["last_processed_bar"] = bar_iso
                    selected["error_count"] = int(selected.get("error_count") or 0) + 1
        store.write_json(job_id, EXPERIMENT_STATE_PATH, state)


def _shadow_state_root(root: Path, *, arm: str, role: str, revision: str) -> Path:
    base = (root / "state" / "evolution_shadows").resolve()
    candidate = (base / arm / role / revision).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError("paper arm state escapes the shadow state root")
    return candidate
