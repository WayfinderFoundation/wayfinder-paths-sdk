"""Notification policy: which job events reach the owner, on which channel,
and when they stay quiet.

``job.reporting["notify"]`` = ``{"channels": [...], "on": [...],
"quiet_hours": {"start": "22:00", "end": "07:00", "tz": "Europe/London"}}``.
Every event that passes through ``fire_triggers`` is offered here; the chat
channel is the job's own result marker (already on every wake), email/sms go
through the notify client. A per-job ledger spaces repeats of the same event
so a flapping halt cannot burn the owner's daily budget.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.store import JobStore

NOTIFY_LEDGER_PATH = "state/notify_ledger.json"
NOTIFY_CHANNELS: tuple[str, ...] = ("chat", "email", "sms")
DEFAULT_ON_HARNESSED: tuple[str, ...] = ("health_red", "risk_halt", "proposal_created")
DEFAULT_ON_SCRIPT: tuple[str, ...] = ("script_failure", "risk_halt", "runner_loop_gap")
NOTIFIABLE_EVENTS: frozenset[str] = frozenset(
    {
        "script_failure",
        "drift_warning",
        "health_red",
        "proposal_created",
        "reconcile_mismatch",
        "risk_halt",
        "regime_shift",
        "runner_loop_gap",
        "disk_pressure",
        "verdict_matured",
        "research_impasse",
        "successor_overdue",
        "regime_remediation_due",
        "proposal_restage_requested",
        "path_upgrade_available",
        "launched",
    }
)
MIN_SPACING_S = 3600
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def default_notifications(job: WayfinderJob) -> dict[str, Any]:
    on = (
        DEFAULT_ON_HARNESSED
        if job.execution_contract == "jobs_v1"
        else DEFAULT_ON_SCRIPT
    )
    return {"channels": ["chat", "email"], "on": list(on), "quiet_hours": None}


def normalize_notifications(raw: Any) -> dict[str, Any]:
    data = dict(raw or {})
    channels = [str(c).lower() for c in data.get("channels") or ["chat", "email"]]
    unknown_channels = sorted(set(channels) - set(NOTIFY_CHANNELS))
    if unknown_channels:
        raise ValueError(
            f"unknown notification channels {unknown_channels}; use {list(NOTIFY_CHANNELS)}"
        )
    on = [str(e) for e in data.get("on") or []]
    unknown_events = sorted(set(on) - NOTIFIABLE_EVENTS)
    if unknown_events:
        raise ValueError(
            f"unknown notification events {unknown_events}; known: {sorted(NOTIFIABLE_EVENTS)}"
        )
    quiet = data.get("quiet_hours")
    if quiet:
        quiet = dict(quiet)
        start, end = str(quiet.get("start") or ""), str(quiet.get("end") or "")
        if not (_HHMM.match(start) and _HHMM.match(end)):
            raise ValueError("quiet_hours needs start and end as HH:MM")
        tz = str(quiet.get("tz") or "UTC")
        ZoneInfo(tz)  # raises on an unknown zone
        quiet = {"start": start, "end": end, "tz": tz}
    else:
        quiet = None
    return {
        "channels": sorted(set(channels), key=NOTIFY_CHANNELS.index),
        "on": sorted(set(on)),
        "quiet_hours": quiet,
    }


def notifications_for(job: WayfinderJob) -> dict[str, Any]:
    configured = (job.reporting or {}).get("notify")
    if isinstance(configured, dict) and configured:
        try:
            return normalize_notifications(configured)
        except ValueError:
            return default_notifications(job)
    return default_notifications(job)


def in_quiet_hours(quiet: dict[str, Any] | None, now: datetime | None = None) -> bool:
    if not quiet:
        return False
    now = now or datetime.now(UTC)
    local = now.astimezone(ZoneInfo(str(quiet.get("tz") or "UTC")))
    start = time.fromisoformat(str(quiet["start"]))
    end = time.fromisoformat(str(quiet["end"]))
    current = local.time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def notify_decision(
    job: WayfinderJob, event: str, *, now: datetime | None = None
) -> dict[str, Any]:
    policy = notifications_for(job)
    if event not in policy["on"]:
        return {
            "deliver": False,
            "channels": [],
            "reason": "event not in the notify list",
        }
    channels = [c for c in policy["channels"] if c != "chat"]
    if in_quiet_hours(policy.get("quiet_hours"), now):
        return {"deliver": False, "channels": [], "reason": "quiet hours"}
    if not channels:
        return {
            "deliver": False,
            "channels": [],
            "reason": "chat only (the wake report carries it)",
        }
    return {"deliver": True, "channels": channels, "reason": "policy"}


def notify_events(
    store: JobStore,
    job: WayfinderJob,
    events: list[str],
    *,
    source: str,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Deliver the events the policy allows, spaced per event type. Raise-free."""
    sent: list[dict[str, Any]] = []
    now = now or datetime.now(UTC)
    try:
        ledger = store.read_json(job.id, NOTIFY_LEDGER_PATH, default={}) or {}
        last_sent = dict(ledger.get("last_sent") or {})
        for event in sorted(set(events)):
            decision = notify_decision(job, event, now=now)
            if not decision["deliver"]:
                continue
            previous = last_sent.get(event)
            if previous:
                try:
                    elapsed = (
                        now - datetime.fromisoformat(str(previous))
                    ).total_seconds()
                except ValueError:
                    elapsed = MIN_SPACING_S
                if elapsed < MIN_SPACING_S:
                    continue
            title = f"[{job.name}] {event.replace('_', ' ')}"
            body = (
                f"Job `{job.id}` raised `{event}` from {source} at {now.isoformat()}.\n\n"
                f"Watch level: {job.agent_loop.mode}. Open the job for the report and the next step."
            )
            outcome = _deliver(title, body, decision["channels"])
            last_sent[event] = now.isoformat()
            row = {
                "event": event,
                "channels": decision["channels"],
                "source": source,
                "at": utc_now_iso(),
                **outcome,
            }
            sent.append(row)
            store.append_journal(job.id, {"type": "notification_sent", **row})
        if sent:
            ledger["last_sent"] = last_sent
            store.write_json(job.id, NOTIFY_LEDGER_PATH, ledger)
    except Exception as exc:  # noqa: BLE001 — a notification must never break the event that produced it
        try:
            store.append_journal(
                job.id, {"type": "notification_failed", "error": str(exc)[:300]}
            )
        except Exception:  # noqa: BLE001
            pass
    return sent


def _deliver(title: str, body: str, channels: list[str]) -> dict[str, Any]:
    from wayfinder_paths.core.clients.NotifyClient import NOTIFY_CLIENT

    results: dict[str, Any] = {}
    for channel in channels:
        coro = NOTIFY_CLIENT.notify(
            title=title[:200], message=body[:20_000], delivery=channel
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        try:
            if loop is not None and loop.is_running():
                loop.create_task(coro)
                results[channel] = "scheduled"
            else:
                asyncio.run(coro)
                results[channel] = "sent"
        except Exception as exc:  # noqa: BLE001
            results[channel] = f"failed: {str(exc)[:200]}"
    return {"delivery": results}


__all__ = [
    "NOTIFIABLE_EVENTS",
    "NOTIFY_CHANNELS",
    "default_notifications",
    "in_quiet_hours",
    "normalize_notifications",
    "notifications_for",
    "notify_decision",
    "notify_events",
]
