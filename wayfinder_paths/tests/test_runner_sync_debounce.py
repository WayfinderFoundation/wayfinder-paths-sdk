"""The backend sync push is debounced on the run-finish path (trailing edge,
coalescing) while start() and every ctl_* mutation keep an immediate flush.
Both pushes (scheduled-jobs bulk_sync + wayfinder-jobs sync_all_jobs) live in
the same debounced action, so they always fire together."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT, RunStatus
from wayfinder_paths.runner.daemon import RunnerDaemon, RunningProcess
from wayfinder_paths.runner.paths import RunnerPaths


def _paths(tmp_path: Path) -> RunnerPaths:
    runner_dir = tmp_path / ".wayfinder" / "runner"
    return RunnerPaths(
        repo_root=tmp_path,
        runner_dir=runner_dir,
        db_path=runner_dir / "state.db",
        logs_dir=runner_dir / "logs",
        sock_path=runner_dir / "runner.sock",
    )


def _wait_for(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _daemon_with_sync_counter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    debounce_seconds: str,
) -> tuple[RunnerDaemon, list[float]]:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-debounce")
    monkeypatch.setenv("WAYFINDER_SYNC_DEBOUNCE_SECONDS", debounce_seconds)
    daemon = RunnerDaemon(paths=_paths(tmp_path))
    calls: list[float] = []
    monkeypatch.setattr(
        daemon, "_start_backend_sync", lambda: calls.append(time.monotonic())
    )
    return daemon, calls


def test_finish_run_syncs_coalesce_on_the_trailing_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, calls = _daemon_with_sync_counter(
        tmp_path, monkeypatch, debounce_seconds="0.2"
    )

    for _ in range(5):
        daemon._sync_to_backend_async(flush=False)

    assert calls == []  # trailing edge: nothing fires inside the window
    assert _wait_for(lambda: len(calls) == 1)
    time.sleep(0.3)
    assert len(calls) == 1  # the burst coalesced into exactly one push


def test_flush_runs_immediately_and_cancels_the_pending_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, calls = _daemon_with_sync_counter(
        tmp_path, monkeypatch, debounce_seconds="0.2"
    )

    daemon._sync_to_backend_async(flush=False)
    daemon._sync_to_backend_async(flush=True)

    assert len(calls) == 1
    time.sleep(0.35)
    assert len(calls) == 1  # the pending debounced fire was cancelled


def test_ctl_mutations_flush_immediately_even_with_a_long_debounce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, calls = _daemon_with_sync_counter(
        tmp_path, monkeypatch, debounce_seconds="600"
    )
    runs_dir = tmp_path / ".wayfinder_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "hello.py").write_text("print('hi')\n", encoding="utf-8")

    resp = daemon.ctl_add_job(
        name="job-a",
        job_type=JOB_TYPE_SCRIPT,
        payload={"script_path": ".wayfinder_runs/hello.py"},
        interval_seconds=60,
    )
    assert resp["ok"] is True
    assert len(calls) == 1

    assert daemon.ctl_pause_job(name="job-a")["ok"] is True
    assert daemon.ctl_resume_job(name="job-a")["ok"] is True
    assert daemon.ctl_delete_job(name="job-a")["ok"] is True
    assert len(calls) == 4


def test_finish_run_uses_the_debounced_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, calls = _daemon_with_sync_counter(
        tmp_path, monkeypatch, debounce_seconds="0.2"
    )
    monkeypatch.setattr(daemon, "_report_finished_run", lambda *a, **k: None)
    job_id = daemon._db.add_job(
        name="job-a",
        job_type=JOB_TYPE_SCRIPT,
        payload={"script_path": "x.py"},
        interval_seconds=60,
        next_run_at=0,
    )
    now = int(time.time())
    run_id = daemon._db.reserve_run(
        job_id=job_id,
        started_at=now,
        next_run_at=now + 60,
        reason="schedule",
        scheduled_for=now,
    )
    rp = RunningProcess(
        run_id=run_id,
        job_id=job_id,
        job_name="job-a",
        started_at=now,
        reason="schedule",
        scheduled_for=now,
        timeout_seconds=None,
        popen=Mock(pid=123),
        log_path=daemon._paths.logs_dir / "job-a" / f"{run_id}.log",
    )

    daemon._finish_run(
        rp, finished_at=now, status=RunStatus.OK, exit_code=0, error_text=None
    )

    assert calls == []  # debounced, not immediate
    assert _wait_for(lambda: len(calls) == 1)


def test_stop_cancels_the_pending_debounced_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, calls = _daemon_with_sync_counter(
        tmp_path, monkeypatch, debounce_seconds="0.2"
    )

    daemon._sync_to_backend_async(flush=False)
    daemon.stop()
    time.sleep(0.35)

    assert calls == []


def test_non_positive_debounce_disables_the_delay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, calls = _daemon_with_sync_counter(
        tmp_path, monkeypatch, debounce_seconds="0"
    )

    daemon._sync_to_backend_async(flush=False)

    assert len(calls) == 1


def test_debounced_fire_runs_both_backend_pushes_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The action behind the debouncer is the real _start_backend_sync: one
    fire drives BOTH the scheduled-jobs registry bulk_sync and the
    wayfinder-jobs sync_all_jobs."""
    from wayfinder_paths.core.clients.ScheduledJobsClient import SCHEDULED_JOBS_CLIENT

    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-debounce")
    monkeypatch.setenv("WAYFINDER_SYNC_DEBOUNCE_SECONDS", "0.1")
    daemon = RunnerDaemon(paths=_paths(tmp_path))

    bulk_calls: list[list[dict]] = []
    sync_all_calls: list[dict] = []
    anchor_calls: list[dict] = []
    budget = {
        "balance_cpu_seconds": 640.0,
        "throttle_total_seconds": 12.0,
        "baseline_cores": 0.25,
        "observed_at": "2026-08-28T02:11:10+00:00",
    }
    monkeypatch.setattr(
        SCHEDULED_JOBS_CLIENT,
        "bulk_sync",
        lambda jobs: bulk_calls.append(jobs) or {"cpu_budget": budget},
    )
    monkeypatch.setattr(
        "wayfinder_paths.runner.daemon.write_cpu_budget_anchor",
        lambda payload: anchor_calls.append(payload),
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.sync.sync_all_jobs",
        lambda **kwargs: sync_all_calls.append(kwargs),
    )

    daemon._sync_to_backend_async(flush=False)
    daemon._sync_to_backend_async(flush=False)

    assert _wait_for(lambda: len(bulk_calls) == 1 and len(sync_all_calls) == 1)
    time.sleep(0.2)
    assert len(bulk_calls) == 1
    assert len(sync_all_calls) == 1
    assert anchor_calls == [budget]
