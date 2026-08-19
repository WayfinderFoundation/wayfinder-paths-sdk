"""WarmChild must duck-type exactly the Popen surface the daemon's _reap uses
(.pid, .poll(), .returncode — None until an exit is observed), the daemon's
process-group kills must land on a setsid'd forked child, and ANY warm-spawn
failure must fall back to the cold Popen path (these ticks trade real money).
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT
from wayfinder_paths.runner.daemon import RunnerDaemon, _kill_process_group
from wayfinder_paths.runner.paths import RunnerPaths
from wayfinder_paths.runner.warm_spawn import WarmChild


def _forkserver_ctx() -> multiprocessing.context.BaseContext:
    return multiprocessing.get_context("forkserver")


# Module-level targets: the forkserver child imports this test module by name
# to unpickle them, so they must not be nested.
def _exit_with_code_7() -> None:
    raise SystemExit(7)


def _setsid_and_sleep() -> None:
    os.setsid()
    time.sleep(60)


def _wait_for(predicate, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_warm_child_poll_reports_none_then_the_exit_code() -> None:
    process = _forkserver_ctx().Process(target=_exit_with_code_7, daemon=False)
    process.start()
    child = WarmChild(process)

    assert child.pid == process.pid
    assert child.returncode is None  # never an exit code before one exists
    assert _wait_for(lambda: child.poll() is not None)
    assert child.poll() == 7
    assert child.returncode == 7


def test_warm_child_killpg_timeout_kill_lands_on_the_sleeping_child() -> None:
    """Exactly what _reap does on timeout: killpg(SIGKILL) against the child's
    pid — valid only because the warm entry calls os.setsid() first, mirroring
    Popen(start_new_session=True)."""
    process = _forkserver_ctx().Process(target=_setsid_and_sleep, daemon=False)
    process.start()
    child = WarmChild(process)

    assert _wait_for(lambda: os.getpgid(child.pid) == child.pid)  # setsid has happened
    assert child.poll() is None
    _kill_process_group(child.pid, sig=signal.SIGKILL)
    assert _wait_for(lambda: child.poll() is not None)
    assert child.poll() == -signal.SIGKILL  # Popen's signal-death convention
    assert child.returncode == -signal.SIGKILL


def _paths(tmp_path: Path) -> RunnerPaths:
    runner_dir = tmp_path / ".wayfinder" / "runner"
    return RunnerPaths(
        repo_root=tmp_path,
        runner_dir=runner_dir,
        db_path=runner_dir / "state.db",
        logs_dir=runner_dir / "logs",
        sock_path=runner_dir / "runner.sock",
    )


def _jobs_v1_env(job_dir: str = "/tmp/job") -> dict[str, str]:
    return {
        "WAYFINDER_JOB_EXECUTION_CONTRACT": "jobs_v1",
        "WAYFINDER_JOB_DIR": job_dir,
    }


def test_warm_spawn_eligibility_gates(tmp_path: Path) -> None:
    daemon = RunnerDaemon(paths=_paths(tmp_path))
    eligible = daemon._warm_spawn_eligible

    assert eligible(job_type=JOB_TYPE_SCRIPT, env=_jobs_v1_env()) is True
    assert eligible(job_type="strategy", env=_jobs_v1_env()) is False
    assert (
        eligible(
            job_type=JOB_TYPE_SCRIPT,
            env={**_jobs_v1_env(), "WAYFINDER_RUNNER_NO_FORK": "1"},
        )
        is False
    )
    assert (
        eligible(
            job_type=JOB_TYPE_SCRIPT,
            env={**_jobs_v1_env(), "WAYFINDER_RUNNER_NO_FORK": "0"},
        )
        is True
    )
    assert (
        eligible(
            job_type=JOB_TYPE_SCRIPT,
            env={**_jobs_v1_env(), "WAYFINDER_JOB_AGENT_MODE": "monitor"},
        )
        is False
    )
    assert (
        eligible(
            job_type=JOB_TYPE_SCRIPT,
            env={**_jobs_v1_env(), "WAYFINDER_JOB_EXECUTION_CONTRACT": "legacy"},
        )
        is False
    )
    assert (
        eligible(
            job_type=JOB_TYPE_SCRIPT,
            env={"WAYFINDER_JOB_EXECUTION_CONTRACT": "jobs_v1"},
        )
        is False
    )  # no WAYFINDER_JOB_DIR


def test_warm_spawn_failure_falls_back_to_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = tmp_path / ".wayfinder_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "tick.py").write_text("print('hi')\n", encoding="utf-8")

    daemon = RunnerDaemon(paths=_paths(tmp_path))
    resp = daemon.ctl_add_job(
        name="warm-job",
        job_type=JOB_TYPE_SCRIPT,
        payload={
            "script_path": ".wayfinder_runs/tick.py",
            "env": _jobs_v1_env(str(tmp_path / "jobdir")),
        },
        interval_seconds=60,
    )
    assert resp["ok"] is True

    def _boom(**_kwargs):
        raise RuntimeError("forkserver is dead")

    monkeypatch.setattr(daemon, "_spawn_warm_child", _boom)
    popen = Mock()
    popen.pid = 4242
    popen_calls: list[tuple] = []
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *args, **kwargs: popen_calls.append((args, kwargs)) or popen,
    )

    job, state = daemon._db.get_job(name="warm-job")
    run_id = daemon._maybe_start_job(
        job={
            "id": job.id,
            "name": job.name,
            "type": job.type,
            "payload": job.payload,
            "interval_seconds": job.interval_seconds,
            "schedule_kind": job.schedule_kind,
            "cron_expr": job.cron_expr,
            "timezone": job.timezone,
            "next_run_at": int(time.time()),
        },
        now=int(time.time()),
        reason="schedule",
    )

    assert run_id is not None
    assert len(popen_calls) == 1  # cold path took over
    run = daemon._db.get_run(run_id=run_id)
    assert run is not None and run["pid"] == 4242
    assert daemon._running[run_id].popen is popen
