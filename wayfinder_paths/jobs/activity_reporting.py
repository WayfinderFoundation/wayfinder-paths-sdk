"""Compact activity snapshots and event-driven monitoring for strategy jobs."""

from __future__ import annotations

from typing import Any

from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.monitor_state import atomic_write_json


def _material(snapshot: dict[str, Any]) -> dict[str, Any]:
    observation = snapshot.get("observation") or {}
    return {
        "status": snapshot.get("status"),
        "reason": snapshot.get("reason"),
        "risk_alert": snapshot.get("risk_alert"),
        "cost_paid": snapshot.get("cost_paid"),
        "operations": snapshot.get("operations"),
        "pending_operations": snapshot.get("pending_operations"),
        "completed_work": snapshot.get("completed_work"),
        "qualifying_activity": snapshot.get("qualifying_activity"),
        "rule_revision": observation.get("rule_revision"),
        "eligible": observation.get("eligible"),
        "readiness": observation.get("readiness"),
        "rewards": [
            {k: v for k, v in row.items() if k != "observed_at"}
            for row in observation.get("rewards") or []
        ],
    }


def record_activity(
    store: JobStore, job_id: str, name: str, snapshot: dict[str, Any]
) -> None:
    relative = f"state/activities/{name}/snapshot.json"
    previous = store.read_json(job_id, relative, default={}) or {}
    changed = _material(previous) != _material(snapshot)
    atomic_write_json(store.job_dir(job_id) / relative, snapshot)
    if not changed:
        return
    store.append_journal(
        job_id, {"type": "strategy_activity", "activity": name, "outcome": snapshot}
    )
    from wayfinder_paths.jobs.triggers import fire_triggers

    # Initial discovery is in the ordinary report. Do not wake on every request.
    if not previous and not snapshot.get("risk_alert"):
        return
    before, after = _material(previous), _material(snapshot)
    # Request lifecycle transitions belong in activity, not extra LLM wakes.
    for value in (before, after):
        for field in (
            "status",
            "reason",
            "cost_paid",
            "operations",
            "pending_operations",
            "completed_work",
            "qualifying_activity",
        ):
            value.pop(field)
    if before == after:
        return
    fire_triggers(
        store,
        store.load(job_id),
        ["risk_halt" if snapshot.get("risk_alert") else "participation_changed"],
        source="activity",
    )


def objective_snapshot(
    store: JobStore, job_id: str, params: dict[str, Any]
) -> dict[str, Any] | None:
    from wayfinder_paths.jobs.activities import objective_strategy

    spec = objective_strategy(params)
    if spec is None:
        return None
    return {
        "has_trading": spec.trading_enabled,
        "primary": spec.primary.model_dump(mode="json"),
        "secondary": [o.model_dump(mode="json") for o in spec.secondary],
        "activities": {
            name: store.read_json(
                job_id, f"state/activities/{name}/snapshot.json", default=None
            )
            for name in spec.activities
        },
        "limits": {
            name: {
                k: v
                for k, v in binding.limits.model_dump(mode="json").items()
                if k not in {"work", "account"}
            }
            for name, binding in spec.activities.items()
        },
    }
