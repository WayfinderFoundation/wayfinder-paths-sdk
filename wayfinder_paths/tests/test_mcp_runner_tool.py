from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from wayfinder_paths.mcp.tools.runner import core_runner, core_runner_mutation
from wayfinder_paths.runner.control import RunnerControlServer


class _FakeDaemon:
    def __init__(self) -> None:
        self.control: RunnerControlServer | None = None
        self.paused: list[str] = []
        self.resumed: list[str] = []
        self.deleted: list[str] = []

    def ctl_status(self) -> dict:
        return {"ok": True, "result": {"hello": "world"}}

    def ctl_shutdown(self) -> dict:
        if self.control is not None:
            self.control.stop()
        return {"ok": True, "result": {"shutdown": True}}

    def ctl_job_runs(self, **_kw) -> dict:  # type: ignore[no-untyped-def]
        return {"ok": True, "result": {"runs": [{"run_id": 1}]}}

    def ctl_run_report(self, **_kw) -> dict:  # type: ignore[no-untyped-def]
        return {"ok": True, "result": {"run": {"run_id": 1}, "log_tail": "ok"}}

    def ctl_add_job(self, **_kw) -> dict:  # type: ignore[no-untyped-def]
        return {"ok": True, "result": {"job_id": 1}}

    def ctl_update_job(self, **_kw) -> dict:  # type: ignore[no-untyped-def]
        return {"ok": True, "result": {"updated": True}}

    def ctl_pause_job(self, **_kw) -> dict:  # type: ignore[no-untyped-def]
        self.paused.append(str(_kw.get("name")))
        return {"ok": True, "result": {"paused": True}}

    def ctl_resume_job(self, **_kw) -> dict:  # type: ignore[no-untyped-def]
        self.resumed.append(str(_kw.get("name")))
        return {"ok": True, "result": {"resumed": True}}

    def ctl_run_once(self, **_kw) -> dict:  # type: ignore[no-untyped-def]
        return {"ok": True, "result": {"run_id": 123}}

    def ctl_delete_job(self, **_kw) -> dict:  # type: ignore[no-untyped-def]
        self.deleted.append(str(_kw.get("name")))
        return {"ok": True, "result": {"deleted": True}}


def test_mcp_runner_tool_status_roundtrip(monkeypatch) -> None:
    monkeypatch.delenv("OPENCODE_INSTANCE_ID", raising=False)
    sock = (
        Path(tempfile.gettempdir()) / f"wayfinder-core_runner-mcp-{time.time_ns()}.sock"
    )
    daemon = _FakeDaemon()
    server = RunnerControlServer(sock_path=sock, daemon=daemon)
    daemon.control = server
    server.start()
    try:
        out = _run(core_runner(action="daemon_status", sock_path=str(sock)))
        assert out["ok"] is True
        assert out["result"]["started"] is True

        out = _run(core_runner(action="status", sock_path=str(sock)))
        assert out["ok"] is True
        assert out["result"]["hello"] == "world"

        out = _run(
            core_runner(action="job_runs", sock_path=str(sock), name="job", limit=5)
        )
        assert out["ok"] is True
        assert out["result"]["runs"][0]["run_id"] == 1

        out = _run(core_runner(action="run_report", sock_path=str(sock), run_id=1))
        assert out["ok"] is True
        assert out["result"]["run"]["run_id"] == 1

        out = _run(core_runner(action="delete_job", sock_path=str(sock), name="job"))
        assert out["ok"] is True
        assert out["result"]["deleted"] is True
        assert daemon.deleted == ["job"]

        out = _run(core_runner(action="daemon_stop", sock_path=str(sock)))
        assert out["ok"] is True
        assert out["result"]["stopped"] is True
    finally:
        server.stop()


def test_core_runner_blocks_mutation_on_opencode(monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "shell-test")
    sock = (
        Path(tempfile.gettempdir()) / f"wayfinder-core_runner-mcp-{time.time_ns()}.sock"
    )
    daemon = _FakeDaemon()
    server = RunnerControlServer(sock_path=sock, daemon=daemon)
    daemon.control = server
    server.start()
    try:
        out = _run(
            core_runner(action="job_runs", sock_path=str(sock), name="job", limit=5)
        )
        assert out["ok"] is True
        assert out["result"]["runs"][0]["run_id"] == 1

        out = _run(core_runner(action="pause_job", sock_path=str(sock), name="job"))
        assert out["ok"] is False
        assert out["error"]["code"] == "runner_action_requires_approval"
        assert out["error"]["details"]["approval_tool"] == "core_runner_mutation"
        assert daemon.paused == []
    finally:
        server.stop()


def test_core_runner_mutation_allows_user_directed_mutation_on_opencode(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "shell-test")
    sock = (
        Path(tempfile.gettempdir()) / f"wayfinder-core_runner-mcp-{time.time_ns()}.sock"
    )
    daemon = _FakeDaemon()
    server = RunnerControlServer(sock_path=sock, daemon=daemon)
    daemon.control = server
    server.start()
    try:
        out = _run(
            core_runner_mutation(action="pause_job", sock_path=str(sock), name="job")
        )
        assert out["ok"] is True
        assert out["result"]["paused"] is True
        assert daemon.paused == ["job"]
    finally:
        server.stop()


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)
