from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

from wayfinder_paths.core.clients.OpenCodeClient import OPENCODE_CLIENT
from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT, RunStatus
from wayfinder_paths.runner.daemon import RunnerDaemon, RunningProcess
from wayfinder_paths.runner.db import RunnerDB
from wayfinder_paths.runner.paths import RunnerPaths
from wayfinder_paths.runner.script_resolver import resolve_script_path


def _paths(tmp_path: Path) -> RunnerPaths:
    runner_dir = tmp_path / ".wayfinder" / "runner"
    return RunnerPaths(
        repo_root=tmp_path,
        runner_dir=runner_dir,
        db_path=runner_dir / "state.db",
        logs_dir=runner_dir / "logs",
        sock_path=runner_dir / "runner.sock",
    )


def test_resolve_script_path_only_allows_wayfinder_runs(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    runs_dir = tmp_path / ".wayfinder_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    script = runs_dir / "hello.py"
    script.write_text("print('hi')\n", encoding="utf-8")

    resolved = resolve_script_path(p, ".wayfinder_runs/hello.py")
    assert resolved.exists()
    assert resolved.name == "hello.py"

    outside = tmp_path / "nope.py"
    outside.write_text("print('no')\n", encoding="utf-8")
    try:
        resolve_script_path(p, "nope.py")
    except ValueError as exc:
        assert "local runs directory" in str(exc)
    else:
        raise AssertionError("Expected ValueError for script outside .wayfinder_runs")


def test_script_job_builds_worker_cmd(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    runs_dir = tmp_path / ".wayfinder_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    script = runs_dir / "hello.py"
    script.write_text("print('hi')\n", encoding="utf-8")

    daemon = RunnerDaemon(paths=p)
    cmd = daemon._build_worker_cmd(
        job={
            "type": "script",
            "payload": {
                "script_path": ".wayfinder_runs/hello.py",
                "args": ["--x", "1"],
            },
        }
    )
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("hello.py")
    assert cmd[-2:] == ["--x", "1"]


def test_daemon_adds_script_job_with_relative_path(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    runs_dir = tmp_path / ".wayfinder_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    script = runs_dir / "hello.py"
    script.write_text("print('hi')\n", encoding="utf-8")

    daemon = RunnerDaemon(paths=p)
    resp = daemon.ctl_add_job(
        name="script-job",
        job_type="script",
        payload={"script_path": str(script), "args": []},
        interval_seconds=60,
    )
    assert resp["ok"] is True

    db = RunnerDB(p.db_path)
    jobs = db.list_jobs()
    job = next(j for j in jobs if j["name"] == "script-job")
    stored = str(job["payload"]["script_path"])
    assert stored.endswith("hello.py")
    assert not Path(stored).is_absolute()


def test_daemon_add_job_uses_runtime_session_without_opencode_scan(
    tmp_path: Path, monkeypatch
) -> None:
    p = _paths(tmp_path)
    runs_dir = tmp_path / ".wayfinder_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    script = runs_dir / "hello.py"
    script.write_text("print('hi')\n", encoding="utf-8")

    daemon = RunnerDaemon(paths=p)
    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_env")
    monkeypatch.setattr(
        OPENCODE_CLIENT,
        "find_runner_session",
        lambda: (_ for _ in ()).throw(AssertionError("should not scan")),
    )
    monkeypatch.setattr(daemon, "_sync_to_backend_async", lambda: None)

    resp = daemon.ctl_add_job(
        name="script-job",
        job_type="script",
        payload={"script_path": str(script), "args": []},
        interval_seconds=60,
    )

    assert resp["ok"] is True
    job, _ = daemon._db.get_job(name="script-job")
    assert job.payload["notify_session_id"] == "ses_env"


def test_daemon_add_job_defers_session_scan_when_session_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    p = _paths(tmp_path)
    runs_dir = tmp_path / ".wayfinder_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    script = runs_dir / "hello.py"
    script.write_text("print('hi')\n", encoding="utf-8")

    daemon = RunnerDaemon(paths=p)
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    monkeypatch.delenv("OPENCODE_SESSIONID", raising=False)
    monkeypatch.setattr(daemon, "_sync_to_backend_async", lambda: None)
    bound: list[str] = []
    monkeypatch.setattr(daemon, "_bind_runner_session_async", bound.append)

    resp = daemon.ctl_add_job(
        name="script-job",
        job_type="script",
        payload={"script_path": str(script), "args": []},
        interval_seconds=60,
    )

    assert resp["ok"] is True
    assert bound == ["script-job"]


def test_notify_session_skips_routine_success(tmp_path: Path, monkeypatch) -> None:
    p = _paths(tmp_path)
    log = p.logs_dir / "job.log"
    p.logs_dir.mkdir(parents=True, exist_ok=True)
    log.write_text("ok\n", encoding="utf-8")

    daemon = RunnerDaemon(paths=p)
    daemon._db.add_job(
        name="quiet-job",
        job_type=JOB_TYPE_SCRIPT,
        payload={
            "script_path": ".wayfinder_runs/hello.py",
            "notify_session_id": "ses_1",
        },
        interval_seconds=60,
    )
    job, _ = daemon._db.get_job(name="quiet-job")
    calls: list[str] = []
    monkeypatch.setattr(OPENCODE_CLIENT, "healthy", lambda: True)
    monkeypatch.setattr(
        OPENCODE_CLIENT,
        "send_message",
        lambda _session_id, message: calls.append(message),
    )

    daemon._notify_session(
        RunningProcess(
            run_id=1,
            job_id=job.id,
            job_name="quiet-job",
            started_at=0,
            timeout_seconds=None,
            popen=object(),  # type: ignore[arg-type]
            log_path=log,
        ),
        status=RunStatus.OK,
        error_text=None,
    )

    assert calls == []


def test_notify_session_posts_failures(tmp_path: Path, monkeypatch) -> None:
    p = _paths(tmp_path)
    log = p.logs_dir / "job.log"
    p.logs_dir.mkdir(parents=True, exist_ok=True)
    log.write_text("bad\n", encoding="utf-8")

    daemon = RunnerDaemon(paths=p)
    daemon._db.add_job(
        name="loud-job",
        job_type=JOB_TYPE_SCRIPT,
        payload={
            "script_path": ".wayfinder_runs/hello.py",
            "notify_session_id": "ses_1",
        },
        interval_seconds=60,
    )
    job, _ = daemon._db.get_job(name="loud-job")
    calls: list[str] = []
    monkeypatch.setattr(OPENCODE_CLIENT, "healthy", lambda: True)
    monkeypatch.setattr(
        OPENCODE_CLIENT,
        "send_message",
        lambda _session_id, message: calls.append(message),
    )

    daemon._notify_session(
        RunningProcess(
            run_id=1,
            job_id=job.id,
            job_name="loud-job",
            started_at=0,
            timeout_seconds=None,
            popen=object(),  # type: ignore[arg-type]
            log_path=log,
        ),
        status=RunStatus.FAILED,
        error_text="failed",
    )

    assert len(calls) == 1
    assert '"type": "job_result"' in calls[0]
    assert '"status": "FAILED"' in calls[0]


def test_notify_session_posts_success_with_job_result_marker(
    tmp_path: Path, monkeypatch
) -> None:
    p = _paths(tmp_path)
    log = p.logs_dir / "job.log"
    p.logs_dir.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "\n".join(
            [
                "routine check ok",
                'WAYFINDER_JOB_RESULT {"summary":"Funding crossover detected","instructions":"Research whether to unroll the position.","severity":"warning"}',
            ]
        ),
        encoding="utf-8",
    )

    daemon = RunnerDaemon(paths=p)
    daemon._db.add_job(
        name="event-job",
        job_type=JOB_TYPE_SCRIPT,
        payload={
            "script_path": ".wayfinder_runs/hello.py",
            "notify_session_id": "ses_1",
        },
        interval_seconds=60,
    )
    job, _ = daemon._db.get_job(name="event-job")
    calls: list[str] = []
    monkeypatch.setattr(OPENCODE_CLIENT, "healthy", lambda: True)
    monkeypatch.setattr(
        OPENCODE_CLIENT,
        "send_message",
        lambda _session_id, message: calls.append(message),
    )

    daemon._notify_session(
        RunningProcess(
            run_id=1,
            job_id=job.id,
            job_name="event-job",
            started_at=0,
            timeout_seconds=None,
            popen=object(),  # type: ignore[arg-type]
            log_path=log,
        ),
        status=RunStatus.OK,
        error_text=None,
    )

    assert len(calls) == 1
    assert '"type": "job_result"' in calls[0]
    assert '"status": "OK"' in calls[0]
    assert '"message": "Funding crossover detected"' in calls[0]
    assert "Research whether to unroll the position." in calls[0]


def test_job_timeout_zero_disables_timeout(tmp_path: Path, monkeypatch) -> None:
    p = _paths(tmp_path)
    runs_dir = tmp_path / ".wayfinder_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    script = runs_dir / "hello.py"
    script.write_text("print('hi')\n", encoding="utf-8")

    daemon = RunnerDaemon(paths=p)
    daemon._db.add_job(
        name="no-timeout-job",
        job_type=JOB_TYPE_SCRIPT,
        payload={"script_path": ".wayfinder_runs/hello.py", "timeout_seconds": 0},
        interval_seconds=60,
    )

    popen = Mock()
    popen.pid = 12345
    monkeypatch.setattr("subprocess.Popen", lambda *_args, **_kwargs: popen)

    job, _ = daemon._db.get_job(name="no-timeout-job")
    run_id = daemon._maybe_start_job(
        job={
            "id": job.id,
            "name": job.name,
            "type": job.type,
            "payload": job.payload,
            "interval_seconds": job.interval_seconds,
        },
        now=1,
        reason="test",
    )

    assert run_id is not None
    assert daemon._running[int(run_id)].timeout_seconds is None


def test_stop_job_kills_running_worker(tmp_path: Path, monkeypatch) -> None:
    p = _paths(tmp_path)
    daemon = RunnerDaemon(paths=p)
    daemon._db.add_job(
        name="running-job",
        job_type=JOB_TYPE_SCRIPT,
        payload={"script_path": ".wayfinder_runs/hello.py"},
        interval_seconds=60,
    )
    job, _ = daemon._db.get_job(name="running-job")
    popen = Mock()
    popen.pid = 12345
    log = p.logs_dir / "job.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    daemon._running[7] = RunningProcess(
        run_id=7,
        job_id=job.id,
        job_name="running-job",
        started_at=0,
        timeout_seconds=None,
        popen=popen,
        log_path=log,
    )

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "wayfinder_paths.runner.daemon._kill_process_group",
        lambda pid, *, sig: killed.append((pid, sig)),
    )

    resp = daemon.ctl_stop_job(name="running-job", sig="INT")

    assert resp["ok"] is True
    assert resp["result"]["killed"] == [{"run_id": 7, "pid": 12345}]
    assert killed == [(12345, 2)]
