from __future__ import annotations

import json
from pathlib import Path

from wayfinder_paths.jobs.compiler import JobCompiler
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


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
