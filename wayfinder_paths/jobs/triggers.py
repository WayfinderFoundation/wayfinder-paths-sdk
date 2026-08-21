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

# Infrastructure events wake the agent regardless of the job's configured
# trigger list: they represent work the pipeline is waiting on the agent for
# (not market conditions the owner opted into watching).
# verdict_matured fires at most once per promotion — a matured forward
# verdict is a research event every job should act on, config or not.
# successor_overdue fires when a kind=process owner rejection's expected
# successor proposal has not appeared within the watchdog window.
# runner_loop_gap fires when the watchdog sees a LIVE job's script loop gone
# dark past 3x its interval — a live book nobody is managing must never wait
# for the next timer poll.
# disk_pressure fires when the watchdog sees the jobs volume past its alert
# threshold — a full disk kills trading exactly like a stalled loop.
# research_impasse fires when a research-stale job's last K wakes produced
# zero progress artifacts — the wake it triggers carries the hatch-stripped
# progress mandate (worker renders it from state/research_impasse.json).
# regime_shift is a deterministic incumbent-health warning/critical transition;
# legacy jobs predate the configurable trigger name, so it always wakes.
# regime_remediation_due retries an open remediation case until it produces a
# proposal, bounded evaluation artifact, or structured blocker.
ALWAYS_WAKE_EVENTS = {
    "proposal_restage_requested",
    "verdict_matured",
    "successor_overdue",
    "runner_loop_gap",
    "disk_pressure",
    "research_impasse",
    "regime_shift",
    "regime_remediation_due",
}


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
    events = set(event_types)
    matched = sorted((events & set(loop.triggers)) | (events & ALWAYS_WAKE_EVENTS))
    if not matched:
        return None

    scorecard = store.read_json(job.id, "scorecard.json", default={}) or {}
    if int(scorecard.get("applying_proposal_applications") or 0) > 0:
        return None  # a wake mid-apply would race the paused loops

    root = store.job_dir(job.id)
    wake_path = root / WAKE_STATE_PATH
    debounce = _debounce_seconds(loop)
    now = datetime.now(UTC)
    wake_state: dict[str, Any] = {}
    if wake_path.exists():
        try:
            loaded = json.loads(wake_path.read_text(encoding="utf-8"))
            wake_state = loaded if isinstance(loaded, dict) else {}
        except (ValueError, TypeError):
            pass  # unreadable state never blocks a wake

    event_times = wake_state.get("events")
    event_times = dict(event_times) if isinstance(event_times, dict) else {}
    legacy_triggers = set(wake_state.get("triggers") or [])
    due: list[str] = []
    for event in matched:
        timestamp = event_times.get(event)
        if timestamp is None and event in legacy_triggers:
            timestamp = wake_state.get("last_triggered_wake_ts")
        try:
            last_ts = datetime.fromisoformat(str(timestamp))
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            due.append(event)
            continue
        if (now - last_ts).total_seconds() >= debounce:
            due.append(event)
    if not due:
        return None

    for event in due:
        event_times[event] = now.isoformat()

    wake_path.parent.mkdir(parents=True, exist_ok=True)
    wake_path.write_text(
        json.dumps(
            {
                "last_triggered_wake_ts": now.isoformat(),
                "triggers": due,
                "source": source,
                "events": event_times,
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
            "triggers": due,
            "source": source,
            "mode": loop.mode,
        },
    )
    # circular import: worker → application → validation → execution.preflight
    # → execution.driver → triggers
    from wayfinder_paths.jobs.worker import run_job_worker

    wakeup = run_job_worker(job.id, mode=loop.mode)
    return {"triggers": due, "source": source, "ts": utc_now_iso(), **wakeup}


def _debounce_seconds(loop: Any) -> int:
    raw = getattr(loop, "trigger_debounce_seconds", None)
    return DEFAULT_DEBOUNCE_SECONDS if raw is None else max(int(raw), 0)
