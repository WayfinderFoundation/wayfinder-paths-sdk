from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import Mock

from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT
from wayfinder_paths.runner.daemon import BURST_MAX_POSTPONE_S, RunnerDaemon
from wayfinder_paths.runner.paths import RunnerPaths


class _FakeBurst:
    def __init__(self, over: bool) -> None:
        self._over = over
        self.balance = 0.0

    def update(self) -> None:  # pragma: no cover - trivial
        pass

    def over_quota(self) -> bool:
        return self._over


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


def _job(d: RunnerDaemon) -> dict:
    job, _ = d._db.get_job(name="j")
    return {
        "id": job.id,
        "name": job.name,
        "type": job.type,
        "payload": job.payload,
        "interval_seconds": job.interval_seconds,
        "schedule_kind": job.schedule_kind,
        "cron_expr": job.cron_expr,
        "timezone": job.timezone,
        "next_run_at": 1_000,
    }


def test_over_quota_postpones_without_reserving(tmp_path: Path) -> None:
    d = _daemon(tmp_path)
    d._burst = _FakeBurst(over=True)
    _, before = d._db.get_job(name="j")
    run_id = d._maybe_start_job(job=_job(d), now=1_000, reason="schedule")
    _, after = d._db.get_job(name="j")
    assert run_id is None  # postponed
    # Schedule NOT advanced → job stays due and retries next tick once quota recovers.
    assert after.next_run_at == before.next_run_at


def test_recovered_quota_launches(tmp_path: Path, monkeypatch) -> None:
    d = _daemon(tmp_path)
    d._burst = _FakeBurst(over=False)
    popen = Mock()
    popen.pid = 123
    monkeypatch.setattr("subprocess.Popen", lambda *_a, **_k: popen)
    run_id = d._maybe_start_job(job=_job(d), now=1_000, reason="schedule")
    assert run_id is not None


def test_burst_gate_only_on_opencode_instance(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "wayfinder_paths.runner.daemon.is_opencode_instance", lambda: False
    )
    assert _daemon(tmp_path)._burst is None  # ungated off a hosted instance
    monkeypatch.setattr(
        "wayfinder_paths.runner.daemon.is_opencode_instance", lambda: True
    )
    assert _daemon(tmp_path)._burst is not None


def test_floor_force_runs_after_max_postpone(tmp_path: Path, monkeypatch) -> None:
    d = _daemon(tmp_path)
    d._burst = _FakeBurst(over=True)
    job = _job(d)
    # Pretend it was first postponed longer ago than the starvation floor.
    d._postponed_since[job["id"]] = time.monotonic() - (BURST_MAX_POSTPONE_S + 1)
    popen = Mock()
    popen.pid = 123
    monkeypatch.setattr("subprocess.Popen", lambda *_a, **_k: popen)
    run_id = d._maybe_start_job(job=job, now=1_000, reason="schedule")
    assert run_id is not None  # forced past the floor despite over-quota
