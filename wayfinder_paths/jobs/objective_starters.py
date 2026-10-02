"""Observation-first objective strategies, not return-certified trading starters."""

from __future__ import annotations

from typing import Any

from wayfinder_paths.jobs.freestyle.create import create_freestyle_job
from wayfinder_paths.jobs.store import JobStore

_DEFINITIONS = (
    (
        "flop-participation",
        "FLOP useful inference",
        "flop",
        "testnet",
        "FLOP_TEST",
        "qualifying_activity",
        "activities",
        "Useful inference within a compute budget; deployed API and eligibility must be verified.",
    ),
    (
        "risex-participation",
        "RISEx bounded participation",
        "risex",
        "season-1",
        "USD",
        "confirmed_rewards",
        "RISE_POINTS",
        "Observe account points and costs; hedged live execution is not enabled.",
    ),
    (
        "perptools-participation",
        "PERPTools attributed participation",
        "perptools",
        "season-1",
        "USD",
        "confirmed_rewards",
        "PERPTOOLS_POINTS",
        "Verify broker/account attribution before execution; excludes Tickets and AI Arena.",
    ),
    (
        "imd-contributor",
        "IMD contributor",
        "imd",
        "contributor",
        "USD",
        "completed_work",
        "accepted_jobs",
        "Monitor an owned seat and earnings; no seat purchase or worker enrollment.",
    ),
)


def objective_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": id_,
            "name": name,
            "protocol": protocol,
            "summary": summary,
            "execution_contract": "freestyle_v1",
            "readiness": "observe_only",
            "live_execution_available": False,
            "objective": {"metric": metric, "direction": "maximize", "unit": unit},
        }
        for id_, name, protocol, _, _, metric, unit, summary in _DEFINITIONS
    ]


def create_objective_strategy(
    starter_id: str,
    *,
    job_id: str | None = None,
    account: str = "",
    seat_id: int | None = None,
    store: JobStore | None = None,
    compile_job: bool = True,
    initializer_session_id: str | None = None,
) -> dict[str, Any]:
    definition = next((row for row in _DEFINITIONS if row[0] == starter_id), None)
    if definition is None:
        raise ValueError(f"unknown objective strategy: {starter_id}")
    id_, name, protocol, program, cost_unit, metric, unit, summary = definition
    store = store or JobStore()
    if (store.job_dir(job_id or id_) / "job.yaml").exists():
        raise FileExistsError(f"job already exists: {job_id or id_}")
    return create_freestyle_job(
        job_id or id_,
        name=name,
        goal=summary,
        script_source='def tick(ctx):\n    ctx.participate("main")\n',
        interval_seconds=900,
        timeout_seconds=120,
        agent_mode="monitor",
        store=store,
        compile_job=compile_job,
        initializer_session_id=initializer_session_id,
        execution_params={
            "objective_strategy": {
                "trading_enabled": False,
                "primary": {"metric": metric, "direction": "maximize", "unit": unit},
                "secondary": [
                    {"metric": "cost_paid", "direction": "minimize", "unit": cost_unit}
                ],
                "activities": {
                    "main": {
                        "capability": f"{protocol}.participation",
                        "limits": {
                            "protocol": protocol,
                            "program": program,
                            "account": account,
                            "rule_revision": "research-2026-10-01",
                            "enabled": False,
                            "cost_unit": cost_unit,
                            "max_total_cost": 0,
                            "max_daily_cost": 0,
                            "max_operation_cost": 0,
                        },
                        "options": {"seat_id": seat_id} if seat_id is not None else {},
                    }
                },
            }
        },
    )
