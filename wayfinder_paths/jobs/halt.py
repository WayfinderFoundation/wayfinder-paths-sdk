"""Manual kill switch for a job: instant reduce-only, optional flatten.

`state/halt.json` is the durable flag the driver checks at the start of every
tick — while present, the tick snapshot is forced to `risk_halt` (the engine's
existing non-valid routing: exits still flow, new risk is blocked), queued
non-reduce-only intents are canceled, and with `flatten: true` all open
positions are market-closed. The flag survives runner pause/resume cycles and
proposal applies by design: resuming loops must never silently un-halt.

Deliberately independent of `evaluate_live_gate` (promotion readiness) and of
risk-limit halts (`workspace/risk_limits.json`): this is the human/agent "stop
now" control.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

HALT_PATH = "state/halt.json"
HALTED_EXECUTION_STATUS = "halted"


def read_halt(root: Path) -> dict[str, Any] | None:
    """Store-free read for the driver hot path."""
    path = Path(root) / HALT_PATH
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        # An unparseable halt file still means "someone tried to stop this
        # job" — fail safe by treating it as an active halt.
        return {"reason": "unreadable halt file", "flatten": False}
    match loaded:
        case dict():
            return loaded
        case _:
            return None


def request_halt(
    store: JobStore,
    job_id: str,
    *,
    reason: str | None = None,
    flatten: bool = False,
    source: str = "manual",
) -> dict[str, Any]:
    """Idempotent one-shot halt. `flatten=True` may be set on the initial
    call or added to an existing halt; it is never cleared implicitly."""
    root = store.job_dir(job_id)
    existing = read_halt(root) or {}
    scorecard = store.read_json(job_id, "scorecard.json", default={}) or {}
    payload = {
        "reason": reason or existing.get("reason") or "manual halt",
        "ts": existing.get("ts") or utc_now_iso(),
        "flatten": bool(flatten or existing.get("flatten")),
        "source": existing.get("source") or source,
        # Restored on clear so an agent-written status isn't lost.
        "prior_live_execution_status": existing.get(
            "prior_live_execution_status",
            scorecard.get("live_execution_status"),
        ),
    }
    path = root / HALT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if not existing:
        store.append_journal(
            job_id,
            {
                "type": "halt_requested",
                "reason": payload["reason"],
                "flatten": payload["flatten"],
                "source": payload["source"],
            },
        )
    elif payload["flatten"] and not existing.get("flatten"):
        store.append_journal(
            job_id, {"type": "halt_flatten_requested", "source": source}
        )
    store.refresh_scorecard(job_id, {"live_execution_status": HALTED_EXECUTION_STATUS})
    return payload


def clear_halt(store: JobStore, job_id: str) -> dict[str, Any]:
    root = store.job_dir(job_id)
    existing = read_halt(root)
    path = root / HALT_PATH
    if path.exists():
        path.unlink()
    if existing is not None:
        store.append_journal(job_id, {"type": "halt_cleared"})
        store.refresh_scorecard(
            job_id,
            {"live_execution_status": existing.get("prior_live_execution_status")},
        )
    return {"cleared": existing is not None, "previous": existing}
