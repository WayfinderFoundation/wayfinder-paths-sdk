from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import Mock

from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT
from wayfinder_paths.runner.daemon import (
    BURST_MAX_POSTPONE_S,
    BURST_SHORT_POSTPONE_S,
    RunnerDaemon,
)
from wayfinder_paths.runner.paths import RunnerPaths


class _FakeBurst:
    def __init__(self, over: bool) -> None:
        self._over = over
        self.balance = 0.0

    def update(self) -> None:  # pragma: no cover - trivial
        pass

    def over_quota(self) -> bool:
        return self._over


def _daemon(tmp_path: Path, *, env: dict[str, str] | None = None) -> RunnerDaemon:
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
    payload: dict = {"script_path": ".wayfinder_runs/hello.py"}
    if env is not None:
        payload["env"] = env
    d.ctl_add_job(
        name="j",
        job_type=JOB_TYPE_SCRIPT,
        payload=payload,
        interval_seconds=60,
    )
    return d


def _force_burst(d: RunnerDaemon, *, over: bool) -> None:
    d._burst = _FakeBurst(over)  # type: ignore[assignment]


def _job(d: RunnerDaemon) -> dict:
    result = d._db.get_job(name="j")
    assert result is not None
    job, _ = result
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
    _force_burst(d, over=True)
    before = d._db.get_job(name="j")
    assert before is not None
    run_id = d._maybe_start_job(job=_job(d), now=1_000, reason="schedule")
    after = d._db.get_job(name="j")
    assert after is not None
    assert run_id is None  # postponed
    # Schedule NOT advanced → job stays due and retries next tick once quota recovers.
    assert after[1].next_run_at == before[1].next_run_at


def test_recovered_quota_launches(tmp_path: Path, monkeypatch) -> None:
    d = _daemon(tmp_path)
    _force_burst(d, over=False)
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
    _force_burst(d, over=True)
    job = _job(d)
    # Pretend it was first postponed longer ago than the starvation floor.
    d._postponed_since[job["id"]] = time.monotonic() - (BURST_MAX_POSTPONE_S + 1)
    popen = Mock()
    popen.pid = 123
    monkeypatch.setattr("subprocess.Popen", lambda *_a, **_k: popen)
    run_id = d._maybe_start_job(job=job, now=1_000, reason="schedule")
    assert run_id is not None  # forced past the floor despite over-quota


def _mock_popen(monkeypatch) -> None:
    popen = Mock()
    popen.pid = 123
    monkeypatch.setattr("subprocess.Popen", lambda *_a, **_k: popen)


def test_live_jobs_v1_tick_exempt_from_postpone(tmp_path: Path, monkeypatch) -> None:
    d = _daemon(
        tmp_path,
        env={
            "WAYFINDER_JOB_EXECUTION_CONTRACT": "jobs_v1",
            "WAYFINDER_JOB_MODE": "live",
        },
    )
    _force_burst(d, over=True)
    _mock_popen(monkeypatch)
    run_id = d._maybe_start_job(job=_job(d), now=1_000, reason="schedule")
    assert run_id is not None  # live trading tick launches despite over-quota


def test_paper_jobs_v1_tick_gets_short_floor(tmp_path: Path, monkeypatch) -> None:
    d = _daemon(
        tmp_path,
        env={
            "WAYFINDER_JOB_EXECUTION_CONTRACT": "jobs_v1",
            "WAYFINDER_JOB_MODE": "paper",
        },
    )
    _force_burst(d, over=True)
    job = _job(d)
    assert d._maybe_start_job(job=job, now=1_000, reason="schedule") is None
    # Just past the SHORT floor (still far under BURST_MAX_POSTPONE_S) → runs.
    d._postponed_since[job["id"]] = time.monotonic() - (BURST_SHORT_POSTPONE_S + 1)
    _mock_popen(monkeypatch)
    assert d._maybe_start_job(job=job, now=1_000, reason="schedule") is not None


def test_scheduled_agent_wake_runs_past_short_floor(
    tmp_path: Path, monkeypatch
) -> None:
    d = _daemon(
        tmp_path,
        env={
            "WAYFINDER_JOB_EXECUTION_CONTRACT": "jobs_v1",
            "WAYFINDER_JOB_MODE": "live",
            "WAYFINDER_JOB_AGENT_MODE": "review",
        },
    )
    _force_burst(d, over=True)
    job = _job(d)
    assert d._maybe_start_job(job=job, now=1_000, reason="schedule") is None
    d._postponed_since[job["id"]] = time.monotonic() - (BURST_SHORT_POSTPONE_S + 1)
    _mock_popen(monkeypatch)
    assert d._maybe_start_job(job=job, now=1_000, reason="schedule") is not None


def test_agent_wake_deferral_keeps_job_due(tmp_path: Path) -> None:
    """Deferring an agent wake must NOT advance the schedule — the job stays
    due and retries every tick until the floor admits it. The skip-outright
    variant advanced the schedule on every drained occurrence, starving wakes
    for as long as the drain lasted."""
    d = _daemon(tmp_path, env={"WAYFINDER_JOB_AGENT_MODE": "review"})
    _force_burst(d, over=True)
    before = d._db.get_job(name="j")
    assert before is not None
    assert d._maybe_start_job(job=_job(d), now=1_000, reason="schedule") is None
    after = d._db.get_job(name="j")
    assert after is not None
    assert after[1].next_run_at == before[1].next_run_at


def _job_root_with_campaign(tmp_path: Path, status: str) -> Path:
    job_root = tmp_path / "jobs" / "majors"
    (job_root / "state").mkdir(parents=True)
    (job_root / "state" / "evolution_campaign.json").write_text(
        json.dumps({"campaign_id": "campaign-1", "status": status}),
        encoding="utf-8",
    )
    return job_root


def test_active_evolution_campaign_exempts_agent_wake(
    tmp_path: Path, monkeypatch
) -> None:
    """A wake whose job has an ACTIVE evolution campaign launches immediately
    under drain — the campaign's evals are what drained the bucket, and only
    the wake relaunches them when they die."""
    job_root = _job_root_with_campaign(tmp_path, "active")
    d = _daemon(
        tmp_path,
        env={
            "WAYFINDER_JOB_EXECUTION_CONTRACT": "jobs_v1",
            "WAYFINDER_JOB_MODE": "live",
            "WAYFINDER_JOB_AGENT_MODE": "review",
            "WAYFINDER_JOB_DIR": str(job_root),
        },
    )
    _force_burst(d, over=True)
    _mock_popen(monkeypatch)
    assert d._maybe_start_job(job=_job(d), now=1_000, reason="schedule") is not None


def test_inactive_evolution_campaign_keeps_agent_wake_gated(
    tmp_path: Path, monkeypatch
) -> None:
    job_root = _job_root_with_campaign(tmp_path, "completed")
    d = _daemon(
        tmp_path,
        env={
            "WAYFINDER_JOB_EXECUTION_CONTRACT": "jobs_v1",
            "WAYFINDER_JOB_MODE": "live",
            "WAYFINDER_JOB_AGENT_MODE": "review",
            "WAYFINDER_JOB_DIR": str(job_root),
        },
    )
    _force_burst(d, over=True)
    _mock_popen(monkeypatch)
    assert d._maybe_start_job(job=_job(d), now=1_000, reason="schedule") is None


def test_indeterminate_mode_defaults_to_short_floor(
    tmp_path: Path, monkeypatch
) -> None:
    # jobs_v1 tick with no WAYFINDER_JOB_MODE baked → fail toward availability.
    d = _daemon(tmp_path, env={"WAYFINDER_JOB_EXECUTION_CONTRACT": "jobs_v1"})
    _force_burst(d, over=True)
    job = _job(d)
    assert d._maybe_start_job(job=job, now=1_000, reason="schedule") is None
    d._postponed_since[job["id"]] = time.monotonic() - (BURST_SHORT_POSTPONE_S + 1)
    _mock_popen(monkeypatch)
    assert d._maybe_start_job(job=job, now=1_000, reason="schedule") is not None


def test_heavy_job_keeps_full_floor(tmp_path: Path, monkeypatch) -> None:
    # No jobs_v1 env (legacy script / heavy op): short floor does NOT apply.
    d = _daemon(tmp_path)
    _force_burst(d, over=True)
    job = _job(d)
    d._postponed_since[job["id"]] = time.monotonic() - (BURST_SHORT_POSTPONE_S + 1)
    _mock_popen(monkeypatch)
    assert d._maybe_start_job(job=job, now=1_000, reason="schedule") is None
    d._postponed_since[job["id"]] = time.monotonic() - (BURST_MAX_POSTPONE_S + 1)
    assert d._maybe_start_job(job=job, now=1_000, reason="schedule") is not None


def test_application_watchdog_exempt(tmp_path: Path, monkeypatch) -> None:
    d = _daemon(tmp_path, env={"WAYFINDER_WATCHDOG": "1"})
    _force_burst(d, over=True)
    _mock_popen(monkeypatch)
    run_id = d._maybe_start_job(job=_job(d), now=1_000, reason="schedule")
    assert run_id is not None  # housekeeping watchdog un-sticks paused loops


def test_short_floor_env_tunable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WAYFINDER_BURST_SHORT_POSTPONE_S", "5")
    d = _daemon(
        tmp_path,
        env={
            "WAYFINDER_JOB_EXECUTION_CONTRACT": "jobs_v1",
            "WAYFINDER_JOB_MODE": "paper",
        },
    )
    _force_burst(d, over=True)
    job = _job(d)
    d._postponed_since[job["id"]] = time.monotonic() - 6
    _mock_popen(monkeypatch)
    assert d._maybe_start_job(job=job, now=1_000, reason="schedule") is not None
