from __future__ import annotations

from pathlib import Path

import pytest

from wayfinder_paths.core.clients.ScheduledJobsClient import SCHEDULED_JOBS_CLIENT
from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT, JobStatus
from wayfinder_paths.runner.daemon import RunnerDaemon
from wayfinder_paths.runner.paths import RunnerPaths


def _paths(tmp_path: Path) -> RunnerPaths:
    runner_dir = tmp_path / "runner"
    runner_dir.mkdir()
    (tmp_path / "pyproject.toml").write_text("[tool.x]\n")
    return RunnerPaths(
        repo_root=tmp_path,
        runner_dir=runner_dir,
        db_path=runner_dir / "state.db",
        logs_dir=runner_dir / "logs",
        sock_path=runner_dir / "runner.sock",
    )


def _add_local(daemon: RunnerDaemon, name: str) -> None:
    daemon._db.add_job(
        name=name,
        job_type=JOB_TYPE_SCRIPT,
        payload={"script_path": "x.py"},
        interval_seconds=60,
        status=JobStatus.ACTIVE,
        next_run_at=0,
    )


def test_reconcile_deletes_remote_only_and_syncs_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-xyz")

    daemon = RunnerDaemon(paths=_paths(tmp_path))
    _add_local(daemon, "local-a")
    _add_local(daemon, "local-b")

    deleted: list[str] = []
    synced: list[str] = []

    monkeypatch.setattr(
        SCHEDULED_JOBS_CLIENT,
        "list_jobs",
        lambda: [
            {"job_name": "local-a"},
            {"job_name": "orphan-1"},
            {"job_name": "orphan-2"},
        ],
    )
    monkeypatch.setattr(
        SCHEDULED_JOBS_CLIENT, "delete_job", lambda name: deleted.append(name)
    )
    monkeypatch.setattr(
        SCHEDULED_JOBS_CLIENT,
        "sync_job",
        lambda name, _data: synced.append(name),
    )

    daemon._reconcile_with_backend()

    assert set(deleted) == {"orphan-1", "orphan-2"}
    assert set(synced) == {"local-a", "local-b"}


def test_reconcile_noop_when_not_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENCODE_INSTANCE_ID", raising=False)

    daemon = RunnerDaemon(paths=_paths(tmp_path))
    _add_local(daemon, "local-a")

    called = {"list": 0, "delete": 0, "sync": 0}
    monkeypatch.setattr(
        SCHEDULED_JOBS_CLIENT,
        "list_jobs",
        lambda: (called.__setitem__("list", called["list"] + 1), [])[1],
    )
    monkeypatch.setattr(
        SCHEDULED_JOBS_CLIENT,
        "delete_job",
        lambda _name: called.__setitem__("delete", called["delete"] + 1),
    )
    monkeypatch.setattr(
        SCHEDULED_JOBS_CLIENT,
        "sync_job",
        lambda _n, _d: called.__setitem__("sync", called["sync"] + 1),
    )

    daemon._reconcile_with_backend()

    assert called == {"list": 0, "delete": 0, "sync": 0}


def test_reconcile_empty_remote_just_syncs_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-xyz")

    daemon = RunnerDaemon(paths=_paths(tmp_path))
    _add_local(daemon, "only-local")

    deleted: list[str] = []
    synced: list[str] = []
    monkeypatch.setattr(SCHEDULED_JOBS_CLIENT, "list_jobs", lambda: [])
    monkeypatch.setattr(
        SCHEDULED_JOBS_CLIENT, "delete_job", lambda name: deleted.append(name)
    )
    monkeypatch.setattr(
        SCHEDULED_JOBS_CLIENT,
        "sync_job",
        lambda name, _data: synced.append(name),
    )

    daemon._reconcile_with_backend()

    assert deleted == []
    assert synced == ["only-local"]
