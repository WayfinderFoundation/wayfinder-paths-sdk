"""Machine-wide heavy-compute lock: cross-process exclusivity, in-process
reentrancy, clear busy errors, and heavy entrypoints actually holding it."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from wayfinder_paths.jobs.compute_lock import (
    ComputeLockBusy,
    heavy_compute_lock,
)


def test_reentrant_within_process(tmp_path) -> None:
    with heavy_compute_lock(repo_root=tmp_path, label="outer"):
        with heavy_compute_lock(repo_root=tmp_path, label="inner"):
            pass  # no self-deadlock


def test_cross_process_exclusivity_and_busy_error(tmp_path) -> None:
    import os

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import time
                from wayfinder_paths.jobs.compute_lock import heavy_compute_lock
                with heavy_compute_lock(repo_root={str(tmp_path)!r}, label="holder"):
                    print("HELD", flush=True)
                    time.sleep(8)
                """
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
        cwd=os.getcwd(),
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "HELD"
        with pytest.raises(ComputeLockBusy, match="held by: pid="):
            with heavy_compute_lock(
                repo_root=tmp_path, label="contender", timeout_s=1.0
            ):
                pass
    finally:
        holder.kill()
        holder.wait()
    # After the holder dies, the lock is acquirable again.
    with heavy_compute_lock(repo_root=tmp_path, label="after"):
        pass


def test_backtest_entrypoint_holds_lock(tmp_path, monkeypatch) -> None:
    """backtest_execution_job must hold the lock while simulating."""
    import wayfinder_paths.jobs.execution.job as job_mod
    from wayfinder_paths.jobs import compute_lock

    seen = {}

    def fake_locked(job_id, **kwargs):
        seen["held"] = getattr(compute_lock._local, "held", False)
        return {"ok": True}

    monkeypatch.setattr(job_mod, "_backtest_execution_job_locked", fake_locked)

    class FakeStore:
        repo_root = tmp_path

    result = job_mod.backtest_execution_job("demo", store=FakeStore())
    assert result == {"ok": True}
    assert seen["held"] is True
    assert not getattr(compute_lock._local, "held", False)  # released


def test_wake_monitors_skip_fast_when_lock_held(tmp_path, monkeypatch) -> None:
    """Replication/counterfactual run in runner workers — when the lock is
    held elsewhere they must give up quickly (journaled skip), never hold a
    worker for the full default wait."""
    import subprocess
    import textwrap
    import time as _time

    from wayfinder_paths.jobs import counterfactual as cf
    from wayfinder_paths.jobs import replication as rep
    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("locked-demo", agent_mode="intervene")
    store.save(job)

    monkeypatch.setattr(rep, "_LOCK_TIMEOUT_S", 1.0)
    monkeypatch.setattr(cf, "_LOCK_TIMEOUT_S", 1.0)

    import os

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import time
                from wayfinder_paths.jobs.compute_lock import heavy_compute_lock
                with heavy_compute_lock(repo_root={str(tmp_path)!r}, label="grid"):
                    print("HELD", flush=True)
                    time.sleep(15)
                """
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
        cwd=os.getcwd(),
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "HELD"

        t0 = _time.monotonic()
        doc = rep.replication_job(job.id, store=store)
        elapsed = _time.monotonic() - t0
        assert doc["available"] is False
        assert "lock busy" in doc["reason"]
        assert elapsed < 8  # gave up fast, did not hold a worker
        journal = (store.job_dir(job.id) / "journal.jsonl").read_text()
        assert "replication_failed" in journal
    finally:
        holder.kill()
        holder.wait()
