from __future__ import annotations

import sys
from pathlib import Path

from wayfinder_paths.runner.daemon import RunnerDaemon
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
