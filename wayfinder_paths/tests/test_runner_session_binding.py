from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from wayfinder_paths.core.clients.OpenCodeClient import OPENCODE_CLIENT
from wayfinder_paths.runner import daemon as daemon_module
from wayfinder_paths.runner.client import RunnerControlClient
from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT, RunStatus
from wayfinder_paths.runner.daemon import RunnerDaemon, RunningProcess
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


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunnerDaemon:
    d = RunnerDaemon(paths=_paths(tmp_path))
    monkeypatch.setattr(d, "_sync_to_backend_async", lambda: None)
    # Run side effects inline so binding is observable synchronously.
    monkeypatch.setattr(d, "_run_side_effect", lambda _label, callback: callback())
    d._db.add_job(
        name="funding-watch",
        job_type=JOB_TYPE_SCRIPT,
        payload={
            "script_path": ".wayfinder_runs/watch.py",
            "notify_session_id": "ses_old",
        },
        interval_seconds=60,
    )
    return d


def _bound_session(d: RunnerDaemon) -> str | None:
    job, _ = d._db.get_job(name="funding-watch")
    return job.payload.get("notify_session_id")


class _CaptureTransport:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def roundtrip(self, payload: bytes) -> bytes:
        self.requests.append(json.loads(payload))
        return b'{"ok": true, "result": {}}\n'

    def describe(self) -> str:
        return "capture"


def test_control_client_forwards_caller_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_caller")
    transport = _CaptureTransport()
    client = RunnerControlClient(sock_path=tmp_path / "s.sock", transport=transport)

    client.call("run_once", {"name": "funding-watch"})

    assert transport.requests[0]["params"] == {
        "name": "funding-watch",
        "caller_session_id": "ses_caller",
    }


def test_control_client_omits_session_when_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    monkeypatch.delenv("OPENCODE_SESSIONID", raising=False)
    transport = _CaptureTransport()
    client = RunnerControlClient(sock_path=tmp_path / "s.sock", transport=transport)

    client.call("status")

    assert transport.requests[0]["params"] == {}


def test_update_job_payload_replacement_keeps_binding(daemon: RunnerDaemon) -> None:
    resp = daemon.ctl_update_job(
        name="funding-watch",
        payload={"script_path": ".wayfinder_runs/watch.py", "args": ["--fast"]},
    )

    assert resp["ok"] is True
    assert _bound_session(daemon) == "ses_old"


@pytest.mark.parametrize(
    "reference",
    [
        lambda d, sid: d.ctl_update_job(
            name="funding-watch", payload=None, caller_session_id=sid
        ),
        lambda d, sid: d.ctl_resume_job(name="funding-watch", caller_session_id=sid),
        lambda d, sid: d.ctl_run_once(name="funding-watch", caller_session_id=sid),
    ],
    ids=["update_job", "resume_job", "run_once"],
)
def test_referencing_a_job_rebinds_to_caller_session(
    daemon: RunnerDaemon,
    monkeypatch: pytest.MonkeyPatch,
    reference: Callable[[RunnerDaemon, str], dict],
) -> None:
    monkeypatch.setattr(daemon, "_maybe_start_job", lambda **_kw: 1)

    assert reference(daemon, "ses_new")["ok"] is True
    assert _bound_session(daemon) == "ses_new"


def test_referencing_a_job_rebinds_via_scan_without_caller_session(
    daemon: RunnerDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon_module, "is_opencode_instance", lambda: True)
    scanned: list[str] = []

    def _find(job_name: str) -> str:
        scanned.append(job_name)
        return "ses_scanned"

    monkeypatch.setattr(OPENCODE_CLIENT, "find_session_referencing_job", _find)

    daemon.ctl_update_job(name="funding-watch", payload=None)

    assert scanned == ["funding-watch"]
    assert _bound_session(daemon) == "ses_scanned"


def test_scan_miss_keeps_existing_binding(
    daemon: RunnerDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon_module, "is_opencode_instance", lambda: True)
    monkeypatch.setattr(
        OPENCODE_CLIENT, "find_session_referencing_job", lambda _name: None
    )

    daemon.ctl_update_job(name="funding-watch", payload=None)

    assert _bound_session(daemon) == "ses_old"


@pytest.mark.parametrize(("live", "expected_posts"), [(True, 1), (False, 0)])
def test_notify_skips_archived_or_deleted_session(
    daemon: RunnerDaemon,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    live: bool,
    expected_posts: int,
) -> None:
    log = tmp_path / "run.log"
    log.write_text("boom\n", encoding="utf-8")
    posts: list[str] = []
    monkeypatch.setattr(OPENCODE_CLIENT, "healthy", lambda: True)
    monkeypatch.setattr(OPENCODE_CLIENT, "is_live_session", lambda _sid: live)
    monkeypatch.setattr(
        OPENCODE_CLIENT, "send_message", lambda sid, _text: posts.append(sid)
    )
    job, _ = daemon._db.get_job(name="funding-watch")

    daemon._notify_session(
        RunningProcess(
            run_id=1,
            job_id=job.id,
            job_name="funding-watch",
            started_at=0,
            reason="schedule",
            scheduled_for=0,
            timeout_seconds=None,
            popen=object(),  # type: ignore[arg-type]
            log_path=log,
        ),
        status=RunStatus.FAILED,
        error_text="boom",
    )

    assert len(posts) == expected_posts
