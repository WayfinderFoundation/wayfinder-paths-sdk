from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.core.clients.OpenCodeClient import OPENCODE_CLIENT
from wayfinder_paths.runner import client as client_module
from wayfinder_paths.runner import daemon as daemon_module
from wayfinder_paths.runner.api import dispatch
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
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RunnerDaemon]:
    d = RunnerDaemon(paths=_paths(tmp_path))
    monkeypatch.setattr(d, "_sync_to_backend_async", lambda: None)
    d._db.add_job(
        name="funding-watch",
        job_type=JOB_TYPE_SCRIPT,
        payload={
            "script_path": ".wayfinder_runs/watch.py",
            "notify_session_id": "ses_old",
        },
        interval_seconds=60,
    )
    yield d
    d._db.close()


@pytest.fixture(autouse=True)
def clear_caller_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in client_module.SESSION_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(client_module, "is_opencode_instance", lambda: False)


def _bound_session(d: RunnerDaemon) -> str | None:
    result = d._db.get_job(name="funding-watch")
    assert result is not None
    job, _ = result
    return job.payload.get("notify_session_id")


class _CaptureTransport:
    def __init__(self, daemon: RunnerDaemon | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.daemon = daemon

    def roundtrip(self, payload: bytes) -> bytes:
        request = json.loads(payload)
        self.requests.append(request)
        if self.daemon is not None:
            return json.dumps(dispatch(self.daemon, **request)).encode() + b"\n"
        return b'{"ok": true, "result": {}}\n'

    def describe(self) -> str:
        return "capture"


@pytest.mark.parametrize("supplied", [None, "ses_explicit"])
def test_control_client_prefers_supplied_session_then_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supplied: str | None
) -> None:
    monkeypatch.setattr(client_module, "is_opencode_instance", lambda: True)
    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_caller")
    monkeypatch.setattr(
        OPENCODE_CLIENT,
        "find_session_referencing_job",
        lambda _name: pytest.fail("Known caller must not scan"),
    )
    transport = _CaptureTransport()
    client = RunnerControlClient(sock_path=tmp_path / "s.sock", transport=transport)

    client.call("run_once", {"name": "funding-watch", "caller_session_id": supplied})

    assert transport.requests[0]["params"] == {
        "name": "funding-watch",
        "caller_session_id": supplied or "ses_caller",
    }


@pytest.mark.parametrize("method", ["status", "job_runs", "pause_job", "delete_job"])
def test_nonbinding_actions_do_not_discover_or_forward_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    monkeypatch.setattr(client_module, "is_opencode_instance", lambda: True)
    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_caller")
    monkeypatch.setattr(
        OPENCODE_CLIENT,
        "find_session_referencing_job",
        lambda _name: pytest.fail("Nonbinding actions must not scan"),
    )
    transport = _CaptureTransport()
    client = RunnerControlClient(sock_path=tmp_path / "s.sock", transport=transport)

    client.call(method, {"name": "funding-watch"})

    assert transport.requests[0]["params"] == {"name": "funding-watch"}


def test_control_client_does_not_scan_outside_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        OPENCODE_CLIENT,
        "find_session_referencing_job",
        lambda _name: pytest.fail("Local clients must not scan"),
    )
    transport = _CaptureTransport()
    client = RunnerControlClient(sock_path=tmp_path / "s.sock", transport=transport)
    client.call("run_once", {"name": "funding-watch"})
    assert transport.requests[0]["params"] == {"name": "funding-watch"}


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
    reference: Callable[[RunnerDaemon, str], dict[str, Any]],
) -> None:
    monkeypatch.setattr(daemon, "_maybe_start_job", lambda **_kw: 1)

    assert reference(daemon, "ses_new")["ok"] is True
    assert _bound_session(daemon) == "ses_new"


def test_referencing_a_job_rebinds_via_scan_without_caller_session(
    daemon: RunnerDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "is_opencode_instance", lambda: True)
    scanned: list[str] = []

    def _find(job_name: str) -> str:
        scanned.append(job_name)
        return "ses_scanned"

    monkeypatch.setattr(OPENCODE_CLIENT, "find_session_referencing_job", _find)

    transport = _CaptureTransport(daemon)
    client = RunnerControlClient(sock_path=Path("unused"), transport=transport)
    response = client.call("update_job", {"name": "funding-watch", "payload": None})

    assert response["ok"] is True
    assert scanned == ["funding-watch"]
    assert _bound_session(daemon) == "ses_scanned"


def test_scan_miss_keeps_existing_binding(
    daemon: RunnerDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "is_opencode_instance", lambda: True)
    monkeypatch.setattr(
        OPENCODE_CLIENT, "find_session_referencing_job", lambda _name: None
    )

    client = RunnerControlClient(
        sock_path=Path("unused"), transport=_CaptureTransport(daemon)
    )
    response = client.call("update_job", {"name": "funding-watch", "payload": None})

    assert response["ok"] is True
    assert _bound_session(daemon) == "ses_old"


@pytest.mark.parametrize("method", ["add_job", "update_job"])
@pytest.mark.parametrize("override", ["ses_override", None])
def test_explicit_payload_binding_overrides_caller(
    daemon: RunnerDaemon, method: str, override: str | None
) -> None:
    if method == "add_job":
        daemon._db.delete_job(name="funding-watch")
    response = dispatch(
        daemon,
        method=method,
        params={
            "name": "funding-watch",
            "type": "strategy",
            "payload": {"strategy": "example", "notify_session_id": override},
            "interval_seconds": 60,
            "caller_session_id": "ses_caller",
        },
    )
    assert response["ok"] is True
    assert _bound_session(daemon) == override


@pytest.mark.parametrize("method", ["add_job", "update_job", "resume_job", "run_once"])
def test_fast_worker_posts_to_discovered_session(
    daemon: RunnerDaemon,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    script = tmp_path / ".wayfinder_runs" / "watch.py"
    script.parent.mkdir()
    script.write_text("raise SystemExit('quick failure')\n", encoding="utf-8")
    if method == "add_job":
        daemon._db.delete_job(name="funding-watch")
    elif method == "resume_job":
        daemon.ctl_pause_job(name="funding-watch")
    transport = _CaptureTransport(daemon)
    client = RunnerControlClient(sock_path=Path("unused"), transport=transport)
    notified = threading.Event()
    posts: list[str] = []

    def find(_name: str) -> str:
        # Discovery must finish before the request can make a worker runnable.
        assert transport.requests == []
        assert not daemon._running
        return "ses_discovered"

    def send(session_id: str, _text: str) -> bool:
        posts.append(session_id)
        notified.set()
        return True

    monkeypatch.setattr(client_module, "is_opencode_instance", lambda: True)
    monkeypatch.setattr(daemon_module, "is_opencode_instance", lambda: False)
    monkeypatch.setattr(OPENCODE_CLIENT, "find_session_referencing_job", find)
    monkeypatch.setattr(OPENCODE_CLIENT, "healthy", lambda: True)
    monkeypatch.setattr(OPENCODE_CLIENT, "is_live_session", lambda _sid: True)
    monkeypatch.setattr(OPENCODE_CLIENT, "send_message", send)

    response = client.call(
        method,
        {
            "name": "funding-watch",
            "type": "script",
            "payload": {"script_path": str(script)},
            "interval_seconds": 60,
        },
    )
    assert response["ok"] is True
    assert _bound_session(daemon) == "ses_discovered"
    if method != "run_once":
        daemon.tick()
    process = next(iter(daemon._running.values()))
    process.popen.wait(timeout=5)
    daemon._reap(now=int(time.time()))

    assert notified.wait(timeout=5)
    assert posts == ["ses_discovered"]


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
    result = daemon._db.get_job(name="funding-watch")
    assert result is not None
    job, _ = result

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
