from __future__ import annotations

import json
from pathlib import Path

from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.runner_bridge import RunnerBridge
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import sync_all_jobs
from wayfinder_paths.jobs.worker import run_job_worker


def test_job_store_creates_versioned_bundle(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "SNX IMX Re-arm",
        name="SNX / IMX Re-arm",
        goal="Trade only after both legs re-arm.",
        script=".wayfinder/jobs/snx-imx-re-arm/workspace/src/fast_loop.py",
        interval_seconds=300,
        agent_mode="monitor",
        agent_wake_seconds=3600,
    )

    path = store.save(job)
    loaded = store.load("snx-imx-re-arm")

    assert path == tmp_path / ".wayfinder/jobs/snx-imx-re-arm/job.yaml"
    assert loaded.id == "snx-imx-re-arm"
    assert loaded.script_loop.enabled is True
    assert loaded.agent_loop.mode == "monitor"
    assert (tmp_path / ".wayfinder/jobs/snx-imx-re-arm/memory.md").exists()
    assert (tmp_path / ".wayfinder/jobs/snx-imx-re-arm/scorecard.json").exists()


def test_job_compiler_writes_runner_wrappers(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []

    class FakeBridge:
        def __init__(self, *, repo_root=None):  # noqa: ANN001
            self.repo_root = repo_root

        def ensure_started(self):
            return {"ok": True}

        def add_or_update_script_job(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "result": {"name": kwargs["name"]}}

    monkeypatch.setattr("wayfinder_paths.jobs.compiler.RunnerBridge", FakeBridge)
    store = JobStore(repo_root=tmp_path)
    script = tmp_path / ".wayfinder/jobs/example/workspace/src/fast_loop.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')\n", encoding="utf-8")
    job = WayfinderJob.new(
        "example",
        script=str(script),
        interval_seconds=60,
        agent_mode="monitor",
        agent_wake_seconds=300,
    )
    store.save(job)

    result = JobCompiler(store=store).compile(job, start_daemon=False)

    assert len(calls) == 2
    assert calls[0]["name"] == "example-script"
    assert calls[0]["script_path"] == ".wayfinder_runs/jobs/example_script.py"
    assert calls[1]["name"] == "example-agent"
    assert calls[1]["script_path"] == ".wayfinder_runs/jobs/example_agent.py"
    assert (tmp_path / ".wayfinder_runs/jobs/example_script.py").exists()
    assert (tmp_path / ".wayfinder_runs/jobs/example_agent.py").exists()
    links = json.loads(
        (tmp_path / ".wayfinder/jobs/example/runner_links.json").read_text(
            encoding="utf-8"
        )
    )
    assert links == result


def test_legacy_agent_modes_normalize() -> None:
    improve_job = WayfinderJob.from_dict(
        {
            "id": "legacy-improve",
            "name": "Legacy Improve",
            "script_loop": {"enabled": True},
            "agent_loop": {"enabled": True, "mode": "improve"},
        }
    )
    decide_job = WayfinderJob.from_dict(
        {
            "id": "legacy-decide",
            "name": "Legacy Decide",
            "script_loop": {"enabled": False},
            "agent_loop": {"enabled": True, "mode": "decide"},
        }
    )

    assert improve_job.agent_loop.mode == "intervene"
    assert improve_job.job_kind == "script_agent"
    assert decide_job.agent_loop.mode == "auto"
    assert decide_job.job_kind == "agent_only"


def test_auto_agent_job_can_run_without_script() -> None:
    job = WayfinderJob.new(
        "auto-demo",
        agent_mode="auto",
        auto_limits={
            "enabled_venues": ["hyperliquid"],
            "allowed_symbols": ["BTC"],
            "max_notional_per_decision": 25,
            "max_daily_notional": 100,
            "max_open_positions": 1,
            "max_open_orders": 2,
        },
    )

    assert job.job_kind == "agent_only"
    assert job.script_loop.enabled is False
    assert job.agent_loop.enabled is True
    assert job.agent_loop.mode == "auto"
    assert job.agent_loop.wake_interval_seconds == 900
    assert job.agent_loop.auto_limits["allowed_symbols"] == ["BTC"]


def test_auto_worker_blocks_missing_limits(tmp_path: Path, monkeypatch) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("auto-demo", agent_mode="auto")
    store.save(job)
    monkeypatch.setattr("wayfinder_paths.jobs.worker.JobStore", lambda: store)

    report = run_job_worker("auto-demo", mode="auto")

    assert report["status"] == "red"
    assert report["queued"] is False
    assert "enabled_venues" in str(report["error"])
    latest = json.loads(
        (tmp_path / ".wayfinder/jobs/auto-demo/reports/auto/latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["summary"].startswith("Auto agent blocked")


def test_runner_bridge_starts_daemon_with_defaults(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_ensure_daemon_started(**kwargs):
        captured.update(kwargs)
        return True, {"status": "ok"}

    monkeypatch.setattr(
        "wayfinder_paths.jobs.runner_bridge.ensure_daemon_started",
        fake_ensure_daemon_started,
    )

    result = RunnerBridge(repo_root=tmp_path).ensure_started()

    assert result["ok"] is True
    assert captured["paths"].repo_root == tmp_path.resolve()
    assert captured["tick_seconds"] == 1.0
    assert captured["max_workers"] == 4
    assert captured["max_failures"] == 5
    assert captured["default_timeout_seconds"] == 20 * 60
    assert captured["log_level"] == "INFO"


def test_sync_all_jobs_noops_outside_opencode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENCODE_INSTANCE_ID", raising=False)
    store = JobStore(repo_root=tmp_path)
    store.save(WayfinderJob.new("local-script", script="workspace/src/loop.py"))

    sync_all_jobs(store=store)
