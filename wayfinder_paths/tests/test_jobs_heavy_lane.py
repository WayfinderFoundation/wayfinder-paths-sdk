"""The heavy-compute lane: a heavy op is a JSON submission under
`.wayfinder/heavy_lane/` plus a `queued` status file, ordered by class then
submission time; cancel withdraws a queued entry or SIGTERMs the running
child; the completion hook journals the outcome and prompts the session that
asked. Every reader of `state/background_ops/<op>.json` (op_status, readout,
island scheduler, remove refusal, gate restamp flag) must understand
`queued` without treating it as failed."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs import background, heavy_lane
from wayfinder_paths.jobs import contracts as contracts_module
from wayfinder_paths.jobs import readout as readout_module
from wayfinder_paths.jobs import remove as remove_module
from wayfinder_paths.jobs import sync as sync_module
from wayfinder_paths.jobs.execution import op_runner
from wayfinder_paths.jobs.execution import simulator as simulator_module
from wayfinder_paths.jobs.execution.op_process import (
    process_identity_fields,
    terminate_campaign_ops,
)
from wayfinder_paths.jobs.improver.scheduler import _continuation_ops
from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.triggers import ALWAYS_WAKE_EVENTS

JOB_A = "job-a"
JOB_B = "job-b"


def _store(tmp_path: Path, *job_ids: str) -> JobStore:
    store = JobStore(repo_root=tmp_path)
    for job_id in job_ids or (JOB_A,):
        store.job_dir(job_id).mkdir(parents=True, exist_ok=True)
    return store


def _status_path(store: JobStore, job_id: str, op: str) -> Path:
    return store.job_dir(job_id) / "state" / "background_ops" / f"{op}.json"


def _write_status(
    store: JobStore, job_id: str, op: str, status: dict[str, Any]
) -> Path:
    path = _status_path(store, job_id, op)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"op": op, "job_id": job_id, **status}), encoding="utf-8"
    )
    return path


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _journal(store: JobStore, job_id: str) -> list[dict[str, Any]]:
    path = store.job_dir(job_id) / "journal.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _running_status(pid: int) -> dict[str, Any]:
    return {
        "state": "running",
        "pid": pid,
        "started_at": utc_now_iso(),
        **process_identity_fields(pid),
    }


# --- submission ----------------------------------------------------------


def test_submit_writes_entry_and_queued_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scheduler_dir = store.job_dir(JOB_A) / "state"
    scheduler_dir.mkdir(parents=True, exist_ok=True)
    (scheduler_dir / "scheduler.json").write_text(
        json.dumps({"history": [{"island": "exploit"}]}), encoding="utf-8"
    )

    result = heavy_lane.submit_heavy_op(
        tmp_path, JOB_A, "experiments", {"grid": "g1"}, submitted_by="cli"
    )

    assert result["queued"] is True
    assert result["position"] == 0
    assert result["class"] == "owner"
    entries = heavy_lane.entries(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["entry_id"] == result["entry"]
    assert entry["op"] == "experiments"
    assert entry["job_id"] == JOB_A
    assert entry["kwargs"] == {"grid": "g1"}
    assert entry["state"] == heavy_lane.QUEUED
    assert entry["island"] == "exploit"
    assert entry["max_runtime_s"] == heavy_lane.MAX_RUNTIME_S["owner"]
    assert Path(entry["entry_path"]).is_file()
    assert Path(entry["status_path"]) == _status_path(store, JOB_A, "experiments")
    status = _read(_status_path(store, JOB_A, "experiments"))
    assert status["state"] == heavy_lane.QUEUED
    assert status["queue_entry"] == entry["entry_id"]
    assert status["lane_class"] == "owner"
    assert status["island"] == "exploit"
    assert status["queued_at"]


def test_submit_twice_returns_already_queued_same_entry(tmp_path: Path) -> None:
    _store(tmp_path)
    first = heavy_lane.submit_heavy_op(
        tmp_path, JOB_A, "experiments", {}, submitted_by="cli"
    )
    second = heavy_lane.submit_heavy_op(
        tmp_path, JOB_A, "experiments", {"other": True}, submitted_by="cli"
    )

    assert second["already_queued"] is True
    assert second["position"] == 0
    assert second["queue_entry"] == first["entry"]
    assert len(heavy_lane.entries(tmp_path)) == 1


def test_submit_returns_already_running_for_live_child(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_status(store, JOB_A, "experiments", _running_status(os.getpid()))

    result = heavy_lane.submit_heavy_op(
        tmp_path, JOB_A, "experiments", {}, submitted_by="cli"
    )

    assert result["already_running"] is True
    assert heavy_lane.entries(tmp_path) == []


def test_queue_orders_by_class_rank_then_submission_time(tmp_path: Path) -> None:
    _store(tmp_path, JOB_A, JOB_B)
    research = heavy_lane.submit_heavy_op(
        tmp_path, JOB_B, "experiments", {}, submitted_by="worker"
    )
    time.sleep(0.002)
    correctness = heavy_lane.submit_heavy_op(
        tmp_path, JOB_B, "backtest_job", {}, submitted_by="watchdog"
    )
    time.sleep(0.002)
    owner = heavy_lane.submit_heavy_op(
        tmp_path, JOB_A, "experiments", {}, submitted_by="cli"
    )

    assert (research["class"], correctness["class"], owner["class"]) == (
        "research",
        "correctness",
        "owner",
    )
    queued = [e["entry_id"] for e in heavy_lane.queued_entries(tmp_path)]
    assert queued == [owner["entry"], correctness["entry"], research["entry"]]
    assert heavy_lane.queue_position(tmp_path, owner["entry"]) == 0
    assert heavy_lane.queue_position(tmp_path, correctness["entry"]) == 1
    assert heavy_lane.queue_position(tmp_path, research["entry"]) == 2
    assert heavy_lane.queue_position(tmp_path, "nope") is None
    # The owner op reports its position at submission even though it was
    # submitted last.
    assert owner["position"] == 0


def test_same_class_orders_by_submission_time(tmp_path: Path) -> None:
    _store(tmp_path, JOB_A, JOB_B)
    first = heavy_lane.submit_heavy_op(
        tmp_path, JOB_B, "signal_scan", {}, submitted_by="cli"
    )
    time.sleep(0.002)
    second = heavy_lane.submit_heavy_op(
        tmp_path, JOB_A, "signal_scan", {}, submitted_by="cli"
    )

    queued = [e["entry_id"] for e in heavy_lane.queued_entries(tmp_path)]
    assert queued == [first["entry"], second["entry"]]


def test_classify_covers_every_class() -> None:
    assert heavy_lane.classify("evolution_evaluate", "cli", None) == "evolution"
    assert heavy_lane.classify("restamp", "cli", None) == "correctness"
    assert heavy_lane.classify("experiments", "watchdog", None) == "correctness"
    assert heavy_lane.classify("experiments", "sync", None) == "correctness"
    assert heavy_lane.classify("experiments", "worker", None) == "research"
    assert heavy_lane.classify("experiments", "cli", {"kind": "worker"}) == "research"
    assert heavy_lane.classify("experiments", "cli", {"kind": "user"}) == "owner"


def test_worker_budget_leaves_one_core_unless_overridden(monkeypatch) -> None:
    monkeypatch.delenv(heavy_lane.WORKER_BUDGET_ENV, raising=False)
    monkeypatch.setattr(heavy_lane.os, "cpu_count", lambda: 4)
    assert heavy_lane.worker_budget() == 3
    monkeypatch.setattr(heavy_lane.os, "cpu_count", lambda: 1)
    assert heavy_lane.worker_budget() == 1
    monkeypatch.setenv(heavy_lane.WORKER_BUDGET_ENV, "2")
    assert heavy_lane.worker_budget() == 2
    monkeypatch.setenv(heavy_lane.WORKER_BUDGET_ENV, "0")
    assert heavy_lane.worker_budget() == 1


# --- views ----------------------------------------------------------------


def test_lane_view_for_job_and_snapshot(tmp_path: Path) -> None:
    _store(tmp_path, JOB_A, JOB_B)
    a_first = heavy_lane.submit_heavy_op(
        tmp_path, JOB_A, "experiments", {}, submitted_by="cli"
    )
    time.sleep(0.002)
    heavy_lane.submit_heavy_op(tmp_path, JOB_B, "backtest_job", {}, submitted_by="cli")
    time.sleep(0.002)
    heavy_lane.submit_heavy_op(tmp_path, JOB_A, "signal_scan", {}, submitted_by="cli")

    # The dispatcher starts the head of the queue.
    running = next(
        e for e in heavy_lane.entries(tmp_path) if e["entry_id"] == a_first["entry"]
    )
    running.update({"state": heavy_lane.RUNNING, "pid": 4242, "started_at": "t1"})
    heavy_lane.write_entry(running)

    view_a = heavy_lane.lane_view_for_job(tmp_path, JOB_A)
    assert [(row["op"], row["state"], row["position"]) for row in view_a] == [
        ("experiments", heavy_lane.RUNNING, None),
        ("signal_scan", heavy_lane.QUEUED, 1),
    ]
    assert view_a[0]["started_at"] == "t1"
    assert view_a[0]["class"] == "owner"
    assert view_a[0]["submitted_by"] == "cli"
    view_b = heavy_lane.lane_view_for_job(tmp_path, JOB_B)
    assert [(row["op"], row["position"]) for row in view_b] == [("backtest_job", 0)]

    snapshot = heavy_lane.lane_snapshot(tmp_path)
    assert snapshot["running"] == {
        "job_id": JOB_A,
        "op": "experiments",
        "class": "owner",
        "pid": 4242,
        "started_at": "t1",
    }
    assert [(row["job_id"], row["op"]) for row in snapshot["queued"]] == [
        (JOB_B, "backtest_job"),
        (JOB_A, "signal_scan"),
    ]
    assert heavy_lane.running_entry(tmp_path)["entry_id"] == a_first["entry"]

    # A cancelled entry drops out of every view but stays readable.
    heavy_lane.cancel_heavy_op(tmp_path, JOB_A, "signal_scan")
    assert heavy_lane.lane_view_for_job(tmp_path, JOB_A) == view_a[:1]
    assert len(heavy_lane.entries(tmp_path)) == 3


def test_lane_views_on_empty_root(tmp_path: Path) -> None:
    assert heavy_lane.entries(tmp_path) == []
    assert heavy_lane.running_entry(tmp_path) is None
    assert heavy_lane.lane_view_for_job(tmp_path, JOB_A) == []
    assert heavy_lane.lane_snapshot(tmp_path) == {"running": None, "queued": []}


# --- cancel ---------------------------------------------------------------


def test_cancel_queued_marks_entry_and_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    submitted = heavy_lane.submit_heavy_op(
        tmp_path, JOB_A, "experiments", {}, submitted_by="cli"
    )

    outcome = heavy_lane.cancel_heavy_op(tmp_path, JOB_A, "experiments")

    assert outcome == {
        "cancelled": True,
        "was": heavy_lane.QUEUED,
        "op": "experiments",
        "job_id": JOB_A,
    }
    status = _read(_status_path(store, JOB_A, "experiments"))
    assert status["state"] == heavy_lane.CANCELLED
    assert status["reason"] == "op_cancel"
    assert status["finished_at"]
    entry = next(
        e for e in heavy_lane.entries(tmp_path) if e["entry_id"] == submitted["entry"]
    )
    assert entry["state"] == heavy_lane.CANCELLED
    assert entry["cancelled_at"]
    assert heavy_lane.queued_entries(tmp_path) == []
    # A cancelled slot is free again.
    again = heavy_lane.submit_heavy_op(
        tmp_path, JOB_A, "experiments", {}, submitted_by="cli"
    )
    assert again["queued"] is True


def test_cancel_running_terminates_the_child_process_group(tmp_path: Path) -> None:
    store = _store(tmp_path)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        _write_status(store, JOB_A, "experiments", _running_status(child.pid))

        outcome = heavy_lane.cancel_heavy_op(tmp_path, JOB_A, "experiments")

        assert outcome["cancelled"] is True
        assert outcome["was"] == heavy_lane.RUNNING
        assert outcome["pid"] == child.pid
        assert child.wait(timeout=10) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_cancel_running_reaches_a_group_the_lane_paused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        os.killpg(child.pid, signal.SIGSTOP)
        _write_status(store, JOB_A, "experiments", _running_status(child.pid))

        heavy_lane.cancel_heavy_op(tmp_path, JOB_A, "experiments")

        assert child.wait(timeout=10) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_cancel_reports_missing_and_finished_ops(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert heavy_lane.cancel_heavy_op(tmp_path, JOB_A, "experiments") == {
        "cancelled": False,
        "error": "not_found",
    }
    _write_status(store, JOB_A, "experiments", {"state": "done"})
    outcome = heavy_lane.cancel_heavy_op(tmp_path, JOB_A, "experiments")
    assert outcome == {"cancelled": False, "error": "already_finished", "state": "done"}
    _write_status(store, JOB_A, "backtest_job", _running_status(2**22 - 1))
    outcome = heavy_lane.cancel_heavy_op(tmp_path, JOB_A, "backtest_job")
    assert outcome["cancelled"] is False
    assert outcome["error"] == "already_finished"


# --- harvest + summaries --------------------------------------------------


def test_mark_harvested_stamps_only_done_unharvested_ops(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_status(store, JOB_A, "experiments", {"state": "done"})
    _write_status(store, JOB_A, "backtest_job", {"state": "done", "harvested": True})
    _write_status(store, JOB_A, "signal_scan", _running_status(os.getpid()))
    _write_status(store, JOB_A, "rank_check", {"state": "queued"})

    stamped = heavy_lane.mark_harvested(
        store.job_dir(JOB_A),
        ["experiments", "backtest_job", "signal_scan", "rank_check", "missing"],
        "wake-7",
    )

    assert stamped == ["experiments"]
    experiments = _read(_status_path(store, JOB_A, "experiments"))
    assert experiments["harvested"] is True
    assert experiments["harvested_by"] == "wake-7"
    assert experiments["harvested_at"]
    assert "harvested_by" not in _read(_status_path(store, JOB_A, "backtest_job"))
    assert "harvested" not in _read(_status_path(store, JOB_A, "signal_scan"))
    assert "harvested" not in _read(_status_path(store, JOB_A, "rank_check"))


def test_result_summary_shapes() -> None:
    experiment = heavy_lane.result_summary(
        {
            "experiment": {
                "grid_id": "g1",
                "run_count": 12,
                "invalid_count": 1,
                "rank_by": "sharpe",
                "best": {
                    "params": {"lookback": 20},
                    "stats": {"sharpe": 1.4, "net_return": 0.2, "equity_curve": [1]},
                },
                "walk_forward": {"decay_ratio": 0.8, "folds": 4, "raw": "big"},
            }
        }
    )
    assert experiment == {
        "experiment": {
            "grid_id": "g1",
            "run_count": 12,
            "invalid_count": 1,
            "rank_by": "sharpe",
            "best_params": {"lookback": 20},
            "best_stats": {"sharpe": 1.4, "net_return": 0.2},
            "walk_forward": {"decay_ratio": 0.8, "folds": 4},
        }
    }

    backtest = heavy_lane.result_summary(
        {"backtest": {"stats": {"net_return": 0.1, "trade_count": 9, "bars": 5000}}}
    )
    assert backtest == {"backtest": {"stats": {"net_return": 0.1, "trade_count": 9}}}

    nested = heavy_lane.result_summary(
        {"result": {"result": {"stats": {"max_drawdown": -0.05}}}}
    )
    assert nested == {"result": {"stats": {"max_drawdown": -0.05}}}

    error = heavy_lane.result_summary({"error": "x" * 500})
    assert error == {"error": "x" * 200}

    unknown = heavy_lane.result_summary({"zeta": 1, "alpha": 2})
    assert unknown == {"keys": ["alpha", "zeta"]}

    assert heavy_lane.result_summary(None) == {"available": False}
    assert heavy_lane.result_summary([1, 2]) == {"available": False}


def test_completion_text_names_op_job_and_timing() -> None:
    entry = {
        "op": "experiments",
        "job_id": JOB_A,
        "submitted_at": "2026-09-22T10:00:00+00:00",
    }
    status = {
        "state": "done",
        "started_at": "2026-09-22T10:01:00+00:00",
        "finished_at": "2026-09-22T10:03:00+00:00",
    }
    text = heavy_lane.completion_text(entry, status)
    assert text.startswith(
        "Background op `experiments` for job `job-a` finished "
        "(done in 2 min after 1 min queued)."
    )
    assert "core_jobs(action='op_status', job_id='job-a', op='experiments')" in text

    bare = heavy_lane.completion_text(
        {"op": "experiments", "job_id": JOB_A}, {"state": "failed"}
    )
    assert "finished (failed)." in bare


# --- completion hook ------------------------------------------------------


class _FakeClient:
    def __init__(
        self, *, exists: bool = True, busy_rounds: int = 0, accept: bool = True
    ) -> None:
        self.exists = exists
        self.busy_rounds = busy_rounds
        self.accept = accept
        self.prompts: list[tuple[str, str]] = []

    def session_exists(self, session_id: str) -> bool:
        return self.exists

    def session_statuses(self) -> dict[str, Any]:
        if self.busy_rounds > 0:
            self.busy_rounds -= 1
            return {"ses-1": {"type": "busy"}}
        return {}

    def prompt_async(self, session_id: str, text: str) -> bool:
        self.prompts.append((session_id, text))
        return self.accept


def _finished_entry(
    tmp_path: Path, store: JobStore, *, notify: dict[str, Any] | None
) -> dict[str, Any]:
    heavy_lane.submit_heavy_op(
        tmp_path, JOB_A, "experiments", {}, submitted_by="cli", notify=notify
    )
    entry = heavy_lane.entries(tmp_path)[0]
    status_path = Path(entry["status_path"])
    status = _read(status_path)
    status.update(
        {
            "state": "done",
            "started_at": "2026-09-22T10:01:00+00:00",
            "finished_at": "2026-09-22T10:03:00+00:00",
        }
    )
    status_path.write_text(json.dumps(status), encoding="utf-8")
    status_path.with_name("experiments.result.json").write_text(
        json.dumps({"experiment": {"grid_id": "g1", "best": {"stats": {"sharpe": 2}}}}),
        encoding="utf-8",
    )
    return entry


def test_completion_hook_prompts_idle_session_and_journals(tmp_path: Path) -> None:
    store = _store(tmp_path)
    entry = _finished_entry(
        tmp_path, store, notify={"session_id": "ses-1", "kind": "user"}
    )
    client = _FakeClient()
    sleeps: list[float] = []

    outcome = heavy_lane.run_completion_hook(entry, client=client, sleep=sleeps.append)

    assert outcome == {"journaled": True, "notified": True}
    assert len(client.prompts) == 1
    session_id, text = client.prompts[0]
    assert session_id == "ses-1"
    assert text == heavy_lane.completion_text(entry, _read(Path(entry["status_path"])))
    assert sleeps == []
    status = _read(Path(entry["status_path"]))
    assert status["notified_at"]
    assert "notify_error" not in status
    rows = _journal(store, JOB_A)
    finished = [r for r in rows if r["type"] == heavy_lane.JOURNAL_FINISHED]
    assert len(finished) == 1
    assert finished[0]["op"] == "experiments"
    assert finished[0]["state"] == "done"
    assert finished[0]["class"] == "owner"
    assert finished[0]["duration_s"] == 120.0
    assert finished[0]["summary"] == {
        "experiment": {
            "grid_id": "g1",
            "best_params": None,
            "best_stats": {"sharpe": 2},
        }
    }
    assert not [r for r in rows if r["type"] == heavy_lane.JOURNAL_NOTIFY_DROPPED]


def test_completion_hook_waits_for_busy_session(tmp_path: Path) -> None:
    store = _store(tmp_path)
    entry = _finished_entry(
        tmp_path, store, notify={"session_id": "ses-1", "kind": "user"}
    )
    client = _FakeClient(busy_rounds=2)
    sleeps: list[float] = []

    outcome = heavy_lane.run_completion_hook(entry, client=client, sleep=sleeps.append)

    assert outcome["notified"] is True
    assert sleeps == [heavy_lane.NOTIFY_RETRY_EVERY_S] * 2
    assert len(client.prompts) == 1
    assert _read(Path(entry["status_path"]))["notified_at"]


def test_completion_hook_drops_notify_when_session_is_gone(tmp_path: Path) -> None:
    store = _store(tmp_path)
    entry = _finished_entry(
        tmp_path, store, notify={"session_id": "ses-1", "kind": "user"}
    )
    client = _FakeClient(exists=False)

    outcome = heavy_lane.run_completion_hook(entry, client=client, sleep=lambda s: None)

    assert outcome == {"journaled": True, "notified": False, "error": "session_gone"}
    assert client.prompts == []
    status = _read(Path(entry["status_path"]))
    assert status["notify_error"] == "session_gone"
    assert "notified_at" not in status
    dropped = [
        r
        for r in _journal(store, JOB_A)
        if r["type"] == heavy_lane.JOURNAL_NOTIFY_DROPPED
    ]
    assert len(dropped) == 1
    assert dropped[0]["session_id"] == "ses-1"
    assert dropped[0]["error"] == "session_gone"


def test_completion_hook_records_rejected_prompt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    entry = _finished_entry(
        tmp_path, store, notify={"session_id": "ses-1", "kind": "user"}
    )
    client = _FakeClient(accept=False)

    outcome = heavy_lane.run_completion_hook(entry, client=client, sleep=lambda s: None)

    assert outcome["error"] == "prompt_rejected"
    assert _read(Path(entry["status_path"]))["notify_error"] == "prompt_rejected"


def test_completion_hook_without_session_only_journals(tmp_path: Path) -> None:
    store = _store(tmp_path)
    entry = _finished_entry(tmp_path, store, notify=None)
    client = _FakeClient()
    before = _read(Path(entry["status_path"]))

    outcome = heavy_lane.run_completion_hook(entry, client=client, sleep=lambda s: None)

    assert outcome == {"journaled": True, "notified": False}
    assert client.prompts == []
    assert _read(Path(entry["status_path"])) == before
    rows = _journal(store, JOB_A)
    assert [r["type"] for r in rows] == [heavy_lane.JOURNAL_FINISHED]


def test_prompt_session_gives_up_after_deadline() -> None:
    client = _FakeClient(busy_rounds=10**6)
    sleeps: list[float] = []
    outcome = heavy_lane._prompt_session(
        client,
        "ses-1",
        "hello",
        sleep=sleeps.append,
        retry_every_s=1.0,
        retry_for_s=0.0,
    )
    assert outcome == {"notified": False, "error": "session_busy"}
    assert sleeps == []
    assert client.prompts == []


# --- op_runner SIGTERM ----------------------------------------------------


class _Exit(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


def test_op_runner_sigterm_handler_records_cancellation(
    tmp_path: Path, monkeypatch
) -> None:
    assert op_runner.STATUS_PATH_ENV == heavy_lane.STATUS_PATH_ENV
    status_path = tmp_path / "experiments.json"
    status_path.write_text(
        json.dumps({"op": "experiments", "state": "running", "pid": 123}),
        encoding="utf-8",
    )
    monkeypatch.setenv(op_runner.STATUS_PATH_ENV, str(status_path))

    def fake_exit(code: int) -> None:
        raise _Exit(code)

    monkeypatch.setattr(os, "_exit", fake_exit)

    with pytest.raises(_Exit) as excinfo:
        op_runner._cancel_on_sigterm(signal.SIGTERM, None)

    assert excinfo.value.code == 143
    status = _read(status_path)
    assert status["state"] == "cancelled"
    assert status["reason"] == "op_cancel"
    assert status["finished_at"]
    assert status["pid"] == 123


def test_op_runner_installs_handler_only_for_lane_children(
    tmp_path: Path, monkeypatch
) -> None:
    previous = signal.getsignal(signal.SIGTERM)
    try:
        monkeypatch.delenv(op_runner.STATUS_PATH_ENV, raising=False)
        op_runner._install_cancel_handler()
        assert signal.getsignal(signal.SIGTERM) is previous

        monkeypatch.setenv(op_runner.STATUS_PATH_ENV, str(tmp_path / "s.json"))
        op_runner._install_cancel_handler()
        assert signal.getsignal(signal.SIGTERM) is op_runner._cancel_on_sigterm
    finally:
        signal.signal(signal.SIGTERM, previous)


# --- readers understand `queued` ------------------------------------------


def test_op_status_summary_reports_queued(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = _write_status(
        store, JOB_A, "experiments", {"state": "queued", "queued_at": "t0"}
    )

    summary = background.op_status_summary(store.job_dir(JOB_A), "experiments")

    assert summary == {"status": "queued", "queued_at": "t0"}
    # No reconciliation write: the lane owns a queued op's lifetime.
    assert _read(path)["state"] == "queued"


def test_spawn_detached_op_submits_heavy_ops_to_the_lane(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(heavy_lane, "lane_enabled", lambda: True)
    spawned: list[str] = []
    monkeypatch.setattr(
        background,
        "_spawn_detached_op",
        lambda store, job_id, op, kwargs: spawned.append(op) or {"started": True},
    )

    result = background.spawn_detached_op(store, JOB_A, "experiments", {"g": 1})

    assert result["queued"] is True
    assert result["class"] == "owner"
    assert result["submitted_by"] == "cli"
    assert _read(_status_path(store, JOB_A, "experiments"))["state"] == "queued"
    assert len(heavy_lane.queued_entries(tmp_path)) == 1
    assert spawned == []

    # Ops outside the lane keep spawning detached even where the lane runs.
    result = background.spawn_detached_op(store, JOB_A, "fetch_dataset", {})
    assert result == {"started": True}
    assert spawned == ["fetch_dataset"]


def test_spawn_detached_op_spawns_directly_without_the_lane(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(heavy_lane, "lane_enabled", lambda: False)
    spawned: list[str] = []
    monkeypatch.setattr(
        background,
        "_spawn_detached_op",
        lambda store, job_id, op, kwargs: spawned.append(op) or {"started": True},
    )

    assert background.spawn_detached_op(store, JOB_A, "experiments", {}) == {
        "started": True
    }
    assert spawned == ["experiments"]
    assert heavy_lane.entries(tmp_path) == []


def test_readout_pending_ops_includes_queued(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_status(store, JOB_A, "backtest_job", _running_status(2**22 - 1))
    _write_status(store, JOB_A, "experiments", {"state": "queued"})
    _write_status(store, JOB_A, "robustness_check", _running_status(os.getpid()))
    _write_status(store, JOB_A, "signal_scan", {"state": "done"})
    (
        store.job_dir(JOB_A) / "state" / "background_ops" / "signal_scan.result.json"
    ).write_text("{}", encoding="utf-8")

    assert readout_module._pending_ops(store.job_dir(JOB_A)) == [
        "experiments (queued)",
        "robustness_check",
    ]
    assert readout_module._pending_ops(tmp_path / "nowhere") == []


def test_scheduler_continuation_keeps_queued_ops(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_status(store, JOB_A, "experiments", {"state": "queued"})
    _write_status(store, JOB_A, "backtest_job", _running_status(2**22 - 1))
    _write_status(store, JOB_A, "evolution_evaluate", {"state": "queued"})

    assert _continuation_ops(store.job_dir(JOB_A), now=None) == ["experiments"]


def test_remove_refuses_while_op_is_queued(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_status(store, JOB_A, "experiments", {"state": "queued"})

    with pytest.raises(
        ValueError,
        match=r"^cannot remove: a background operation is still queued \(experiments\)",
    ) as excinfo:
        remove_module._refuse_while_background_op_running(store, JOB_A)
    assert 'core_jobs(action="op_cancel")' in str(excinfo.value)


def test_remove_running_refusal_names_op_cancel(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_status(store, JOB_A, "experiments", _running_status(os.getpid()))

    with pytest.raises(
        ValueError,
        match=r"^cannot remove: a background operation is still running \(experiments\)",
    ) as excinfo:
        remove_module._refuse_while_background_op_running(store, JOB_A)
    assert 'core_jobs(action="op_cancel")' in str(excinfo.value)


def test_remove_ignores_cancelled_and_done_ops(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_status(store, JOB_A, "experiments", {"state": "cancelled"})
    _write_status(store, JOB_A, "backtest_job", {"state": "done"})
    remove_module._refuse_while_background_op_running(store, JOB_A)


def test_gate_with_restamp_flags_queued_restamp(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(
        contracts_module,
        "evaluate_live_readiness",
        lambda job_id, store=None: {"live_ready": False, "reasons": ["stale"]},
    )

    assert "restamp_in_progress" not in sync_module._gate_with_restamp(JOB_A, store)

    _write_status(store, JOB_A, "restamp", {"state": "queued", "queued_at": "t0"})
    gate = sync_module._gate_with_restamp(JOB_A, store)
    assert gate["restamp_in_progress"] is True
    assert gate["restamp_state"] == "queued"
    assert gate["restamp_started_at"] is None
    assert gate["live_ready"] is False

    _write_status(store, JOB_A, "restamp", {**_running_status(os.getpid())})
    gate = sync_module._gate_with_restamp(JOB_A, store)
    assert gate["restamp_in_progress"] is True
    assert gate["restamp_state"] == "running"
    assert gate["restamp_started_at"]

    _write_status(store, JOB_A, "restamp", _running_status(2**22 - 1))
    assert "restamp_in_progress" not in sync_module._gate_with_restamp(JOB_A, store)

    _write_status(store, JOB_A, "restamp", {"state": "cancelled"})
    assert "restamp_in_progress" not in sync_module._gate_with_restamp(JOB_A, store)


def test_effective_workers_leaves_one_core_for_the_live_tick(monkeypatch) -> None:
    monkeypatch.setattr(simulator_module, "available_cpu_count", lambda: 4)
    assert simulator_module._effective_workers(0, "process") == 3
    assert simulator_module._effective_workers(-1, "process") == 3
    assert simulator_module._effective_workers(2, "process") == 2
    assert simulator_module._effective_workers(9, "process") == 4
    monkeypatch.setattr(simulator_module, "available_cpu_count", lambda: 1)
    assert simulator_module._effective_workers(0, "process") == 1


# --- system submitters ----------------------------------------------------


def test_system_heavy_ops_join_the_lane() -> None:
    assert {"evolution_evaluate", "evolution_finalize", "restamp"} <= (
        heavy_lane.HEAVY_LANE_OPS
    )
    assert heavy_lane.classify("evolution_finalize", "watchdog", None) == "evolution"
    assert heavy_lane.classify("restamp", "sync", None) == "correctness"


def test_spawn_detached_op_passes_submitter_and_notify_to_the_lane(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(heavy_lane, "lane_enabled", lambda: True)

    result = background.spawn_detached_op(
        store,
        JOB_A,
        "restamp",
        {"job_id": JOB_A},
        submitted_by="watchdog",
        notify={"wake": True},
    )

    assert result["queued"] is True
    assert result["class"] == "correctness"
    status = _read(_status_path(store, JOB_A, "restamp"))
    assert status["submitted_by"] == "watchdog"
    assert status["notify"]["wake"] is True
    (entry,) = heavy_lane.queued_entries(tmp_path)
    assert entry["submitted_by"] == "watchdog"
    assert entry["notify"]["wake"] is True


def test_completion_hook_wakes_the_job_when_asked(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    job = WayfinderJob.new(JOB_A, agent_mode="intervene")
    assert job.id == JOB_A
    store.save(job)
    entry = _finished_entry(tmp_path, store, notify={"wake": True})
    fired: list[dict[str, Any]] = []

    def fake_fire(store_arg, job_arg, events, *, source):  # noqa: ANN001
        fired.append({"job_id": job_arg.id, "events": events, "source": source})
        return {"triggers": events}

    monkeypatch.setattr(heavy_lane, "fire_triggers", fake_fire)
    client = _FakeClient()

    outcome = heavy_lane.run_completion_hook(entry, client=client, sleep=lambda s: None)

    assert fired == [
        {"job_id": JOB_A, "events": ["background_op_finished"], "source": "heavy_lane"}
    ]
    assert outcome == {"journaled": True, "notified": False, "woke": True}
    assert client.prompts == []  # no user session asked for a prompt
    assert "background_op_finished" in ALWAYS_WAKE_EVENTS


def test_completion_hook_without_wake_never_fires_triggers(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    entry = _finished_entry(tmp_path, store, notify=None)
    monkeypatch.setattr(
        heavy_lane,
        "fire_triggers",
        lambda *a, **k: pytest.fail("no wake was requested"),
    )

    outcome = heavy_lane.run_completion_hook(
        entry, client=_FakeClient(), sleep=lambda s: None
    )

    assert "woke" not in outcome


def test_terminate_campaign_ops_withdraws_queued_campaign_entries(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, JOB_A, JOB_B)
    for job_id in (JOB_A, JOB_B):
        heavy_lane.submit_heavy_op(
            tmp_path,
            job_id,
            "evolution_evaluate",
            {"job_id": job_id, "candidate_id": "c01"},
            submitted_by="watchdog",
        )
    heavy_lane.submit_heavy_op(
        tmp_path, JOB_A, "experiments", {"job_id": JOB_A}, submitted_by="cli"
    )

    reaped = terminate_campaign_ops(store, JOB_A, "campaign-1")

    assert reaped == [{"pid": None, "op": "evolution_evaluate", "state": "queued"}]
    assert _read(_status_path(store, JOB_A, "evolution_evaluate"))["state"] == (
        heavy_lane.CANCELLED
    )
    still_queued = {(e["job_id"], e["op"]) for e in heavy_lane.queued_entries(tmp_path)}
    assert still_queued == {(JOB_B, "evolution_evaluate"), (JOB_A, "experiments")}


def test_sync_scorecard_carries_the_job_lane_view(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(WayfinderJob.new(JOB_A, agent_mode="intervene"))
    heavy_lane.submit_heavy_op(
        tmp_path, JOB_A, "restamp", {"job_id": JOB_A}, submitted_by="sync"
    )

    scorecard = sync_module.snapshot_job(JOB_A, store=store)["scorecard"]

    assert [
        (row["op"], row["state"], row["position"]) for row in scorecard["pending_ops"]
    ] == [("restamp", heavy_lane.QUEUED, 0)]
    assert scorecard["pending_ops"][0]["submitted_by"] == "sync"
