"""One machine-wide lane for heavy compute.

Every producer of heavy work on a box — the chat agent's ad-hoc ops, the
wake worker's research, the watchdog's gate re-stamp, the CLI — used to
spawn its own `op_runner` child whenever it liked. On a shared-CPU box that
drains the burst credits under the live trading tick. Now a heavy op is a
*submission*: a JSON entry under `.wayfinder/heavy_lane/` plus the usual
`state/background_ops/<op>.json` status file in state `queued`. The runner
daemon (`wayfinder_paths.runner.heavy_lane`) dispatches at most one entry
at a time when the box can afford it, and on completion runs the hook at
the bottom of this module, which journals the outcome and prompts the
session that asked.

The status file keeps the exact shape `jobs/background.py` writes, so
`op_status`, readout `pending_ops`, `remove_job` refusals and the island
scheduler see a queued op without learning anything new.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wayfinder_paths.core.clients.OpenCodeClient import OPENCODE_CLIENT
from wayfinder_paths.core.config import is_opencode_instance
from wayfinder_paths.jobs.compute_lock import job_state_lock
from wayfinder_paths.jobs.execution.op_process import recorded_process_alive
from wayfinder_paths.jobs.models import safe_job_id, utc_now_iso
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.monitor_state import atomic_write_json

LANE_RELATIVE = Path(".wayfinder") / "heavy_lane"
SCHEMA_VERSION = 1

# Ops that replay the dataset (grids, folds, robustness) and must never run
# unpaced beside a live tick. Evolution phases and `restamp` join later.
HEAVY_LANE_OPS = frozenset(
    {
        "experiments",
        "backtest_job",
        "robustness_check",
        "signal_scan",
        "holdout_check",
        "rank_check",
        "forward_experience",
    }
)

CLASS_RANKS = {"owner": 0, "correctness": 1, "evolution": 2, "research": 3}
# Seconds a running op of each class may take before the dispatcher kills
# it; None leaves the lifetime to the campaign machinery.
MAX_RUNTIME_S: dict[str, int | None] = {
    "owner": 3600,
    "correctness": 3600,
    "evolution": None,
    "research": 1800,
}

QUEUED = "queued"
RUNNING = "running"
CANCELLED = "cancelled"
JOURNAL_FINISHED = "background_op_finished"
JOURNAL_NOTIFY_DROPPED = "background_op_notify_dropped"

WORKER_BUDGET_ENV = "WAYFINDER_HEAVY_WORKER_BUDGET"
STATUS_PATH_ENV = "WAYFINDER_OP_STATUS_PATH"
ENTRY_PATH_ENV = "WAYFINDER_HEAVY_LANE_ENTRY"
NOTIFY_RETRY_EVERY_S = 30.0
NOTIFY_RETRY_FOR_S = 600.0


def lane_enabled() -> bool:
    """The lane exists only where runnerd runs beside live ticks (a hosted
    box). Dev laptops keep spawning detached ops directly."""
    return is_opencode_instance()


def worker_budget() -> int:
    """Process-pool fan-out for a lane op: leave one vCPU for the live tick."""
    override = os.environ.get(WORKER_BUDGET_ENV)
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    return max(1, (os.cpu_count() or 1) - 1)


def lane_dir(repo_root: Path) -> Path:
    return Path(repo_root) / LANE_RELATIVE


def classify(op: str, submitted_by: str, notify: dict[str, Any] | None) -> str:
    if op.startswith("evolution_"):
        return "evolution"
    if op == "restamp" or submitted_by in {"watchdog", "sync"}:
        return "correctness"
    if submitted_by == "worker" or (notify or {}).get("kind") == "worker":
        return "research"
    return "owner"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _ops_dir(store: JobStore, job_id: str) -> Path:
    return store.job_dir(job_id) / "state" / "background_ops"


def _island_stamp(store: JobStore, job_id: str) -> str | None:
    scheduler_state = _read_json(store.job_dir(job_id) / "state" / "scheduler.json")
    history = (scheduler_state or {}).get("history") or []
    last = history[-1] if history and isinstance(history[-1], dict) else {}
    island = last.get("island")
    return str(island) if island else None


def entries(repo_root: Path) -> list[dict[str, Any]]:
    """Every readable entry, queued first by (rank, submission time)."""
    directory = lane_dir(repo_root)
    if not directory.is_dir():
        return []
    loaded: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        entry = _read_json(path)
        if entry is None or entry.get("schema_version") != SCHEMA_VERSION:
            continue
        entry["entry_path"] = str(path)
        loaded.append(entry)
    loaded.sort(
        key=lambda e: (int(e.get("rank", 99)), int(e.get("submitted_at_ms", 0)))
    )
    return loaded


def queued_entries(repo_root: Path) -> list[dict[str, Any]]:
    return [e for e in entries(repo_root) if e.get("state") == QUEUED]


def running_entry(repo_root: Path) -> dict[str, Any] | None:
    for entry in entries(repo_root):
        if entry.get("state") == RUNNING:
            return entry
    return None


def queue_position(repo_root: Path, entry_id: str) -> int | None:
    """0 = next to start. None when the entry is not queued."""
    for index, entry in enumerate(queued_entries(repo_root)):
        if entry.get("entry_id") == entry_id:
            return index
    return None


def write_entry(entry: dict[str, Any]) -> None:
    payload = {k: v for k, v in entry.items() if k != "entry_path"}
    atomic_write_json(Path(entry["entry_path"]), payload)


def delete_entry(entry: dict[str, Any]) -> None:
    Path(entry["entry_path"]).unlink(missing_ok=True)


def submit_heavy_op(
    repo_root: Path,
    job_id: str,
    op: str,
    kwargs: dict[str, Any],
    *,
    submitted_by: str,
    notify: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Queue `op` for `job_id`. One slot per (job, op): a queued or live
    entry is returned as `already_queued` / `already_running` instead of a
    duplicate, mirroring `background.spawn_detached_op`."""
    repo_root = Path(repo_root)
    store = JobStore(repo_root=repo_root)
    notify = {"session_id": None, "kind": None, "wake": False, **(notify or {})}
    with job_state_lock(repo_root, job_id, name=f"background_{op}"):
        ops_dir = _ops_dir(store, job_id)
        ops_dir.mkdir(parents=True, exist_ok=True)
        status_path = ops_dir / f"{op}.json"
        existing = _read_json(status_path) or {}
        if existing.get("state") == QUEUED:
            position = queue_position(repo_root, str(existing.get("queue_entry") or ""))
            if position is not None:
                return {"already_queued": True, "position": position, **existing}
        if existing.get("state") == RUNNING and recorded_process_alive(existing):
            return {"already_running": True, **existing}

        now = datetime.now(UTC)
        submitted_at_ms = int(now.timestamp() * 1000)
        lane_class = classify(op, submitted_by, notify)
        entry_id = f"{submitted_at_ms:013d}-{os.getpid()}-{safe_job_id(job_id)}-{op}"
        entry = {
            "schema_version": SCHEMA_VERSION,
            "entry_id": entry_id,
            "op": op,
            "job_id": job_id,
            "kwargs": kwargs,
            "class": lane_class,
            "rank": CLASS_RANKS[lane_class],
            "submitted_at": now.isoformat(),
            "submitted_at_ms": submitted_at_ms,
            "submitted_by": submitted_by,
            "submitter_pid": os.getpid(),
            "notify": notify,
            "island": _island_stamp(store, job_id),
            "repo_root": str(repo_root),
            "status_path": str(status_path),
            "state": QUEUED,
            "started_at": None,
            "pid": None,
            "max_runtime_s": MAX_RUNTIME_S[lane_class],
            "admission": {},
            "entry_path": str(lane_dir(repo_root) / f"{entry_id}.json"),
        }
        lane_dir(repo_root).mkdir(parents=True, exist_ok=True)
        write_entry(entry)
        # A stale result from an earlier run must not read as this one's.
        (ops_dir / f"{op}.result.json").unlink(missing_ok=True)
        status = {
            "op": op,
            "job_id": job_id,
            "state": QUEUED,
            "queued_at": utc_now_iso(),
            "queue_entry": entry_id,
            "lane_class": lane_class,
            "submitted_by": submitted_by,
            "notify": notify,
            **({"island": entry["island"]} if entry["island"] else {}),
        }
        atomic_write_json(status_path, status)
        return {
            "queued": True,
            "entry": entry_id,
            "class": lane_class,
            "position": queue_position(repo_root, entry_id),
            **status,
        }


def lane_view_for_job(repo_root: Path, job_id: str) -> list[dict[str, Any]]:
    """Queued and running lane ops for one job, with queue positions."""
    view: list[dict[str, Any]] = []
    queued = queued_entries(Path(repo_root))
    for entry in entries(Path(repo_root)):
        if entry.get("job_id") != job_id or entry.get("state") not in {QUEUED, RUNNING}:
            continue
        position = next(
            (
                i
                for i, q in enumerate(queued)
                if q.get("entry_id") == entry.get("entry_id")
            ),
            None,
        )
        view.append(
            {
                "op": entry.get("op"),
                "state": entry.get("state"),
                "class": entry.get("class"),
                "position": position,
                "submitted_by": entry.get("submitted_by"),
                "submitted_at": entry.get("submitted_at"),
                "started_at": entry.get("started_at"),
                "admission": entry.get("admission") or {},
            }
        )
    return view


def lane_snapshot(repo_root: Path) -> dict[str, Any]:
    running = running_entry(Path(repo_root))
    queued = queued_entries(Path(repo_root))
    return {
        "running": (
            {k: running.get(k) for k in ("job_id", "op", "class", "pid", "started_at")}
            if running
            else None
        ),
        "queued": [
            {k: e.get(k) for k in ("job_id", "op", "class", "submitted_at")}
            for e in queued
        ],
    }


def cancel_heavy_op(repo_root: Path, job_id: str, op: str) -> dict[str, Any]:
    """Withdraw a queued op or SIGTERM a running one (the child writes its
    own `cancelled` status; the dispatcher reaps it)."""
    repo_root = Path(repo_root)
    store = JobStore(repo_root=repo_root)
    with job_state_lock(repo_root, job_id, name=f"background_{op}"):
        status_path = _ops_dir(store, job_id) / f"{op}.json"
        status = _read_json(status_path)
        if status is None:
            return {"cancelled": False, "error": "not_found"}
        state = status.get("state")
        if state == QUEUED:
            for entry in entries(repo_root):
                if entry.get("entry_id") == status.get("queue_entry"):
                    entry["state"] = CANCELLED
                    entry["cancelled_at"] = utc_now_iso()
                    write_entry(entry)
            status.update(
                {
                    "state": CANCELLED,
                    "finished_at": utc_now_iso(),
                    "reason": "op_cancel",
                }
            )
            atomic_write_json(status_path, status)
            return {"cancelled": True, "was": QUEUED, "op": op, "job_id": job_id}
        if state == RUNNING and recorded_process_alive(status):
            pid = int(status["pid"])
            try:
                # The child is a session leader (start_new_session), so its
                # pid is its process group: the pool workers die with it.
                os.killpg(pid, signal.SIGTERM)
                # A group the lane paused for low credit cannot act on the
                # SIGTERM until it runs again.
                os.killpg(pid, signal.SIGCONT)
            except ProcessLookupError:
                return {"cancelled": False, "error": "already_finished"}
            return {
                "cancelled": True,
                "was": RUNNING,
                "pid": pid,
                "op": op,
                "job_id": job_id,
            }
        return {"cancelled": False, "error": "already_finished", "state": state}


def mark_harvested(job_dir: Path, ops: list[str], wake_id: str) -> list[str]:
    """Stamp finished ops a wake has read so the island scheduler's
    continuation check releases the rotation."""
    stamped: list[str] = []
    for op in ops:
        status_path = Path(job_dir) / "state" / "background_ops" / f"{op}.json"
        status = _read_json(status_path)
        if not status or status.get("state") != "done" or status.get("harvested"):
            continue
        status.update(
            {"harvested": True, "harvested_by": wake_id, "harvested_at": utc_now_iso()}
        )
        atomic_write_json(status_path, status)
        stamped.append(op)
    return stamped


def result_summary(payload: Any) -> dict[str, Any]:
    """A few hundred bytes an agent can act on, never the 8 MB raw result."""
    if not isinstance(payload, dict):
        return {"available": False}
    out: dict[str, Any] = {}
    if payload.get("error"):
        out["error"] = str(payload["error"])[:200]
    experiment = payload.get("experiment")
    if isinstance(experiment, dict):
        summary = {
            k: experiment.get(k)
            for k in ("grid_id", "run_count", "invalid_count", "rank_by")
            if k in experiment
        }
        best = experiment.get("best")
        if isinstance(best, dict):
            summary["best_params"] = best.get("params")
            summary["best_stats"] = _stats_subset(best.get("stats"))
        walk_forward = experiment.get("walk_forward")
        if isinstance(walk_forward, dict):
            summary["walk_forward"] = {
                k: walk_forward.get(k)
                for k in ("decay_ratio", "oos_positive_folds", "folds", "status")
                if k in walk_forward
            }
        out["experiment"] = summary
    for key in ("backtest", "result"):
        block = payload.get(key)
        if isinstance(block, dict):
            stats = block.get("stats") or (block.get("result") or {}).get("stats")
            if isinstance(stats, dict):
                out[key] = {"stats": _stats_subset(stats)}
    if not out:
        out["keys"] = sorted(str(k) for k in payload)[:12]
    return out


def _stats_subset(stats: Any) -> dict[str, Any]:
    if not isinstance(stats, dict):
        return {}
    return {
        k: stats.get(k)
        for k in ("net_return", "sharpe", "max_drawdown", "trade_count", "win_rate")
        if k in stats
    }


def _seconds_between(start: Any, end: Any) -> float | None:
    try:
        a = datetime.fromisoformat(str(start))
        b = datetime.fromisoformat(str(end))
    except (TypeError, ValueError):
        return None
    return round((b - a).total_seconds(), 1)


def completion_text(entry: dict[str, Any], status: dict[str, Any]) -> str:
    duration = _seconds_between(status.get("started_at"), status.get("finished_at"))
    queued = _seconds_between(entry.get("submitted_at"), status.get("started_at"))
    timing = ""
    if duration is not None:
        timing = f" in {duration / 60:.0f} min"
        if queued:
            timing += f" after {queued / 60:.0f} min queued"
    return (
        f"Background op `{entry['op']}` for job `{entry['job_id']}` finished "
        f"({status.get('state')}{timing}). Read the result with "
        f"core_jobs(action='op_status', job_id='{entry['job_id']}', op='{entry['op']}') "
        "and continue from where you left off."
    )


def _prompt_session(
    client: Any,
    session_id: str,
    text: str,
    *,
    sleep: Any = time.sleep,
    retry_every_s: float = NOTIFY_RETRY_EVERY_S,
    retry_for_s: float = NOTIFY_RETRY_FOR_S,
) -> dict[str, Any]:
    deadline = time.monotonic() + retry_for_s
    while True:
        if client.session_exists(session_id) is False:
            return {"notified": False, "error": "session_gone"}
        if session_id not in (client.session_statuses() or {}):
            if client.prompt_async(session_id, text):
                return {"notified": True}
            return {"notified": False, "error": "prompt_rejected"}
        if time.monotonic() >= deadline:
            return {"notified": False, "error": "session_busy"}
        sleep(retry_every_s)


def run_completion_hook(
    entry: dict[str, Any],
    *,
    client: Any = OPENCODE_CLIENT,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Journal a finished lane op and prompt the session that submitted it.
    Runs in its own short-lived process so the runner daemon never imports
    the jobs machinery or waits on the agent host."""
    store = JobStore(repo_root=Path(entry["repo_root"]))
    job_id, op = str(entry["job_id"]), str(entry["op"])
    status_path = Path(entry["status_path"])
    status = _read_json(status_path) or {}
    result = _read_json(status_path.with_name(f"{op}.result.json"))
    summary = result_summary(result)
    event = {
        "type": JOURNAL_FINISHED,
        "op": op,
        "state": status.get("state"),
        "class": entry.get("class"),
        "submitted_by": entry.get("submitted_by"),
        "duration_s": _seconds_between(
            status.get("started_at"), status.get("finished_at")
        ),
        "queued_s": _seconds_between(
            entry.get("submitted_at"), status.get("started_at")
        ),
        "summary": summary,
    }
    store.append_journal(job_id, event)
    outcome: dict[str, Any] = {"journaled": True, "notified": False}
    notify = entry.get("notify") or {}
    session_id = notify.get("session_id")
    if notify.get("kind") == "user" and session_id:
        outcome.update(
            _prompt_session(
                client, str(session_id), completion_text(entry, status), sleep=sleep
            )
        )
        if outcome.get("notified"):
            status["notified_at"] = utc_now_iso()
        else:
            status["notify_error"] = outcome.get("error")
            store.append_journal(
                job_id,
                {
                    "type": JOURNAL_NOTIFY_DROPPED,
                    "op": op,
                    "session_id": session_id,
                    "error": outcome.get("error"),
                },
            )
        atomic_write_json(status_path, status)
    return outcome


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] != ["finished"]:
        print(
            "usage: python -m wayfinder_paths.jobs.heavy_lane finished < entry.json",
            file=sys.stderr,
        )
        return 2
    entry = json.load(sys.stdin)
    outcome = run_completion_hook(entry)
    print(json.dumps(outcome))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
