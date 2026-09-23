from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs.execution.op_process import op_runner_command
from wayfinder_paths.jobs.heavy_lane import (
    CANCELLED,
    ENTRY_PATH_ENV,
    QUEUED,
    RUNNING,
    STATUS_PATH_ENV,
    cancel_heavy_op,
    entries,
    submit_heavy_op,
    worker_budget,
    write_entry,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT
from wayfinder_paths.runner.daemon import RunnerDaemon, _burst_postpone_tier
from wayfinder_paths.runner.heavy_lane import (
    ADMISSION_INTERVAL_S,
    ADMISSION_REWRITE_INTERVAL_S,
    JOURNAL_DEFERRED,
    JOURNAL_PAUSED,
    JOURNAL_RESUMED,
    PAUSE_PATH_ENV,
    HeavyLane,
    lane_pause_path,
)
from wayfinder_paths.runner.paths import RunnerPaths

LIVE_ENV = {"WAYFINDER_JOB_EXECUTION_CONTRACT": "jobs_v1", "WAYFINDER_JOB_MODE": "live"}
PAPER_ENV = {**LIVE_ENV, "WAYFINDER_JOB_MODE": "paper"}
# Above every Linux pid_max and macOS PID_MAX: os.kill(pid, 0) is always ESRCH.
DEAD_PID = 2**22 + 12345
T0 = 1_000.0
STEP = ADMISSION_INTERVAL_S


class _FakeBurst:
    def __init__(self, over: bool) -> None:
        self._over = over
        self.balance = 0.0

    def update(self) -> None:  # pragma: no cover - trivial
        pass

    def over_quota(self) -> bool:
        return self._over

    def snapshot(self) -> dict[str, Any]:
        return {"source": "fake", "allow_new_heavy": not self._over}


class _Stdin:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> int:
        self.data += data
        return len(data)

    def close(self) -> None:
        self.closed = True


class _FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stdin = _Stdin()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode

    def exit(self, code: int) -> None:
        self.returncode = code

    def payload(self) -> Any:
        return json.loads(self.stdin.data)


class _PopenSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any], _FakeProc]] = []
        self._next_pid = 40_000

    def __call__(self, args: list[str], **kwargs: Any) -> _FakeProc:
        proc = _FakeProc(self._next_pid)
        self._next_pid += 1
        self.calls.append((list(args), kwargs, proc))
        return proc

    def ops(self) -> list[tuple[list[str], dict[str, Any], _FakeProc]]:
        return [
            c for c in self.calls if "wayfinder_paths.jobs.execution.op_runner" in c[0]
        ]

    def hooks(self) -> list[tuple[list[str], dict[str, Any], _FakeProc]]:
        return [c for c in self.calls if "wayfinder_paths.jobs.heavy_lane" in c[0]]


class _Box:
    def __init__(self) -> None:
        self.over = False
        # None: a fresh image governor whose verdict is `over`.
        self.credit: dict[str, Any] | None = None
        self.running: set[int] = set()
        self.jobs: list[dict[str, Any]] = []

    def burst_snapshot(self) -> dict[str, Any]:
        if self.credit is not None:
            return self.credit
        return {"source": "governor", "allow_new_heavy": not self.over}

    def local_credit(self, balance: float) -> None:
        self.credit = {"source": "local_estimator", "balance_cpu_seconds": balance}


@pytest.fixture(autouse=True)
def _quiet_box(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(PAUSE_PATH_ENV, str(tmp_path / "lane-pause.json"))
    # A Linux CI box reports real memory/steal; pin the probes so only the
    # test under study drives them.
    monkeypatch.setattr(
        "wayfinder_paths.runner.heavy_lane.mem_available_mb", lambda: None
    )
    monkeypatch.setattr("wayfinder_paths.runner.heavy_lane.cpu_steal_pct", lambda: None)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".wayfinder" / "jobs").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def popen(monkeypatch: pytest.MonkeyPatch) -> _PopenSpy:
    spy = _PopenSpy()
    monkeypatch.setattr("subprocess.Popen", spy)
    return spy


def _lane(root: Path) -> tuple[HeavyLane, _Box]:
    box = _Box()
    lane = HeavyLane(
        root,
        burst_snapshot=box.burst_snapshot,
        running_job_ids=lambda: box.running,
        list_jobs=lambda: box.jobs,
        tier_of=_burst_postpone_tier,
    )
    return lane, box


def _submit(
    root: Path,
    job_id: str = "job-a",
    op: str = "experiments",
    *,
    submitted_by: str = "user",
    kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    (root / ".wayfinder" / "jobs" / job_id).mkdir(parents=True, exist_ok=True)
    return submit_heavy_op(
        root,
        job_id,
        op,
        kwargs or {"job_id": job_id},
        submitted_by=submitted_by,
        notify={"kind": "user", "session_id": "ses-1"},
    )


def _status_path(root: Path, job_id: str = "job-a", op: str = "experiments") -> Path:
    return (
        JobStore(repo_root=root).job_dir(job_id)
        / "state"
        / "background_ops"
        / f"{op}.json"
    )


def _status(
    root: Path, job_id: str = "job-a", op: str = "experiments"
) -> dict[str, Any]:
    return json.loads(_status_path(root, job_id, op).read_text(encoding="utf-8"))


def _journal(root: Path, job_id: str = "job-a") -> list[dict[str, Any]]:
    path = JobStore(repo_root=root).job_dir(job_id) / "journal.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _only_entry(root: Path) -> dict[str, Any]:
    found = entries(root)
    assert len(found) == 1
    return found[0]


def _job_row(
    job_id: int,
    env: dict[str, str],
    *,
    next_run_at: float,
    status: str = "ACTIVE",
) -> dict[str, Any]:
    return {
        "id": job_id,
        "name": f"job-{job_id}",
        "type": JOB_TYPE_SCRIPT,
        "payload": {"env": env},
        "status": status,
        "next_run_at": next_run_at,
        "updated_at": 1,
    }


# --------------------------------------------------------------- admission


def test_refused_over_quota_then_admitted(root: Path, popen: _PopenSpy) -> None:
    lane, box = _lane(root)
    _submit(root)
    box.over = True
    lane.tick(T0)
    assert popen.ops() == []
    entry = _only_entry(root)
    assert entry["state"] == QUEUED
    assert entry["admission"]["last_reason"] == "over_quota"
    assert lane.snapshot()["admission"]["last_reason"] == "over_quota"
    box.over = False
    lane.tick(T0 + STEP)
    assert len(popen.ops()) == 1
    assert _status(root)["state"] == RUNNING


def test_refused_while_live_tick_running(root: Path, popen: _PopenSpy) -> None:
    lane, box = _lane(root)
    _submit(root)
    box.jobs = [_job_row(7, LIVE_ENV, next_run_at=T0 + 3600)]
    box.running = {7}
    assert lane.admission_reason(T0) == "live_tick_running"
    box.running = set()
    assert lane.admission_reason(T0) is None
    # A running PAPER tick does not hold the lane.
    box.jobs = [_job_row(8, PAPER_ENV, next_run_at=T0 + 3600)]
    box.running = {8}
    assert lane.admission_reason(T0) is None


def test_refused_when_live_tick_due_within_window(root: Path) -> None:
    lane, box = _lane(root)
    box.jobs = [_job_row(7, LIVE_ENV, next_run_at=T0 + 30)]
    assert lane.admission_reason(T0) == "live_tick_due"
    box.jobs = [_job_row(7, LIVE_ENV, next_run_at=T0 + 31)]
    assert lane.admission_reason(T0) is None
    # Overdue (negative delta) live ticks are the daemon's problem, not ours.
    box.jobs = [_job_row(7, LIVE_ENV, next_run_at=T0 - 1)]
    assert lane.admission_reason(T0) is None
    box.jobs = [_job_row(7, LIVE_ENV, next_run_at=T0 + 10, status="PAUSED")]
    assert lane.admission_reason(T0) is None
    box.jobs = [_job_row(8, PAPER_ENV, next_run_at=T0 + 10)]
    assert lane.admission_reason(T0) is None
    # The tier is cached per job name until the row's updated_at moves
    # (update_job bumps it): a paper→live flip is seen once it is re-stamped.
    box.jobs = [{**_job_row(8, LIVE_ENV, next_run_at=T0 + 10), "updated_at": 1}]
    assert lane.admission_reason(T0) is None
    box.jobs = [{**_job_row(8, LIVE_ENV, next_run_at=T0 + 10), "updated_at": 2}]
    assert lane.admission_reason(T0) == "live_tick_due"


def test_refused_on_low_memory_and_high_steal(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane, _box = _lane(root)
    monkeypatch.setattr(
        "wayfinder_paths.runner.heavy_lane.mem_available_mb", lambda: 500.0
    )
    assert lane.admission_reason(T0) == "low_memory"
    monkeypatch.setenv("WAYFINDER_EVOLUTION_MIN_AVAILABLE_MB", "400")
    assert lane.admission_reason(T0) is None
    monkeypatch.setattr("wayfinder_paths.runner.heavy_lane.cpu_steal_pct", lambda: 75.0)
    assert lane.admission_reason(T0) == "high_steal"
    monkeypatch.setattr("wayfinder_paths.runner.heavy_lane.cpu_steal_pct", lambda: 10.0)
    assert lane.admission_reason(T0) is None


def test_refused_while_compute_lock_flocked(root: Path, popen: _PopenSpy) -> None:
    lane, _box = _lane(root)
    _submit(root)
    lock_path = root / ".wayfinder" / "compute.lock"
    with lock_path.open("a+") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        lane.tick(T0)
        assert popen.ops() == []
        assert _only_entry(root)["admission"]["last_reason"] == "compute_lock_busy"
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    lane.tick(T0 + STEP)
    assert len(popen.ops()) == 1


def test_first_refusal_journals_deferred_once(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane, box = _lane(root)
    _submit(root)
    box.over = True
    lane.tick(T0)
    lane.tick(T0 + STEP)
    box.over = False
    monkeypatch.setattr(
        "wayfinder_paths.runner.heavy_lane.mem_available_mb", lambda: 1.0
    )
    lane.tick(T0 + 2 * STEP)  # reason changes to low_memory: no second row
    deferred = [row for row in _journal(root) if row["type"] == JOURNAL_DEFERRED]
    assert len(deferred) == 1
    assert deferred[0]["op"] == "experiments"
    assert deferred[0]["reason"] == "over_quota"
    assert _only_entry(root)["admission"]["last_reason"] == "low_memory"


def test_admission_rewrite_throttled_unless_reason_changes(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stamps = iter(f"2026-01-01T00:00:{i:02d}+00:00" for i in range(60))
    monkeypatch.setattr(
        "wayfinder_paths.runner.heavy_lane.utc_now_iso", lambda: next(stamps)
    )
    lane, box = _lane(root)
    _submit(root)
    box.over = True
    lane.tick(T0)
    first = _only_entry(root)["admission"]["checked_at"]
    lane.tick(T0 + STEP)
    assert _only_entry(root)["admission"]["checked_at"] == first  # same reason, <30s
    lane.tick(T0 + ADMISSION_REWRITE_INTERVAL_S)
    assert _only_entry(root)["admission"]["checked_at"] != first  # refreshed


def test_admission_checked_at_most_every_interval(root: Path, popen: _PopenSpy) -> None:
    lane, box = _lane(root)
    box.over = True
    lane.tick(T0)  # empty queue still counts as a check
    _submit(root)
    box.over = False
    lane.tick(T0 + STEP - 1)
    assert popen.ops() == []
    lane.tick(T0 + STEP)
    assert len(popen.ops()) == 1


# ---------------------------------------------------------------- dispatch


def test_dispatches_by_rank_then_submission_one_at_a_time(
    root: Path, popen: _PopenSpy
) -> None:
    lane, _box = _lane(root)
    _submit(root, "job-research", submitted_by="worker", kwargs={"who": "research"})
    _submit(root, "job-owner", submitted_by="user", kwargs={"who": "owner"})
    lane.tick(T0)
    assert [c[2].payload()["kwargs"]["who"] for c in popen.ops()] == ["owner"]
    assert _status(root, "job-owner")["state"] == RUNNING
    assert _status(root, "job-research")["state"] == QUEUED
    lane.tick(T0 + STEP)
    assert len(popen.ops()) == 1  # lane busy: research stays queued
    popen.ops()[0][2].exit(0)
    lane.tick(T0 + 2 * STEP)
    assert [c[2].payload()["kwargs"]["who"] for c in popen.ops()] == [
        "owner",
        "research",
    ]
    assert _status(root, "job-owner")["state"] == "done"
    assert _status(root, "job-research")["state"] == RUNNING


def test_spawn_env_status_and_entry_shape(root: Path, popen: _PopenSpy) -> None:
    lane, _box = _lane(root)
    submitted = _submit(root, kwargs={"grid": "g1"})
    queued_status = _status(root)
    lane.tick(T0)
    args, kwargs, proc = popen.ops()[0]
    assert args == op_runner_command("experiments")
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == str(root)
    assert kwargs["stdin"] is subprocess.PIPE
    env = kwargs["env"]
    assert env["WAYFINDER_MAX_BACKTEST_WORKERS"] == str(worker_budget())
    assert env[STATUS_PATH_ENV] == str(_status_path(root))
    assert env[ENTRY_PATH_ENV] == _only_entry(root)["entry_path"]
    assert proc.payload() == {"op": "experiments", "kwargs": {"grid": "g1"}}
    assert proc.stdin.closed

    status = _status(root)
    assert status["state"] == RUNNING
    assert status["pid"] == proc.pid
    assert status["started_at"]
    assert status["worker_budget"] == worker_budget()
    for key in ("queued_at", "queue_entry", "lane_class", "notify", "submitted_by"):
        assert status[key] == queued_status[key]
    assert status["queue_entry"] == submitted["entry"]

    entry = _only_entry(root)
    assert entry["state"] == RUNNING
    assert entry["pid"] == proc.pid
    assert entry["started_at"] == status["started_at"]

    running = lane.snapshot()["running"]
    assert running["job_id"] == "job-a"
    assert running["op"] == "experiments"
    assert running["pid"] == proc.pid
    assert "running_s" in running
    assert lane.snapshot()["admission"]["last_reason"] is None


def test_max_runtime_kills_and_writes_timeout(
    root: Path, popen: _PopenSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "wayfinder_paths.runner.heavy_lane._kill_process_group",
        lambda pid, *, sig: kills.append((pid, sig)),
    )
    lane, _box = _lane(root)
    _submit(root, submitted_by="worker")  # research: 1800s
    lane.tick(T0)
    proc = popen.ops()[0][2]
    lane.tick(T0 + 1800)
    assert kills == []
    assert _status(root)["state"] == RUNNING
    lane.tick(T0 + 1801)
    assert kills == [(proc.pid, signal.SIGKILL)]
    status = _status(root)
    assert status["state"] == "timeout"
    assert status["reason"] == "max_runtime_s=1800"
    assert status["finished_at"]
    assert entries(root) == []
    assert len(popen.hooks()) == 1


def test_unlimited_class_never_times_out(root: Path, popen: _PopenSpy) -> None:
    lane, _box = _lane(root)
    _submit(root, op="evolution_evaluate", submitted_by="worker")
    lane.tick(T0)
    lane.tick(T0 + 10**7)
    assert _status(root, op="evolution_evaluate")["state"] == RUNNING
    assert popen.hooks() == []


def test_cancelled_between_listing_and_spawn_is_skipped(
    root: Path, popen: _PopenSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane, _box = _lane(root)
    _submit(root)
    entry_path = Path(_only_entry(root)["entry_path"])

    def cancel_then_admit(now: float) -> str | None:
        # Lands after the head was listed and before the locked re-read.
        cancel_heavy_op(root, "job-a", "experiments")
        return None

    monkeypatch.setattr(lane, "admission_reason", cancel_then_admit)
    lane.tick(T0)
    assert popen.ops() == []
    assert not entry_path.exists()
    assert _status(root)["state"] == CANCELLED


def test_cancelled_entries_are_swept(root: Path, popen: _PopenSpy) -> None:
    lane, _box = _lane(root)
    _submit(root, "job-a")
    _submit(root, "job-b")
    cancel_heavy_op(root, "job-a", "experiments")
    assert [e["state"] for e in entries(root)] == [CANCELLED, QUEUED]
    lane.tick(T0)
    remaining = _only_entry(root)
    assert remaining["job_id"] == "job-b"
    assert remaining["state"] == RUNNING
    assert popen.ops()[0][2].payload()["kwargs"] == {"job_id": "job-b"}


# ----------------------------------------------------------- credit policy


@pytest.fixture
def killpg(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    return calls


def _pause_file() -> dict[str, Any]:
    return json.loads(lane_pause_path().read_text(encoding="utf-8"))


def test_local_estimator_start_floor(
    root: Path, popen: _PopenSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane, box = _lane(root)
    _submit(root)
    box.local_credit(299.9)
    lane.tick(T0)
    assert popen.ops() == []
    assert _only_entry(root)["admission"]["last_reason"] == "low_credit"
    box.local_credit(300.0)
    lane.tick(T0 + STEP)
    assert len(popen.ops()) == 1
    monkeypatch.setenv("WAYFINDER_HEAVY_START_FLOOR_CPU_S", "500")
    assert lane.admission_reason(T0) == "low_credit"
    box.credit = {"source": "disabled"}
    assert lane.admission_reason(T0) is None


def test_governor_branch_keeps_over_quota(root: Path) -> None:
    lane, box = _lane(root)
    box.credit = {"source": "governor", "allow_new_heavy": True, "paused": False}
    assert lane.admission_reason(T0) is None
    box.credit = {"source": "governor", "allow_new_heavy": True, "paused": True}
    assert lane.admission_reason(T0) == "over_quota"
    box.credit = {"source": "governor", "allow_new_heavy": False, "paused": False}
    assert lane.admission_reason(T0) == "over_quota"


def test_pause_floor_stops_group_then_resumes_at_start_floor(
    root: Path,
    popen: _PopenSpy,
    killpg: list[tuple[int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane, box = _lane(root)
    _submit(root)
    box.local_credit(400.0)
    lane.tick(T0)
    proc = popen.ops()[0][2]
    group = [proc.pid, proc.pid + 7, proc.pid + 9]
    monkeypatch.setattr(
        "wayfinder_paths.runner.heavy_lane.process_group_pids", lambda _pgid: group
    )
    lane.tick(T0 + 1)
    assert killpg == []
    assert _pause_file()["paused"] is False
    assert _pause_file()["affected_pids"] == group

    box.local_credit(149.0)
    lane.tick(T0 + 2)
    assert killpg == [(proc.pid, signal.SIGSTOP)]
    pause = _pause_file()
    assert pause["paused"] is True
    assert pause["affected_pids"] == group
    assert pause["updated_at"] > 0
    paused_rows = [row for row in _journal(root) if row["type"] == JOURNAL_PAUSED]
    assert len(paused_rows) == 1
    assert paused_rows[0]["balance_cpu_seconds"] == 149.0
    assert paused_rows[0]["reason"] == "low_credit"
    snap = lane.snapshot()
    assert snap["paused"] is True
    assert snap["credit"] == {
        "source": "local_estimator",
        "balance_cpu_seconds": 149.0,
        "start_floor": 300.0,
        "pause_floor": 150.0,
    }

    box.local_credit(299.0)  # between the floors: stays stopped
    lane.tick(T0 + 3)
    assert killpg == [(proc.pid, signal.SIGSTOP)]
    box.local_credit(300.0)
    lane.tick(T0 + 12)
    assert killpg[-1] == (proc.pid, signal.SIGCONT)
    assert _pause_file()["paused"] is False
    resumed = [row for row in _journal(root) if row["type"] == JOURNAL_RESUMED]
    assert len(resumed) == 1
    assert resumed[0]["balance_cpu_seconds"] == 300.0
    assert lane.snapshot()["paused"] is False

    proc.exit(0)
    lane.tick(T0 + 13)
    assert not lane_pause_path().exists()
    assert lane.snapshot()["paused_s"] == 0.0


def test_live_tick_running_pauses_below_start_floor(
    root: Path, popen: _PopenSpy, killpg: list[tuple[int, int]]
) -> None:
    lane, box = _lane(root)
    _submit(root)
    box.local_credit(400.0)
    lane.tick(T0)
    proc = popen.ops()[0][2]
    box.jobs = [_job_row(7, LIVE_ENV, next_run_at=T0 + 3600)]
    box.running = {7}
    lane.tick(T0 + 1)  # credit above the start floor: the tick can share
    assert killpg == []
    box.local_credit(250.0)
    lane.tick(T0 + 2)
    assert killpg == [(proc.pid, signal.SIGSTOP)]
    assert [r["reason"] for r in _journal(root) if r["type"] == JOURNAL_PAUSED] == [
        "live_tick_running"
    ]


def test_governor_source_takes_over_a_lane_pause(
    root: Path, popen: _PopenSpy, killpg: list[tuple[int, int]]
) -> None:
    lane, box = _lane(root)
    _submit(root)
    box.local_credit(300.0)
    lane.tick(T0)
    proc = popen.ops()[0][2]
    box.local_credit(10.0)
    lane.tick(T0 + 1)
    box.credit = {"source": "governor", "allow_new_heavy": False, "paused": True}
    lane.tick(T0 + 2)
    assert killpg == [(proc.pid, signal.SIGSTOP), (proc.pid, signal.SIGCONT)]
    lane.tick(T0 + 3)  # the governor pauses on its own: no lane SIGSTOP
    assert len(killpg) == 2


def test_pending_cancel_continues_the_group_for_good(
    root: Path,
    popen: _PopenSpy,
    killpg: list[tuple[int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane, box = _lane(root)
    _submit(root)
    box.local_credit(300.0)
    lane.tick(T0)
    proc = popen.ops()[0][2]
    box.local_credit(10.0)
    lane.tick(T0 + 1)
    monkeypatch.setattr(
        "wayfinder_paths.runner.heavy_lane.sigterm_pending", lambda _pid: True
    )
    lane.tick(T0 + 2)
    assert killpg == [(proc.pid, signal.SIGSTOP), (proc.pid, signal.SIGCONT)]
    lane.tick(T0 + 3)
    assert len(killpg) == 2


def test_max_runtime_excludes_paused_time(
    root: Path,
    popen: _PopenSpy,
    killpg: list[tuple[int, int]],
) -> None:
    lane, box = _lane(root)
    _submit(root, submitted_by="worker")  # research: 1800s
    box.local_credit(300.0)
    lane.tick(T0)
    proc = popen.ops()[0][2]
    box.local_credit(10.0)
    lane.tick(T0 + 100)
    lane.tick(T0 + 2000)  # 100s active + 1900s stopped
    assert _status(root)["state"] == RUNNING
    assert lane.snapshot()["paused"] is True
    box.local_credit(300.0)
    lane.tick(T0 + 2100)  # resumes: 2000s paused banked
    assert _status(root)["state"] == RUNNING
    lane.tick(T0 + 3800)  # 1800s active
    assert _status(root)["state"] == RUNNING
    lane.tick(T0 + 3801)
    assert killpg[-1] == (proc.pid, signal.SIGKILL)
    assert _status(root)["state"] == "timeout"
    assert not lane_pause_path().exists()


# -------------------------------------------------------------------- reap


def test_reap_done_deletes_entry_and_spawns_hook(root: Path, popen: _PopenSpy) -> None:
    lane, _box = _lane(root)
    _submit(root)
    lane.tick(T0)
    running_entry = _only_entry(root)
    proc = popen.ops()[0][2]
    proc.exit(0)
    lane.tick(T0 + 1)
    status = _status(root)
    assert status["state"] == "done"
    assert status["exit_code"] == 0
    assert status["finished_at"]
    assert entries(root) == []
    assert lane.snapshot()["running"] is None

    args, kwargs, hook = popen.hooks()[0]
    assert args[1:] == ["-m", "wayfinder_paths.jobs.heavy_lane", "finished"]
    assert kwargs["stdin"] is subprocess.PIPE
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == str(root)
    assert hook.payload() == running_entry
    assert hook.stdin.closed


def test_reap_maps_exit_codes_and_keeps_child_terminal_state(
    root: Path, popen: _PopenSpy
) -> None:
    lane, _box = _lane(root)

    _submit(root, "job-fail")
    lane.tick(T0)
    popen.ops()[-1][2].exit(3)
    lane.tick(T0 + 1)
    assert _status(root, "job-fail")["state"] == "failed"
    assert _status(root, "job-fail")["exit_code"] == 3

    _submit(root, "job-kill")
    lane.tick(T0 + 2 * STEP)
    popen.ops()[-1][2].exit(-9)
    lane.tick(T0 + 2 * STEP + 1)
    assert _status(root, "job-kill")["state"] == "killed"

    _submit(root, "job-cancel")
    lane.tick(T0 + 4 * STEP)
    status_path = _status_path(root, "job-cancel")
    child_written = {**_status(root, "job-cancel"), "state": CANCELLED}
    status_path.write_text(json.dumps(child_written), encoding="utf-8")
    popen.ops()[-1][2].exit(-15)
    lane.tick(T0 + 4 * STEP + 1)
    assert _status(root, "job-cancel")["state"] == CANCELLED
    assert _status(root, "job-cancel")["exit_code"] == -15
    assert entries(root) == []
    assert len(popen.hooks()) == 3


# ------------------------------------------------------------------- adopt


def _running_entry_on_disk(root: Path, *, pid: int, **identity: Any) -> dict[str, Any]:
    _submit(root)
    entry = _only_entry(root)
    started_at = "2026-01-01T00:00:00+00:00"
    entry.update({"state": RUNNING, "pid": pid, "started_at": started_at, **identity})
    write_entry(entry)
    status_path = _status_path(root)
    status = {**_status(root), "state": RUNNING, "pid": pid, "started_at": started_at}
    status_path.write_text(json.dumps(status), encoding="utf-8")
    return entry


def test_adopt_dead_pid_with_result_is_done(root: Path, popen: _PopenSpy) -> None:
    _running_entry_on_disk(root, pid=DEAD_PID)
    _status_path(root).with_name("experiments.result.json").write_text(
        json.dumps({"ok": True}), encoding="utf-8"
    )
    lane, _box = _lane(root)
    lane.adopt()
    status = _status(root)
    assert status["state"] == "done"
    assert status["exit_code"] is None
    assert entries(root) == []
    assert len(popen.hooks()) == 1
    assert lane.snapshot()["running"] is None


def test_adopt_dead_pid_without_result_is_failed(root: Path, popen: _PopenSpy) -> None:
    _running_entry_on_disk(root, pid=DEAD_PID)
    lane, _box = _lane(root)
    lane.adopt()
    status = _status(root)
    assert status["state"] == "failed"
    assert status["error"] == "detached operation exited without a result"
    assert entries(root) == []


def test_adopt_boot_id_mismatch_is_lost(root: Path, popen: _PopenSpy) -> None:
    _running_entry_on_disk(root, pid=os.getpid(), boot_id="not-this-boot")
    lane, _box = _lane(root)
    lane.adopt()
    status = _status(root)
    assert status["state"] == "lost"
    assert status["reason"] == "boot_id mismatch"
    assert entries(root) == []  # no requeue
    assert lane.snapshot()["running"] is None
    assert len(popen.hooks()) == 1


def test_adopt_alive_child_then_reconciles_when_it_dies(
    root: Path, popen: _PopenSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    _running_entry_on_disk(root, pid=os.getpid())
    lane, _box = _lane(root)
    lane.adopt()
    assert popen.hooks() == []
    running = lane.snapshot()["running"]
    assert running["pid"] == os.getpid()
    assert "running_s" in running
    lane.tick(T0)
    assert _status(root)["state"] == RUNNING
    monkeypatch.setattr(
        "wayfinder_paths.runner.heavy_lane.recorded_process_alive", lambda _r: False
    )
    _status_path(root).with_name("experiments.result.json").write_text(
        "{}", encoding="utf-8"
    )
    lane.tick(T0 + 1)
    assert _status(root)["state"] == "done"
    assert entries(root) == []
    assert len(popen.hooks()) == 1


def test_adopt_continues_a_stopped_child(
    root: Path, popen: _PopenSpy, killpg: list[tuple[int, int]]
) -> None:
    _running_entry_on_disk(root, pid=os.getpid())
    lane, box = _lane(root)
    box.local_credit(10.0)
    lane.adopt()
    assert killpg == [(os.getpid(), signal.SIGCONT)]
    lane.tick(T0)  # the next tick re-applies the credit policy
    assert killpg[-1] == (os.getpid(), signal.SIGSTOP)


def test_stop_leaves_child_running(root: Path, popen: _PopenSpy) -> None:
    lane, _box = _lane(root)
    _submit(root)
    lane.tick(T0)
    lane.stop()
    assert _status(root)["state"] == RUNNING
    assert _only_entry(root)["state"] == RUNNING


# ------------------------------------------------------------------ daemon


def _daemon(tmp_path: Path) -> RunnerDaemon:
    runner_dir = tmp_path / ".wayfinder" / "runner"
    runs_dir = tmp_path / ".wayfinder_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    d = RunnerDaemon(
        paths=RunnerPaths(
            repo_root=tmp_path,
            runner_dir=runner_dir,
            db_path=runner_dir / "state.db",
            logs_dir=runner_dir / "logs",
            sock_path=runner_dir / "runner.sock",
        )
    )
    d.ctl_add_job(
        name="j",
        job_type=JOB_TYPE_SCRIPT,
        payload={"script_path": ".wayfinder_runs/hello.py"},
        interval_seconds=60,
    )
    return d


def test_daemon_lane_only_on_opencode_instance(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "wayfinder_paths.runner.daemon.is_opencode_instance", lambda: False
    )
    d = _daemon(root)
    assert d._heavy_lane is None
    assert d.ctl_status()["result"]["heavy_lane"] == {"source": "disabled"}


def test_daemon_ctl_status_heavy_lane_shape(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "wayfinder_paths.runner.daemon.is_opencode_instance", lambda: True
    )
    d = _daemon(root)
    assert d._heavy_lane is not None
    _submit(root)
    lane = d.ctl_status()["result"]["heavy_lane"]
    assert lane["running"] is None
    assert [q["job_id"] for q in lane["queued"]] == ["job-a"]
    assert lane["admission"] == {}


def test_daemon_tick_drives_lane_off_daemon_worker_budget(
    root: Path, popen: _PopenSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "wayfinder_paths.runner.daemon.is_opencode_instance", lambda: True
    )
    d = _daemon(root)
    d._burst = _FakeBurst(over=True)  # type: ignore[assignment]
    _submit(root)
    d.tick()
    assert popen.ops() == []
    assert _only_entry(root)["admission"]["last_reason"] == "over_quota"
    d._burst = _FakeBurst(over=False)  # type: ignore[assignment]
    assert d._heavy_lane is not None
    d._heavy_lane._last_admission_at = float("-inf")
    d.tick()
    assert len(popen.ops()) == 1
    assert _status(root)["state"] == RUNNING
    # The lane child is not a daemon worker.
    assert d.ctl_status()["result"]["running_workers"] == len(d._running)
    assert all(rp.popen.pid != popen.ops()[0][2].pid for rp in d._running.values())
    lane = d.ctl_status()["result"]["heavy_lane"]
    assert lane["running"]["job_id"] == "job-a"
    assert lane["admission"]["last_reason"] is None
