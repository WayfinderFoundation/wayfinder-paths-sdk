"""Paper-only candidate execution on the incumbent's incoming bar stream."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

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
from wayfinder_paths.jobs.probation import PAPER_TIER, load_probation
from wayfinder_paths.jobs.store import JobStore


def active_candidate_shadows(store: JobStore, job_id: str) -> bool:
    return any(
        leg.get("status") == "active"
        and leg.get("tier") == PAPER_TIER
        and leg.get("candidate_bundle_id")
        and leg.get("candidate_bundle")
        and leg.get("shadow_stream")
        for leg in load_probation(store, job_id).get("legs") or []
    )


async def run_candidate_shadows(
    store: JobStore,
    job_id: str,
    *,
    view: CompletedBarsView,
    now: pd.Timestamp,
) -> list[dict[str, Any]]:
    """Run every active evolution probation leg without a venue broker.

    A fresh :class:`PaperBroker` plus durable candidate EngineState gives the
    candidate the same next-open fill model as backtests while guaranteeing it
    cannot route an external order. One bad candidate is isolated from both the
    incumbent tick and its sibling shadows.
    """
    root = store.job_dir(job_id).resolve()
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for leg in load_probation(store, job_id).get("legs") or []:
        if leg.get("status") != "active" or leg.get("tier") != PAPER_TIER:
            continue
        candidate_id = str(leg.get("candidate_bundle_id") or "")
        relative = str(leg.get("candidate_bundle") or "")
        if not candidate_id or not relative or candidate_id in seen:
            continue
        seen.add(candidate_id)
        try:
            candidate_root = (root / relative).resolve()
            campaign_root = (root / "research" / "evolution" / "campaigns").resolve()
            if not candidate_root.is_relative_to(campaign_root):
                raise ValueError("candidate bundle escapes the evolution campaign root")
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
            shadow_root = _shadow_state_root(root, candidate_id)
            state = EngineState.load(shadow_root / "engine_state.json")
            state.mode = "paper"
            state.revision = str(leg.get("candidate_revision") or state.revision or "")
            broker = PaperBroker(
                fee_bps=_resolve_fee_bps(params, strategy),
                maker_fee_bps=_resolve_maker_fee_bps(params, strategy),
                slippage_bps=float(params.get("slippage_bps") or 0.0),
            )
            engine_state_pre = state.to_dict()
            tick = await run_tick(
                strategy,
                view=candidate_view,
                brokers={"*": broker},
                state=state,
                spec=spec,
                params=params,
                timestamp=now,
                snapshot=StateSnapshot(status="valid"),
                client_order_prefix=f"shadow-{candidate_id}",
            )
            state.save(shadow_root / "engine_state.json")
            stream_root = (root / str(leg["shadow_stream"])).resolve()
            allowed_streams = (root / "results" / "forward" / "shadows").resolve()
            if not stream_root.is_relative_to(allowed_streams):
                raise ValueError("candidate shadow stream escapes its paper root")
            recorder = ForwardRecorder(
                job_id=job_id,
                forward_dir=stream_root,
                mode="paper",
                revision=state.revision,
            )
            # Import lazily: driver invokes this module after its helpers have
            # loaded, avoiding a module-import cycle.
            from wayfinder_paths.jobs.execution.driver import _record

            _record(
                recorder,
                tick,
                view=candidate_view,
                params=params,
                now=now,
                engine_state_pre=engine_state_pre,
            )
            (shadow_root / "last_error.json").unlink(missing_ok=True)
            results.append(
                {
                    "candidate_id": candidate_id,
                    "skipped": tick.skipped,
                    "intents": len(tick.intents),
                    "fills": len(tick.fills),
                }
            )
        except Exception as exc:  # noqa: BLE001 - one shadow cannot stop live
            error = str(exc)[:300]
            try:
                error_root = _shadow_state_root(root, candidate_id)
            except ValueError:
                digest = hashlib.sha256(candidate_id.encode()).hexdigest()[:16]
                error_root = root / "state" / "evolution_shadows" / f"invalid-{digest}"
            error_path = error_root / "last_error.json"
            try:
                previous = json.loads(error_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}
            notify = not isinstance(previous, dict) or previous.get("error") != error
            error_path.parent.mkdir(parents=True, exist_ok=True)
            error_path.write_text(
                json.dumps(
                    {
                        "candidate_id": candidate_id,
                        "error": error,
                        "last_seen_at": now.isoformat(),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            results.append(
                {"candidate_id": candidate_id, "error": error, "notify": notify}
            )
    return results


def _shadow_state_root(root: Path, candidate_id: str) -> Path:
    base = (root / "state" / "evolution_shadows").resolve()
    candidate = (base / candidate_id).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError("candidate id escapes the shadow state root")
    return candidate
