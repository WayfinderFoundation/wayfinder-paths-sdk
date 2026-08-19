from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from wayfinder_paths.runner.client import RunnerControlClient
from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT, JobStatus, RunStatus
from wayfinder_paths.runner.daemon import RunnerDaemon
from wayfinder_paths.runner.db import RunnerDB
from wayfinder_paths.runner.paths import RunnerPaths, find_repo_root


def _short_runner_dir(prefix: str) -> Path:
    base = Path("/tmp") if Path("/tmp").exists() else Path(tempfile.gettempdir())
    short_prefix = "".join(ch for ch in prefix if ch.isalnum() or ch in ("-", "_"))[:12]
    return base / f"{short_prefix}-{time.time_ns()}"


def _wait_for_status(client: RunnerControlClient, *, timeout_s: float = 10.0) -> dict:
    deadline = time.time() + float(timeout_s)
    last = None
    while time.time() < deadline:
        resp = client.call("status")
        if resp.get("ok"):
            return resp
        last = resp
        time.sleep(0.1)
    raise AssertionError(f"Runner did not become ready in time (last={last})")


def _wait_for_job_run_id(
    client: RunnerControlClient, *, name: str, timeout_s: float = 10.0
) -> int:
    deadline = time.time() + float(timeout_s)
    last = None
    while time.time() < deadline:
        resp = client.call("job_runs", {"name": str(name), "limit": 1})
        if resp.get("ok") and resp.get("result", {}).get("runs"):
            return int(resp["result"]["runs"][0]["run_id"])
        last = resp
        time.sleep(0.05)
    raise AssertionError(f"Run was not created in time (last={last})")


def _wait_for_run_finished(
    client: RunnerControlClient, *, run_id: int, timeout_s: float = 10.0
) -> dict:
    deadline = time.time() + float(timeout_s)
    last_report = None
    while time.time() < deadline:
        report = client.call("run_report", {"run_id": int(run_id), "tail_bytes": 5000})
        assert report.get("ok") is True, report
        last_report = report
        status = report["result"]["run"]["status"]
        if status != "RUNNING":
            return report
        time.sleep(0.05)
    raise AssertionError(f"Run did not finish in time (last={last_report})")


def test_runner_daemon_runs_script_job_end_to_end(tmp_path: Path) -> None:
    runner_dir = _short_runner_dir("wayfinder-runner-e2e")
    if runner_dir.exists():
        shutil.rmtree(runner_dir, ignore_errors=True)
    runner_dir.mkdir(parents=True, exist_ok=True)

    runs_dir = tmp_path / ".wayfinder_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    script = runs_dir / "hello.py"
    script.write_text("print('HELLO_FROM_SCRIPT')\n", encoding="utf-8")

    paths = RunnerPaths(
        repo_root=tmp_path,
        runner_dir=runner_dir,
        db_path=runner_dir / "state.db",
        logs_dir=runner_dir / "logs",
        sock_path=runner_dir / "runner.sock",
    )

    daemon = RunnerDaemon(paths=paths, tick_seconds=0.05, max_workers=2)
    t = threading.Thread(target=daemon.start, name="runner-e2e-daemon")
    t.start()

    client = RunnerControlClient(sock_path=paths.sock_path)
    try:
        _wait_for_status(client, timeout_s=10.0)

        add = client.call(
            "add_job",
            {
                "name": "hello",
                "type": "script",
                "payload": {"script_path": ".wayfinder_runs/hello.py", "args": []},
                "interval_seconds": 3600,
            },
        )
        assert add.get("ok") is True, add

        run_id = _wait_for_job_run_id(client, name="hello", timeout_s=5.0)
        report = _wait_for_run_finished(client, run_id=run_id, timeout_s=10.0)
        assert report["result"]["run"]["status"] == RunStatus.OK
        tail = report["result"]["log_tail"]
        assert isinstance(tail, str) and "HELLO_FROM_SCRIPT" in tail
    finally:
        try:
            client.call("shutdown")
        except Exception:  # noqa: BLE001
            daemon.stop()
        t.join(timeout=5)
        assert not t.is_alive()
        shutil.rmtree(runner_dir, ignore_errors=True)


def test_runner_daemon_run_once_executes_job_when_not_due(tmp_path: Path) -> None:
    runner_dir = _short_runner_dir("wayfinder-runner-once")
    if runner_dir.exists():
        shutil.rmtree(runner_dir, ignore_errors=True)
    runner_dir.mkdir(parents=True, exist_ok=True)

    runs_dir = tmp_path / ".wayfinder_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    script = runs_dir / "hello.py"
    script.write_text("print('HELLO_FROM_RUN_ONCE')\n", encoding="utf-8")

    paths = RunnerPaths(
        repo_root=tmp_path,
        runner_dir=runner_dir,
        db_path=runner_dir / "state.db",
        logs_dir=runner_dir / "logs",
        sock_path=runner_dir / "runner.sock",
    )

    daemon = RunnerDaemon(paths=paths, tick_seconds=0.05, max_workers=2)
    t = threading.Thread(target=daemon.start, name="runner-e2e-daemon-once")
    t.start()

    client = RunnerControlClient(sock_path=paths.sock_path)
    try:
        _wait_for_status(client, timeout_s=10.0)

        # Insert a job directly with a future next_run_at so the scheduler does not
        # start it before we call run_once.
        db = RunnerDB(paths.db_path)
        db.add_job(
            name="hello",
            job_type=JOB_TYPE_SCRIPT,
            payload={"script_path": ".wayfinder_runs/hello.py", "args": []},
            interval_seconds=3600,
            status=JobStatus.ACTIVE,
            next_run_at=int(time.time()) + 3600,
        )
        db._conn.close()

        once = client.call("run_once", {"name": "hello"})
        assert once.get("ok") is True, once
        run_id = int(once["result"]["run_id"])

        report = _wait_for_run_finished(client, run_id=run_id, timeout_s=10.0)
        assert report["result"]["run"]["status"] == RunStatus.OK
        tail = report["result"]["log_tail"]
        assert isinstance(tail, str) and "HELLO_FROM_RUN_ONCE" in tail
    finally:
        try:
            client.call("shutdown")
        except Exception:  # noqa: BLE001
            daemon.stop()
        t.join(timeout=5)
        assert not t.is_alive()
        shutil.rmtree(runner_dir, ignore_errors=True)


# Executed INSIDE the forked warm child (via _load_strategy): registers an
# offline replay venue at import time — module import happens before
# build_adapter in the driver — so a real run_scheduled_tick completes
# end-to-end with no network.
_WARM_STRATEGY_SRC = """
from __future__ import annotations

import pandas as pd

from wayfinder_paths.jobs.execution.preflight import (
    ReplayAdapter,
    ReplayBroker,
    ReplayFeed,
)
from wayfinder_paths.jobs.execution.venues import register_venue


def _bars() -> list[dict]:
    end = pd.Timestamp.now(tz="UTC").floor("1h")
    return [
        {
            "timestamp": (end - pd.Timedelta(hours=5 - i)).isoformat(),
            "symbol": "TEST",
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.0,
        }
        for i in range(6)
    ]


def _build_replay_adapter(*, mode, spec, params):
    feed = ReplayFeed(_bars())
    feed.cursor = 100  # serve the full dataset
    return ReplayAdapter(feed=feed, broker=ReplayBroker())


register_venue("warm_replay", _build_replay_adapter)


def decide(ctx):
    return None
"""


def test_runner_daemon_runs_warm_forked_jobs_v1_tick_end_to_end(
    tmp_path: Path,
) -> None:
    """A jobs_v1 script tick routed through the forkserver warm path: the REAL
    run_scheduled_tick executes in the forked child, its JSON payload lands in
    the run log, the exit code is plumbed through WarmChild, and the daemon
    reaps the run as OK. The registered script wrapper is a decoy that prints
    a marker — if the daemon had fallen back to the cold Popen path, the
    marker (and not the driver payload) would appear in the log."""
    import json

    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.store import JobStore

    runner_dir = _short_runner_dir("wayfinder-runner-warm")
    if runner_dir.exists():
        shutil.rmtree(runner_dir, ignore_errors=True)
    runner_dir.mkdir(parents=True, exist_ok=True)

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "warm-e2e",
        script="workspace/src/strategy.py",
        interval_seconds=3600,
        execution_contract="jobs_v1",
    )
    store.create_job(job)
    root = store.job_dir(job.id)
    (root / "workspace" / "src").mkdir(parents=True, exist_ok=True)
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "workspace" / "src" / "strategy.py").write_text(
        _WARM_STRATEGY_SRC, encoding="utf-8"
    )
    (root / "execution_spec.json").write_text(
        json.dumps(
            {
                "data_contract": {
                    "bar_interval": "1h",
                    "symbols": ["TEST"],
                    "market_kind": "swap",
                },
                "venues": ["warm_replay"],
            }
        ),
        encoding="utf-8",
    )

    runs_dir = tmp_path / ".wayfinder_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "wrapper.py").write_text(
        "print('POPEN_FALLBACK_MARKER')\n", encoding="utf-8"
    )

    paths = RunnerPaths(
        repo_root=tmp_path,
        runner_dir=runner_dir,
        db_path=runner_dir / "state.db",
        logs_dir=runner_dir / "logs",
        sock_path=runner_dir / "runner.sock",
    )

    daemon = RunnerDaemon(paths=paths, tick_seconds=0.05, max_workers=2)
    t = threading.Thread(target=daemon.start, name="runner-e2e-daemon-warm")
    t.start()

    client = RunnerControlClient(sock_path=paths.sock_path)
    try:
        _wait_for_status(client, timeout_s=10.0)

        add = client.call(
            "add_job",
            {
                "name": "warm-e2e-script",
                "type": "script",
                "payload": {
                    "script_path": ".wayfinder_runs/wrapper.py",
                    "env": {
                        "WAYFINDER_JOB_EXECUTION_CONTRACT": "jobs_v1",
                        "WAYFINDER_JOB_DIR": str(root),
                        "WAYFINDER_JOB_MODE": "paper",
                    },
                },
                "interval_seconds": 3600,
            },
        )
        assert add.get("ok") is True, add

        # Generous timeouts: the first warm spawn starts the forkserver and
        # pays the preload (pandas + jobs execution stack) once.
        run_id = _wait_for_job_run_id(client, name="warm-e2e-script", timeout_s=90.0)
        report = _wait_for_run_finished(client, run_id=run_id, timeout_s=120.0)
        assert report["result"]["run"]["status"] == RunStatus.OK, report
        assert report["result"]["run"]["exit_code"] == 0
        tail = report["result"]["log_tail"]
        assert isinstance(tail, str)
        assert "POPEN_FALLBACK_MARKER" not in tail  # warm path, not Popen
        assert '"ok": true' in tail  # the real driver payload
        assert '"job_id": "warm-e2e"' in tail
        # The forked tick really ran the driver: forward artifacts were
        # written into the job dir by the child.
        assert (root / "results" / "forward" / "ticks.jsonl").exists()
    finally:
        try:
            client.call("shutdown")
        except Exception:  # noqa: BLE001
            daemon.stop()
        t.join(timeout=10)
        assert not t.is_alive()
        shutil.rmtree(runner_dir, ignore_errors=True)


def test_detached_daemon_survives_parent_process_group_termination() -> None:
    if os.name == "nt":
        pytest.skip("POSIX-only (relies on process groups via killpg)")

    runner_dir = _short_runner_dir("wayfinder-runner-detach")
    if runner_dir.exists():
        shutil.rmtree(runner_dir, ignore_errors=True)
    runner_dir.mkdir(parents=True, exist_ok=True)

    repo_root = find_repo_root(start=Path(__file__).resolve())
    env = os.environ.copy()
    env["WAYFINDER_RUNNER_DIR"] = str(runner_dir)

    wrapper_code = "\n".join(
        [
            "import os, subprocess, sys, time",
            "subprocess.Popen([sys.executable, '-m', 'wayfinder_paths.runnerd', 'start'], cwd=os.getcwd(), env=os.environ.copy())",
            "time.sleep(60)",
        ]
    )

    wrapper = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", wrapper_code],
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    client = RunnerControlClient(sock_path=runner_dir / "runner.sock")
    daemon_pid = None
    try:
        status = _wait_for_status(client, timeout_s=10.0)
        daemon_pid = int(status["result"]["pid"])

        os.killpg(wrapper.pid, signal.SIGTERM)
        try:
            wrapper.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(wrapper.pid, signal.SIGKILL)
            wrapper.wait(timeout=5)

        # The daemon should still be alive (it was started detached into a new session).
        after = _wait_for_status(client, timeout_s=5.0)
        assert int(after["result"]["pid"]) == daemon_pid

        # Extra sanity: ensure we can still stop it cleanly.
        resp = client.call("shutdown")
        assert resp.get("ok") is True, resp
    finally:
        if wrapper.poll() is None:
            try:
                os.killpg(wrapper.pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass

        if daemon_pid is not None:
            try:
                client.call("shutdown")
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(runner_dir, ignore_errors=True)
