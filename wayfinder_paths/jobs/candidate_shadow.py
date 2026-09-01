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
    resolve_compute_window,
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
from wayfinder_paths.jobs.probation import (
    PROBATION_FORWARD_ROOT,
    PROBATION_VIEW_PATH,
    active_probation_trials,
    enqueue_probation_view,
    maybe_adjudicate_probation,
    probation_targets,
    resolve_probation_bundle,
    update_probation_target,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.monitor_state import atomic_write_json


def active_candidate_shadows(store: JobStore, job_id: str) -> bool:
    return active_probation_trials(store, job_id)


async def run_candidate_shadows(
    store: JobStore,
    job_id: str,
    *,
    view: CompletedBarsView | None = None,
    now: pd.Timestamp | None = None,
) -> list[dict[str, Any]]:
    """Catch every probation pair up to each unseen completed bar."""
    with job_state_lock(store.repo_root, job_id, name="probation_shadow_runner"):
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
        enqueue_probation_view(
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
    payload = store.read_json(job_id, PROBATION_VIEW_PATH, default={}) or {}
    rows = payload.get("rows") or []
    if not rows:
        return []
    full_view = CompletedBarsView.from_rows(rows)
    if not active_probation_trials(store, job_id):
        return []
    results: list[dict[str, Any]] = []
    targets = probation_targets(store, job_id)
    for stamp in full_view.timestamps:
        replay_view = full_view.through(stamp)
        advanced = False
        for target in targets:
            last = target.get("last_processed_bar")
            if last and stamp <= pd.Timestamp(last):
                continue
            advanced = True
            try:
                row = await _run_target(
                    store,
                    job_id,
                    target=target,
                    view=replay_view,
                    timestamp=stamp,
                )
            except Exception as exc:  # noqa: BLE001 - one trial cannot stop its peers
                row = _record_error(
                    store,
                    job_id,
                    target=target,
                    timestamp=stamp,
                    error=str(exc)[:300],
                )
            results.append(row)
        if advanced:
            outcomes = maybe_adjudicate_probation(
                store, job_id, now=stamp.to_pydatetime()
            )
            if outcomes:
                targets = probation_targets(store, job_id)
    maybe_adjudicate_probation(
        store,
        job_id,
        now=now.to_pydatetime() if now is not None else None,
    )
    return results


async def _run_target(
    store: JobStore,
    job_id: str,
    *,
    target: dict[str, Any],
    view: CompletedBarsView,
    timestamp: pd.Timestamp,
) -> dict[str, Any]:
    root = store.job_dir(job_id).resolve()
    candidate_root = resolve_probation_bundle(store, job_id, target)
    if compute_workspace_revision(candidate_root) != str(target["revision"]):
        raise ValueError("paper candidate revision changed after freeze")
    stream_root = (root / str(target["stream"])).resolve()
    allowed_streams = (root / PROBATION_FORWARD_ROOT).resolve()
    legacy_streams = (root / "results/forward/experiment").resolve()
    if not (
        stream_root.is_relative_to(allowed_streams)
        or (
            target.get("bundle_scope") == "legacy_experiment"
            and stream_root.is_relative_to(legacy_streams)
        )
    ):
        raise ValueError("probation stream escapes its paper root")
    bar_iso = timestamp.isoformat()
    if _stream_has_bar(stream_root / "ticks.jsonl", bar_iso):
        _advance_cursor(store, job_id, target, bar_iso)
        return {
            "trial_id": target["trial_id"],
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
    # Same bounded window the simulator replays with and the live driver
    # fetches — the shadow lane must not hand candidates deeper history than
    # production would.
    window = resolve_compute_window(params, strategy)
    view = window.slice_view(view, len(view.timestamps) - 1)
    candidate_view = apply_precompute(strategy, view)
    shadow_root = _target_shadow_state_root(root, target)
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
            f"paper-ab-{target['legacy_shadow_arm']}-champion-{target['revision'][:8]}"
            if target.get("bundle_scope") == "legacy_experiment"
            else (
                f"probation-{target['trial_id'][:12]}-{target['role']}-"
                f"{target['revision'][:8]}"
            )
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
        "trial_id": target["trial_id"],
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
    update_probation_target(store, job_id, target, bar_iso=bar_iso)


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
    error_root = _target_shadow_state_root(root, target)
    error_path = error_root / "last_error.json"
    try:
        previous = json.loads(error_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    notify = not isinstance(previous, dict) or previous.get("error") != error
    atomic_write_json(
        error_path,
        {
            "trial_id": target["trial_id"],
            "role": target["role"],
            "candidate_id": target.get("candidate_id"),
            "error": error,
            "bar_timestamp": timestamp.isoformat(),
            "last_seen_at": utc_now_iso(),
        },
    )
    _increment_error(store, job_id, target, timestamp.isoformat())
    return {
        "trial_id": target["trial_id"],
        "role": target["role"],
        "error": error,
        "notify": notify,
    }


def _increment_error(
    store: JobStore, job_id: str, target: dict[str, Any], bar_iso: str
) -> None:
    update_probation_target(store, job_id, target, bar_iso=bar_iso, error=True)


def _shadow_state_root(
    root: Path,
    *,
    trial_id: str,
    phase: str,
    role: str,
    revision: str,
) -> Path:
    base = (root / "state" / "probation_shadows").resolve()
    candidate = (base / trial_id / phase / role / revision).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError("probation state escapes the shadow state root")
    return candidate


def _target_shadow_state_root(root: Path, target: dict[str, Any]) -> Path:
    """Preserve legacy champion engine state when migrating a live A/B."""
    if target.get("bundle_scope") != "legacy_experiment":
        return _shadow_state_root(
            root,
            trial_id=str(target["trial_id"]),
            phase=str(target["phase"]),
            role=str(target["role"]),
            revision=str(target.get("revision") or "invalid"),
        )
    arm = str(target.get("legacy_shadow_arm") or "")
    role = str(target.get("legacy_shadow_role") or "")
    if arm not in {"control", "evolution"} or role != "champion":
        raise ValueError("legacy probation target has an invalid shadow state key")
    base = (root / "state" / "evolution_shadows").resolve()
    candidate = (base / arm / role / str(target["revision"])).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError("legacy probation state escapes the shadow state root")
    return candidate
