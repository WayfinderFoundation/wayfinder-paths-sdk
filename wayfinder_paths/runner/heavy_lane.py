"""Dispatcher half of the machine-wide heavy-compute lane.

`wayfinder_paths.jobs.heavy_lane` is the queue: producers drop JSON entries
under `.wayfinder/heavy_lane/`. This is what the runner daemon runs every
tick to admit at most one of them when the box can afford it, reap it when
it ends, and hand the outcome to the completion hook. The lane child is
tracked here and never in the daemon's `_running`, so it never counts
against `max_workers` and a daemon restart adopts it instead of killing it.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from wayfinder_paths.jobs.compute_lock import (
    LOCK_RELATIVE,
    ComputeLockBusy,
    job_state_lock,
)
from wayfinder_paths.jobs.execution.op_process import (
    op_runner_command,
    process_identity_fields,
    recorded_process_alive,
)
from wayfinder_paths.jobs.failures import (
    HEAVY_STEAL_THRESHOLD_PCT,
    cpu_steal_pct,
    mem_available_mb,
)
from wayfinder_paths.jobs.heavy_lane import (
    CANCELLED,
    ENTRY_PATH_ENV,
    QUEUED,
    RUNNING,
    STATUS_PATH_ENV,
    delete_entry,
    entries,
    lane_snapshot,
    worker_budget,
    write_entry,
)
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.constants import JobStatus
from wayfinder_paths.runner.monitor_state import atomic_write_json

ADMISSION_INTERVAL_S = 5.0
ADMISSION_REWRITE_INTERVAL_S = 30.0
# A live tick due inside this window would land on top of a freshly started
# heavy op's cold import; the op can wait one more admission round instead.
LIVE_TICK_DUE_WINDOW_S = 30.0
LIVE_TIER = "live-exempt"
MIN_AVAILABLE_MB_ENV = "WAYFINDER_EVOLUTION_MIN_AVAILABLE_MB"
DEFAULT_MIN_AVAILABLE_MB = 1100.0
JOURNAL_DEFERRED = "heavy_op_deferred"
JOURNAL_PAUSED = "heavy_op_paused"
JOURNAL_RESUMED = "heavy_op_resumed"
# Credit floors apply only to the local estimator: on the primary box the
# background load roughly matches the baseline refill, so the bucket sits
# flat around ~340 CPU-s and a 35% start threshold would almost never admit.
START_FLOOR_ENV = "WAYFINDER_HEAVY_START_FLOOR_CPU_S"
PAUSE_FLOOR_ENV = "WAYFINDER_HEAVY_PAUSE_FLOOR_CPU_S"
DEFAULT_START_FLOOR_CPU_S = 300.0
DEFAULT_PAUSE_FLOOR_CPU_S = 150.0
LOCAL_ESTIMATOR = "local_estimator"
CREDIT_DISABLED = "disabled"
# Same shape as the image governor's state file, so isolated_phase and the
# watchdog read both alike (fresh + paused + pid listed == paused).
PAUSE_PATH_ENV = "WAYFINDER_HEAVY_LANE_PAUSE_PATH"
DEFAULT_PAUSE_PATH = Path("/tmp/wayfinder-heavy-lane-pause.json")
SIGTERM_MASK = 1 << (signal.SIGTERM - 1)
COMPLETION_HOOK_MODULE = "wayfinder_paths.jobs.heavy_lane"
# States the child (or a cancel) may already have written into the status
# file; the reaper never overwrites one of these with its own exit mapping.
TERMINAL_STATES = frozenset(
    {"done", "failed", "cancelled", "killed", "timeout", "lost"}
)
# Status-file keys carried over from the queued shape when the op starts.
QUEUED_STATUS_KEYS = (
    "queued_at",
    "queue_entry",
    "lane_class",
    "island",
    "notify",
    "submitted_by",
)

TierClassifier = Callable[[Mapping[str, Any]], tuple[str, float | None]]
BurstSnapshot = Callable[[], Mapping[str, Any]]


@dataclass
class LaneChild:
    entry: dict[str, Any]
    pid: int
    started_wall: float
    # Popen for a child this daemon spawned; None for one adopted from a
    # previous daemon, which only has pid liveness and no exit code.
    popen: subprocess.Popen[bytes] | None
    paused_since: float | None = None
    paused_total_s: float = 0.0
    # Set once a cancel's SIGTERM was found pending on the stopped group: the
    # child must run its cancel path, so the lane never stops it again.
    cancelling: bool = False

    def paused_s(self, now: float) -> float:
        current = now - self.paused_since if self.paused_since is not None else 0.0
        return self.paused_total_s + max(0.0, current)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _iso_timestamp(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _identity_record(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: entry.get(key)
        for key in ("pid", "boot_id", "process_start_ticks", "started_at")
    }


def _kill_process_group(pid: int, *, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return


def compute_lock_held(repo_root: Path) -> bool:
    """True when another process holds `.wayfinder/compute.lock` — an inline
    backtest is running and a lane op would double the box's peak memory."""
    path = repo_root / LOCK_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    # "a+" never truncates: the holder's pid/label line stays readable.
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


def min_available_mb() -> float:
    return float(os.environ.get(MIN_AVAILABLE_MB_ENV, DEFAULT_MIN_AVAILABLE_MB))


def start_floor_cpu_s() -> float:
    return float(os.environ.get(START_FLOOR_ENV, DEFAULT_START_FLOOR_CPU_S))


def pause_floor_cpu_s() -> float:
    return float(os.environ.get(PAUSE_FLOOR_ENV, DEFAULT_PAUSE_FLOOR_CPU_S))


def lane_pause_path() -> Path:
    return Path(os.environ.get(PAUSE_PATH_ENV, str(DEFAULT_PAUSE_PATH)))


def process_group_pids(pgid: int) -> list[int]:
    """Every pid in the group (pool workers, forked isolated-phase children);
    just the leader off Linux, where there is no /proc."""
    pids = {pgid}
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            text = stat_path.read_text(encoding="utf-8")
        except OSError:
            continue
        # comm may hold spaces or parens: fields resume after the last ')'.
        fields = text[text.rfind(")") + 2 :].split()
        if int(fields[2]) == pgid:
            pids.add(int(stat_path.parent.name))
    return sorted(pids)


def sigterm_pending(pid: int) -> bool:
    """True when a SIGTERM (op cancel) waits on a stopped group leader; it
    cannot be handled until the group is continued."""
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        if line.startswith("ShdPnd:"):
            return bool(int(line.split()[1], 16) & SIGTERM_MASK)
    return False


class HeavyLane:
    def __init__(
        self,
        repo_root: Path,
        *,
        burst_snapshot: BurstSnapshot,
        running_job_ids: Callable[[], Iterable[int]],
        list_jobs: Callable[[], list[dict[str, Any]]],
        tier_of: TierClassifier,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._burst_snapshot = burst_snapshot
        self._running_job_ids = running_job_ids
        self._list_jobs = list_jobs
        self._tier_of = tier_of
        self._child: LaneChild | None = None
        self._last_admission_at = float("-inf")
        self._admission_written_at = float("-inf")
        self._last_admission: dict[str, Any] = {}
        # job name -> (updated_at, tier): the tier is a function of the
        # payload env, which only changes when the job row is updated.
        self._tier_cache: dict[str, tuple[Any, str]] = {}

    # ----------------------------------------------------------------- start

    def adopt(self) -> None:
        """Pick up the op a previous daemon left running, or settle its
        status if it is gone. Called once at daemon start."""
        for entry in entries(self._repo_root):
            if entry.get("state") != RUNNING:
                continue
            if recorded_process_alive(_identity_record(entry)):
                if self._child is not None:
                    logger.warning(
                        f"heavy lane: second running entry {entry['entry_id']} "
                        f"(pid {entry.get('pid')}) left untracked"
                    )
                    continue
                # A previous daemon may have stopped it; nobody else will
                # continue it. The next tick re-applies the credit policy.
                _kill_process_group(int(entry["pid"]), sig=signal.SIGCONT)
                started_wall = _iso_timestamp(entry.get("started_at")) or time.time()
                self._child = LaneChild(
                    entry=entry,
                    pid=int(entry["pid"]),
                    started_wall=started_wall,
                    popen=None,
                )
                logger.info(
                    f"heavy lane adopted {entry['op']} for {entry['job_id']} "
                    f"(pid {entry['pid']}, class {entry.get('class')})"
                )
                continue
            recorded_boot = entry.get("boot_id")
            current_boot = process_identity_fields(os.getpid()).get("boot_id")
            if recorded_boot and current_boot != recorded_boot:
                self._finalize(
                    entry,
                    verb="lost",
                    state="lost",
                    exit_code=None,
                    reason="boot_id mismatch",
                )
            else:
                self._finalize(
                    entry, verb="reconciled", state=None, exit_code=None, reason=None
                )

    def stop(self) -> None:
        """A daemon stop leaves the lane op running: the next daemon adopts it."""
        if self._child is not None:
            logger.info(
                f"heavy lane leaving {self._child.entry['op']} "
                f"(pid {self._child.pid}) running for adoption"
            )

    # ------------------------------------------------------------------ tick

    def tick(self, now: float) -> None:
        if self._child is not None:
            self._reap(now)
        if self._child is not None:
            self._govern(self._child, now)
            return
        if now - self._last_admission_at < ADMISSION_INTERVAL_S:
            return
        self._last_admission_at = now
        queued: list[dict[str, Any]] = []
        for entry in entries(self._repo_root):
            if entry.get("state") == CANCELLED:
                delete_entry(entry)
            elif entry.get("state") == QUEUED:
                queued.append(entry)
        if not queued:
            return
        head = queued[0]
        reason = self.admission_reason(now)
        self._last_admission = {"last_reason": reason, "checked_at": utc_now_iso()}
        if reason is not None:
            self._refuse(head, reason, now)
            return
        self._dispatch(head, now)

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        snapshot = lane_snapshot(self._repo_root)
        running = snapshot.get("running")
        if self._child is not None and running is not None:
            running["running_s"] = round(now - self._child.started_wall, 1)
        snapshot["admission"] = dict(self._last_admission)
        child = self._child
        snapshot["paused"] = child is not None and child.paused_since is not None
        snapshot["paused_s"] = round(child.paused_s(now), 1) if child else 0.0
        credit = self._burst_snapshot()
        snapshot["credit"] = {
            "source": credit.get("source"),
            "balance_cpu_seconds": credit.get("balance_cpu_seconds"),
            "start_floor": start_floor_cpu_s(),
            "pause_floor": pause_floor_cpu_s(),
        }
        return snapshot

    # ------------------------------------------------------------- admission

    def admission_reason(self, now: float) -> str | None:
        """Why the head entry may not start right now; None when it may.
        Cheap checks first; the steal sample sleeps, so it runs last."""
        credit_refusal = self._credit_refusal()
        if credit_refusal is not None:
            return credit_refusal
        jobs = self._list_jobs()
        if self._live_tick_running(jobs):
            return "live_tick_running"
        for job in jobs:
            next_run_at = job.get("next_run_at")
            if (
                job.get("status") == JobStatus.ACTIVE
                and next_run_at is not None
                and 0 <= float(next_run_at) - now <= LIVE_TICK_DUE_WINDOW_S
                and self._tier(job) == LIVE_TIER
            ):
                return "live_tick_due"
        if compute_lock_held(self._repo_root):
            return "compute_lock_busy"
        available = mem_available_mb()
        if available is not None and available < min_available_mb():
            return "low_memory"
        steal = cpu_steal_pct()
        if steal is not None and steal > HEAVY_STEAL_THRESHOLD_PCT:
            return "high_steal"
        return None

    def _credit_refusal(self) -> str | None:
        credit = self._burst_snapshot()
        source = credit.get("source")
        if source == CREDIT_DISABLED:
            return None
        if source == LOCAL_ESTIMATOR:
            if float(credit["balance_cpu_seconds"]) < start_floor_cpu_s():
                return "low_credit"
            return None
        if credit.get("paused") or credit.get("allow_new_heavy") is not True:
            return "over_quota"
        return None

    def _live_tick_running(self, jobs: list[dict[str, Any]]) -> bool:
        by_id = {job["id"]: job for job in jobs}
        for job_id in self._running_job_ids():
            job = by_id.get(job_id)
            if job is not None and self._tier(job) == LIVE_TIER:
                return True
        return False

    def _tier(self, job: Mapping[str, Any]) -> str:
        cached = self._tier_cache.get(job["name"])
        if cached is not None and cached[0] == job.get("updated_at"):
            return cached[1]
        tier, _floor = self._tier_of(job)
        self._tier_cache[job["name"]] = (job.get("updated_at"), tier)
        return tier

    def _refuse(self, head: dict[str, Any], reason: str, now: float) -> None:
        previous = head.get("admission") or {}
        first_refusal = not previous
        if (
            first_refusal
            or previous.get("last_reason") != reason
            or now - self._admission_written_at >= ADMISSION_REWRITE_INTERVAL_S
        ):
            self._write_admission(head, dict(self._last_admission))
            self._admission_written_at = now
        if first_refusal:
            logger.info(
                f"heavy lane deferring {head['op']} for {head['job_id']}: {reason}"
            )
            JobStore(repo_root=self._repo_root).append_journal(
                str(head["job_id"]),
                {"type": JOURNAL_DEFERRED, "op": head["op"], "reason": reason},
            )

    def _write_admission(self, head: dict[str, Any], record: dict[str, Any]) -> None:
        # Under the op's state lock and re-read from disk so the write can
        # never clobber a concurrent cancel's `state: cancelled`.
        try:
            with job_state_lock(
                self._repo_root,
                str(head["job_id"]),
                name=f"background_{head['op']}",
                timeout_s=0.0,
            ):
                current = _read_json(Path(head["entry_path"]))
                if current is None or current.get("state") != QUEUED:
                    return
                current["entry_path"] = head["entry_path"]
                current["admission"] = record
                write_entry(current)
        except ComputeLockBusy:
            return

    # ---------------------------------------------------------------- govern

    def _govern(self, child: LaneChild, now: float) -> None:
        """Stop the running op's whole process group when local credit runs
        low (or a live tick needs the CPU), continue it once credit is back.
        A fresh image governor pauses heavy children itself."""
        credit = self._burst_snapshot()
        source = credit.get("source")
        if source != LOCAL_ESTIMATOR:
            if child.paused_since is not None:
                balance = credit.get("balance_cpu_seconds")
                self._resume(child, now, balance, f"credit_source_{source}")
        else:
            balance = float(credit["balance_cpu_seconds"])
            if child.paused_since is None:
                reason = None if child.cancelling else self._pause_reason(balance)
                if reason is not None:
                    self._pause(child, now, balance, reason)
            elif sigterm_pending(child.pid):
                child.cancelling = True
                self._resume(child, now, balance, "cancel_pending")
            elif balance >= start_floor_cpu_s():
                self._resume(child, now, balance, "credit_recovered")
        atomic_write_json(
            lane_pause_path(),
            {
                "paused": child.paused_since is not None,
                "affected_pids": process_group_pids(child.pid),
                "updated_at": time.time(),
            },
        )

    def _pause_reason(self, balance: float) -> str | None:
        if balance < pause_floor_cpu_s():
            return "low_credit"
        if balance < start_floor_cpu_s() and self._live_tick_running(self._list_jobs()):
            return "live_tick_running"
        return None

    def _pause(self, child: LaneChild, now: float, balance: Any, reason: str) -> None:
        _kill_process_group(child.pid, sig=signal.SIGSTOP)
        child.paused_since = now
        logger.warning(
            f"heavy lane paused {child.entry['op']} for {child.entry['job_id']} "
            f"(pgid {child.pid}): {reason}, balance {balance} CPU-s"
        )
        self._journal_pause_change(child, JOURNAL_PAUSED, balance, reason)

    def _resume(self, child: LaneChild, now: float, balance: Any, reason: str) -> None:
        _kill_process_group(child.pid, sig=signal.SIGCONT)
        assert child.paused_since is not None
        paused_for = max(0.0, now - child.paused_since)
        child.paused_total_s += paused_for
        child.paused_since = None
        logger.info(
            f"heavy lane resumed {child.entry['op']} for {child.entry['job_id']} "
            f"(pgid {child.pid}) after {paused_for:.0f}s: {reason}, "
            f"balance {balance} CPU-s"
        )
        self._journal_pause_change(child, JOURNAL_RESUMED, balance, reason)

    def _journal_pause_change(
        self, child: LaneChild, event: str, balance: Any, reason: str
    ) -> None:
        JobStore(repo_root=self._repo_root).append_journal(
            str(child.entry["job_id"]),
            {
                "type": event,
                "op": child.entry["op"],
                "reason": reason,
                "balance_cpu_seconds": balance,
            },
        )

    # -------------------------------------------------------------- dispatch

    def _dispatch(self, head: dict[str, Any], now: float) -> None:
        job_id, op = str(head["job_id"]), str(head["op"])
        try:
            with job_state_lock(
                self._repo_root, job_id, name=f"background_{op}", timeout_s=0.0
            ):
                current = _read_json(Path(head["entry_path"]))
                if current is None:
                    return
                current["entry_path"] = head["entry_path"]
                if current.get("state") == CANCELLED:
                    delete_entry(current)
                    return
                if current.get("state") != QUEUED:
                    return
                self._child = self._spawn(current, now)
        except ComputeLockBusy:
            logger.debug(f"heavy lane: {op} for {job_id} state lock busy; retrying")

    def _spawn(self, entry: dict[str, Any], now: float) -> LaneChild:
        op, job_id = str(entry["op"]), str(entry["job_id"])
        status_path = Path(entry["status_path"])
        ops_dir = status_path.parent
        ops_dir.mkdir(parents=True, exist_ok=True)
        log_path = ops_dir / f"{op}.log"
        result_path = ops_dir / f"{op}.result.json"
        budget = worker_budget()
        env = {
            **os.environ,
            "WAYFINDER_MAX_BACKTEST_WORKERS": str(budget),
            STATUS_PATH_ENV: str(status_path),
            ENTRY_PATH_ENV: str(entry["entry_path"]),
        }
        with log_path.open("wb") as log_handle, result_path.open("wb") as result_handle:
            proc = subprocess.Popen(  # noqa: S603
                op_runner_command(op),
                stdin=subprocess.PIPE,
                stdout=result_handle,
                stderr=log_handle,
                start_new_session=True,
                cwd=str(self._repo_root),
                env=env,
            )
        assert proc.stdin is not None
        proc.stdin.write(
            json.dumps({"op": op, "kwargs": entry.get("kwargs") or {}}).encode()
        )
        proc.stdin.close()

        identity = process_identity_fields(proc.pid)
        started_at = utc_now_iso()
        queued_status = _read_json(status_path) or {}
        status = {
            **{k: queued_status[k] for k in QUEUED_STATUS_KEYS if k in queued_status},
            "op": op,
            "job_id": job_id,
            "state": RUNNING,
            "pid": proc.pid,
            "started_at": started_at,
            "worker_budget": budget,
            **identity,
        }
        atomic_write_json(status_path, status)
        entry.update(
            {"state": RUNNING, "started_at": started_at, "pid": proc.pid, **identity}
        )
        write_entry(entry)
        logger.info(
            f"heavy lane started {op} for {job_id} (pid {proc.pid}, "
            f"class {entry.get('class')}, workers {budget})"
        )
        return LaneChild(entry=entry, pid=proc.pid, started_wall=now, popen=proc)

    # ------------------------------------------------------------------ reap

    def _reap(self, now: float) -> None:
        child = self._child
        assert child is not None
        if child.popen is not None:
            exit_code: int | None = child.popen.poll()
            alive = exit_code is None
        else:
            exit_code = None
            alive = recorded_process_alive(_identity_record(child.entry))
        if alive:
            max_runtime = child.entry.get("max_runtime_s")
            active_s = now - child.started_wall - child.paused_s(now)
            if max_runtime is None or active_s <= float(max_runtime):
                return
            logger.warning(
                f"heavy lane killing {child.entry['op']} for {child.entry['job_id']} "
                f"(pid {child.pid}): over {max_runtime}s"
            )
            _kill_process_group(child.pid, sig=signal.SIGKILL)
            if child.popen is not None:
                try:
                    child.popen.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass
                exit_code = child.popen.returncode
            self._child = None
            lane_pause_path().unlink(missing_ok=True)
            self._finalize(
                child.entry,
                verb="timed out",
                state="timeout",
                exit_code=exit_code,
                reason=f"max_runtime_s={max_runtime}",
            )
            return
        self._child = None
        lane_pause_path().unlink(missing_ok=True)
        self._finalize(
            child.entry, verb="reaped", state=None, exit_code=exit_code, reason=None
        )

    def _finalize(
        self,
        entry: dict[str, Any],
        *,
        verb: str,
        state: str | None,
        exit_code: int | None,
        reason: str | None,
    ) -> None:
        op, job_id = str(entry["op"]), str(entry["job_id"])
        status_path = Path(entry["status_path"])
        status = _read_json(status_path) or {"op": op, "job_id": job_id}
        if state is None:
            state = _outcome(
                status, exit_code, status_path.with_name(f"{op}.result.json")
            )
        status.update(
            {"state": state, "finished_at": utc_now_iso(), "exit_code": exit_code}
        )
        if reason is not None:
            status["reason"] = reason
        if state == "failed" and exit_code is None:
            # Same text op_status_summary stamps on a reaper-less loss, so
            # its reconciled-failure re-check treats the two alike.
            status.setdefault("error", "detached operation exited without a result")
        atomic_write_json(status_path, status)
        delete_entry(entry)
        logger.info(f"heavy lane {verb} {op} for {job_id}: {state} (exit {exit_code})")
        self._spawn_completion_hook(entry)

    def _spawn_completion_hook(self, entry: dict[str, Any]) -> None:
        log_path = Path(entry["status_path"]).with_name(f"{entry['op']}.log")
        with log_path.open("ab") as log_handle:
            proc = subprocess.Popen(  # noqa: S603
                [sys.executable, "-m", COMPLETION_HOOK_MODULE, "finished"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=log_handle,
                start_new_session=True,
                cwd=str(self._repo_root),
            )
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(entry).encode())
        proc.stdin.close()


def _result_parseable(result_path: Path) -> bool:
    try:
        json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return True


def _outcome(
    status: Mapping[str, Any], exit_code: int | None, result_path: Path
) -> str:
    if status.get("state") in TERMINAL_STATES:
        return str(status["state"])
    if exit_code is None:
        # No exit code (adopted child): the same rule op_status uses for a
        # reaper-less detached op — a parseable result means it finished.
        return "done" if _result_parseable(result_path) else "failed"
    if exit_code == 0:
        return "done"
    return "failed" if exit_code > 0 else "killed"
