"""Event-driven agent wakes: `agent_loop.triggers` consumers.

The agent loop is otherwise a pure timer poll; these hooks let operational
events (a risk halt, a reconcile mismatch, drift) wake the advisor agent
immediately instead of waiting out the interval. Debounced (one wake per
window, however many events matched), suppressed while a proposal application
is in flight (a wake mid-apply would race the paused loops), and raise-free —
a trigger failure must never break the tick that produced it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.store import JobStore

WAKE_STATE_PATH = "state/agent_wake_state.json"
DEFAULT_DEBOUNCE_SECONDS = 600


def fire_triggers(
    store: JobStore,
    job: WayfinderJob,
    event_types: list[str],
    *,
    source: str,
) -> dict[str, Any] | None:
    """Wake the agent worker if any event matches the job's trigger list.

    Returns the wake payload when fired, None when suppressed/no-op.
    """
    try:
        return _fire_triggers(store, job, event_types, source=source)
    except Exception as exc:
        try:
            store.append_journal(
                job.id,
                {"type": "agent_trigger_wake_failed", "error": str(exc)},
            )
        except Exception:
            pass
        return None


def _fire_triggers(
    store: JobStore,
    job: WayfinderJob,
    event_types: list[str],
    *,
    source: str,
) -> dict[str, Any] | None:
    loop = job.agent_loop
    if not loop.enabled or loop.mode == "off":
        return None
    matched = sorted(set(event_types) & set(loop.triggers))
    if not matched:
        return None

    scorecard = store.read_json(job.id, "scorecard.json", default={}) or {}
    if int(scorecard.get("applying_proposal_applications") or 0) > 0:
        return None  # a wake mid-apply would race the paused loops

    root = store.job_dir(job.id)
    wake_path = root / WAKE_STATE_PATH
    debounce = _debounce_seconds(loop)
    now = datetime.now(UTC)
    if wake_path.exists():
        try:
            last = json.loads(wake_path.read_text(encoding="utf-8"))
            last_ts = datetime.fromisoformat(str(last.get("last_triggered_wake_ts")))
            if (now - last_ts).total_seconds() < debounce:
                return None
        except (ValueError, TypeError):
            pass  # unreadable state never blocks a wake

    wake_path.parent.mkdir(parents=True, exist_ok=True)
    wake_path.write_text(
        json.dumps(
            {
                "last_triggered_wake_ts": now.isoformat(),
                "triggers": matched,
                "source": source,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    store.append_journal(
        job.id,
        {
            "type": "agent_triggered_wake",
            "triggers": matched,
            "source": source,
            "mode": loop.mode,
        },
    )
    from wayfinder_paths.jobs.worker import run_job_worker

    wakeup = run_job_worker(job.id, mode=loop.mode)
    return {"triggers": matched, "source": source, "ts": utc_now_iso(), **wakeup}


def _debounce_seconds(loop: Any) -> int:
    raw = getattr(loop, "trigger_debounce_seconds", None)
    return DEFAULT_DEBOUNCE_SECONDS if raw is None else max(int(raw), 0)
