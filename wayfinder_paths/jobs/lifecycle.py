"""Bootstrap contract and monitor-decay parking for rotting jobs.

A job that never reaches operational state consumes wakes, disk, and owner
attention forever — nothing in the pipeline ever concluded "this job failed
to bootstrap".  This module owns that conclusion in the mechanical zone
(paper-mode, wallet-unbound jobs ONLY — anything live or one operator flip
from live is never auto-touched): past half the bootstrap deadline the job is
nudged once and every wake prompt leads with a bootstrap directive; past the
full deadline it is parked — runner loops paused with the same machinery as
the ``wayfinder job pause`` verb, a durable failure record written, and the
decision journaled with a bounded undo.  Monitor-mode jobs whose deploy
evidence stopped replicating rot the same way and share the park path.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.owner_attention import job_live_capital_risk
from wayfinder_paths.jobs.replication import load_replication
from wayfinder_paths.jobs.runner_bridge import RunnerBridge
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import sync_all_jobs

LIFECYCLE_PATH = "state/lifecycle.json"
BOOTSTRAP_FAILURE_PATH = "state/bootstrap_failure.json"
FORWARD_TRADES_PATH = "results/forward/trades.jsonl"
BASELINE_BACKTEST_PATH = "results/backtest/baseline.json"
BOOTSTRAP_DEADLINE_HOURS_ENV = "WAYFINDER_BOOTSTRAP_DEADLINE_HOURS"
DEFAULT_BOOTSTRAP_DEADLINE_HOURS = 72.0
MONITOR_DECAY_PARK_DAYS_ENV = "WAYFINDER_MONITOR_DECAY_PARK_DAYS"
DEFAULT_MONITOR_DECAY_PARK_DAYS = 7.0
# Kill-switch: "0" disables both park paths. The nudge journal and the wake
# prompt directive stay — visibility is never switched off.
LIFECYCLE_REAPER_ENV = "WAYFINDER_LIFECYCLE_REAPER"
_DECAYED_REPLICATION_STATUSES = frozenset({"decayed", "invalid"})
# Fleet-level sweep throttle (fleet artifact convention, like the portfolio
# report): the watchdog calls the sweep every pass, the throttle makes it a
# daily decision cadence.
SWEEP_STATE_RELPATH = Path(".wayfinder") / "lifecycle" / "sweep.json"
SWEEP_INTERVAL_SECONDS = 24 * 3600

BOOTSTRAP_DIRECTIVE = (
    "BOOTSTRAP CONTRACT — this job has never reached operational state. "
    "FIRST priority this wake: complete bootstrap (compile, dataset, "
    "baseline backtest, first tick) or journal a concrete blocker.\n\n"
)


def is_operational(store: JobStore, job_id: str) -> bool:
    """Has this job EVER produced a sign of life? Any completed script run or
    agent wake, a baseline backtest, or a closed forward trade counts."""
    scorecard = store.read_json(job_id, "scorecard.json", default={}) or {}
    if scorecard.get("last_script_run_at") or scorecard.get("last_agent_check_at"):
        return True
    if (store.job_dir(job_id) / BASELINE_BACKTEST_PATH).exists():
        return True
    return closed_forward_trades(store, job_id) > 0


def closed_forward_trades(store: JobStore, job_id: str) -> int:
    """Same source of truth as the wake prompt's standing-checks block: the
    forward trades ledger, not the summary (which can lag a recorder bug)."""
    return len(store.read_jsonl(job_id, FORWARD_TRADES_PATH))


def bootstrap_directive(store: JobStore, job_id: str) -> str:
    """Wake prompt directive for a never-operational job past half its
    deadline. Rendered regardless of the reaper kill-switch."""
    job = store.load(job_id)
    now = dt.datetime.now(dt.UTC)
    if _job_age_hours(job, now) <= _deadline_hours() / 2:
        return ""
    if is_operational(store, job_id):
        return ""
    return BOOTSTRAP_DIRECTIVE


def lifecycle_sweep(
    store: JobStore,
    *,
    now: dt.datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Apply the bootstrap contract and monitor-decay policy to every job.

    Throttled to at most one pass per day (the watchdog calls it every 5
    minutes); ``force`` bypasses the throttle. Never raises per-job — one
    broken job record must not stop the sweep.
    """
    now = now or dt.datetime.now(dt.UTC)
    state_path = store.repo_root / SWEEP_STATE_RELPATH
    if not force and _sweep_ran_recently(state_path, now):
        return {"throttled": True, "scanned": 0, "actions": [], "errors": []}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"ran_at": now.isoformat()}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    actions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    scanned = 0
    for job in store.list_jobs():
        scanned += 1
        try:
            actions.extend(_sweep_job(store, job, now))
        except Exception as exc:  # noqa: BLE001 — sweep the remaining jobs
            errors.append({"job_id": job.id, "error": str(exc)[:300]})
    if any(str(action.get("action") or "").startswith("job_parked") for action in actions):
        try:
            sync_all_jobs(store=store)
        except Exception:  # noqa: BLE001 — the park stands; next sync reflects it
            pass
    return {"throttled": False, "scanned": scanned, "actions": actions, "errors": errors}


def reaper_enabled() -> bool:
    return os.environ.get(LIFECYCLE_REAPER_ENV) != "0"


def _sweep_job(
    store: JobStore, job: WayfinderJob, now: dt.datetime
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    marker = store.read_json(job.id, LIFECYCLE_PATH, default={}) or {}
    operational = is_operational(store, job.id)
    age_hours = _job_age_hours(job, now)
    deadline = _deadline_hours()
    parked = bool(store.read_json(job.id, BOOTSTRAP_FAILURE_PATH))
    exempt = job_live_capital_risk(job)

    if (
        not operational
        and age_hours > deadline / 2
        and not marker.get("bootstrap_nudged_at")
    ):
        marker["bootstrap_nudged_at"] = now.isoformat()
        store.write_json(job.id, LIFECYCLE_PATH, marker)
        store.append_journal(
            job.id,
            {
                "type": "bootstrap_lagging",
                "age_hours": round(age_hours, 1),
                "deadline_hours": deadline,
                "predicates_failed": _operational_predicates_failed(store, job.id),
            },
        )
        actions.append({"job_id": job.id, "action": "bootstrap_lagging"})

    if (
        not operational
        and age_hours > deadline
        and not exempt
        and not parked
        and reaper_enabled()
    ):
        actions.append(
            _park(
                store,
                job,
                reason=(
                    f"never operational within the {deadline:g}h bootstrap deadline"
                ),
                journal_type="job_parked_unbootstrapped",
                predicates_failed=_operational_predicates_failed(store, job.id),
            )
        )
        return actions  # parked — nothing further to evaluate this pass

    decay_action = _monitor_decay(
        store, job, marker, now, exempt=exempt, parked=parked
    )
    if decay_action is not None:
        actions.append(decay_action)
    return actions


def _monitor_decay(
    store: JobStore,
    job: WayfinderJob,
    marker: dict[str, Any],
    now: dt.datetime,
    *,
    exempt: bool,
    parked: bool,
) -> dict[str, Any] | None:
    """Park a monitor-mode job whose deploy evidence stopped replicating and
    that shows no forward trades and no journal activity for the window."""
    if str(job.agent_loop.mode or "off") != "monitor":
        return None
    replication = load_replication(store, job.id) or {}
    status = str(replication.get("status") or "")
    if (
        not replication.get("available")
        or status not in _DECAYED_REPLICATION_STATUSES
    ):
        if marker.pop("monitor_decay", None) is not None:
            store.write_json(job.id, LIFECYCLE_PATH, marker)
        return None
    decay = dict(marker.get("monitor_decay") or {})
    if not decay.get("first_seen_at"):
        marker["monitor_decay"] = {"status": status, "first_seen_at": now.isoformat()}
        store.write_json(job.id, LIFECYCLE_PATH, marker)
        return None
    if decay.get("status") != status:
        # decayed<->invalid flips stay inside the bad set — keep the clock.
        marker["monitor_decay"] = {**decay, "status": status}
        store.write_json(job.id, LIFECYCLE_PATH, marker)
    first_seen = _parse_time(decay.get("first_seen_at"))
    park_days = _decay_park_days()
    if first_seen is None or now - first_seen < dt.timedelta(days=park_days):
        return None
    if closed_forward_trades(store, job.id) > 0:
        return None
    if _journal_activity_since(store, job.id, str(decay.get("first_seen_at"))):
        return None
    if exempt or parked or not reaper_enabled():
        return None
    return _park(
        store,
        job,
        reason=(
            f"monitor-mode job with backtest replication {status} for more "
            f"than {park_days:g} days, zero forward trades, and no journal "
            "activity in the window"
        ),
        journal_type="job_parked_monitor_decay",
        predicates_failed=[
            f"replication_{status}",
            "no_forward_trades",
            "no_journal_activity",
        ],
    )


def _park(
    store: JobStore,
    job: WayfinderJob,
    *,
    reason: str,
    journal_type: str,
    predicates_failed: list[str],
) -> dict[str, Any]:
    # Same machinery as the `wayfinder job pause` CLI verb: RunnerBridge
    # pause per enabled loop. The undo is the matching `resume` verb.
    bridge = RunnerBridge(repo_root=store.repo_root)
    responses = [
        {
            "runner_job_name": loop.runner_job_name,
            "response": bridge.pause(loop.runner_job_name),
        }
        for loop in (job.script_loop, job.agent_loop)
        if loop.enabled
    ]
    store.write_json(
        job.id,
        BOOTSTRAP_FAILURE_PATH,
        {
            "reason": reason,
            "parked_at": utc_now_iso(),
            "predicates_failed": predicates_failed,
        },
    )
    undo = {"command": f"wayfinder job resume {job.id}"}
    store.append_journal(
        job.id,
        {
            "type": journal_type,
            "reason": reason,
            "predicates_failed": predicates_failed,
            "undo": undo,
            "runner_responses": responses,
        },
    )
    return {"job_id": job.id, "action": journal_type, "reason": reason}


def _operational_predicates_failed(store: JobStore, job_id: str) -> list[str]:
    scorecard = store.read_json(job_id, "scorecard.json", default={}) or {}
    failed: list[str] = []
    if not scorecard.get("last_script_run_at"):
        failed.append("no_script_run")
    if not scorecard.get("last_agent_check_at"):
        failed.append("no_agent_check")
    if not (store.job_dir(job_id) / BASELINE_BACKTEST_PATH).exists():
        failed.append("no_baseline_backtest")
    if closed_forward_trades(store, job_id) == 0:
        failed.append("no_forward_trades")
    return failed


def _journal_activity_since(store: JobStore, job_id: str, since_ts: str) -> bool:
    # Rows are append-ordered, so the tail bounds the newest timestamps.
    return any(
        str(row.get("ts") or "") > since_ts
        for row in store.read_jsonl(job_id, "journal.jsonl", limit=200)
    )


def _sweep_ran_recently(state_path: Path, now: dt.datetime) -> bool:
    if not state_path.exists():
        return False
    try:
        doc = json.loads(state_path.read_text(encoding="utf-8"))
    except ValueError:
        return False
    ran_at = _parse_time(doc.get("ran_at")) if isinstance(doc, dict) else None
    return (
        ran_at is not None
        and (now - ran_at).total_seconds() < SWEEP_INTERVAL_SECONDS
    )


def _job_age_hours(job: WayfinderJob, now: dt.datetime) -> float:
    created = _parse_time(job.created_at)
    if created is None:
        return 0.0  # unparseable creation stamp — treat as new, never park
    return (now - created).total_seconds() / 3600


def _deadline_hours() -> float:
    return float(
        os.environ.get(BOOTSTRAP_DEADLINE_HOURS_ENV)
        or DEFAULT_BOOTSTRAP_DEADLINE_HOURS
    )


def _decay_park_days() -> float:
    return float(
        os.environ.get(MONITOR_DECAY_PARK_DAYS_ENV)
        or DEFAULT_MONITOR_DECAY_PARK_DAYS
    )


def _parse_time(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed
