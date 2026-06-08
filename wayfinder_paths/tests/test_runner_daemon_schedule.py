from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT, RunStatus
from wayfinder_paths.runner.daemon import RunnerDaemon
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


def _write_script(tmp_path: Path) -> None:
    runs_dir = tmp_path / ".wayfinder_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "hello.py").write_text("print('hi')\n", encoding="utf-8")


def _job_dict(daemon: RunnerDaemon, name: str, *, next_run_at: int) -> dict:
    job, _ = daemon._db.get_job(name=name)
    return {
        "id": job.id,
        "name": job.name,
        "type": job.type,
        "payload": job.payload,
        "interval_seconds": job.interval_seconds,
        "schedule_kind": job.schedule_kind,
        "cron_expr": job.cron_expr,
        "timezone": job.timezone,
        "next_run_at": next_run_at,
    }


def test_cron_scheduled_run_advances_to_next_occurrence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_script(tmp_path)
    daemon = RunnerDaemon(paths=_paths(tmp_path))
    resp = daemon.ctl_add_job(
        name="cron-job",
        job_type=JOB_TYPE_SCRIPT,
        payload={"script_path": ".wayfinder_runs/hello.py"},
        cron_expr="* * * * *",
        timezone="UTC",
    )
    assert resp["ok"] is True
    job, _ = daemon._db.get_job(name="cron-job")
    daemon._db.set_next_run_at(job_id=job.id, next_run_at=1_704_067_200)

    popen = Mock()
    popen.pid = 123
    monkeypatch.setattr("subprocess.Popen", lambda *_args, **_kwargs: popen)

    run_id = daemon._maybe_start_job(
        job=_job_dict(daemon, "cron-job", next_run_at=1_704_067_200),
        now=1_704_067_200,
        reason="schedule",
    )
    _, state = daemon._db.get_job(name="cron-job")
    run = daemon._db.get_run(run_id=run_id)

    assert run_id is not None
    assert state.next_run_at == 1_704_067_260
    assert run is not None
    assert run["reason"] == "schedule"
    assert run["scheduled_for"] == 1_704_067_200


def test_run_once_preserves_next_run_at(tmp_path: Path, monkeypatch) -> None:
    _write_script(tmp_path)
    daemon = RunnerDaemon(paths=_paths(tmp_path))
    resp = daemon.ctl_add_job(
        name="interval-job",
        job_type=JOB_TYPE_SCRIPT,
        payload={"script_path": ".wayfinder_runs/hello.py"},
        interval_seconds=60,
    )
    assert resp["ok"] is True
    job, _ = daemon._db.get_job(name="interval-job")
    daemon._db.set_next_run_at(job_id=job.id, next_run_at=9_999)

    popen = Mock()
    popen.pid = 456
    monkeypatch.setattr("subprocess.Popen", lambda *_args, **_kwargs: popen)

    resp = daemon.ctl_run_once(name="interval-job")
    _, state = daemon._db.get_job(name="interval-job")
    run = daemon._db.get_run(run_id=resp["result"]["run_id"])

    assert resp["ok"] is True
    assert state.next_run_at == 9_999
    assert run is not None
    assert run["reason"] == "run_once"
    assert run["scheduled_for"] is None


def test_build_failure_is_recorded_after_reservation(tmp_path: Path) -> None:
    daemon = RunnerDaemon(paths=_paths(tmp_path))
    daemon._db.add_job(
        name="bad-job",
        job_type=JOB_TYPE_SCRIPT,
        payload={"script_path": ".wayfinder_runs/missing.py"},
        interval_seconds=60,
        next_run_at=100,
    )

    run_id = daemon._maybe_start_job(
        job=_job_dict(daemon, "bad-job", next_run_at=100),
        now=100,
        reason="schedule",
    )
    job, state = daemon._db.get_job(name="bad-job")
    runs = daemon._db.runs_for_job(job_id=job.id)

    assert run_id is None
    assert len(runs) == 1
    assert runs[0]["status"] == RunStatus.FAILED
    assert runs[0]["reason"] == "schedule"
    assert state.next_run_at == 160
