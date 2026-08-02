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
