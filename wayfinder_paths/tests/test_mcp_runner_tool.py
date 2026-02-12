from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from wayfinder_paths.mcp.tools.runner import runner
from wayfinder_paths.runner.control import RunnerControlServer


class _FakeDaemon:
    def __init__(self) -> None:
        self.control: RunnerControlServer | None = None

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
        return {"ok": True, "result": {"paused": True}}

    def ctl_resume_job(self, **_kw) -> dict:  # type: ignore[no-untyped-def]
        return {"ok": True, "result": {"resumed": True}}

    def ctl_run_once(self, **_kw) -> dict:  # type: ignore[no-untyped-def]
        return {"ok": True, "result": {"run_id": 123}}

    def ctl_delete_job(self, **_kw) -> dict:  # type: ignore[no-untyped-def]
        return {"ok": True, "result": {"deleted": True}}


def test_mcp_runner_tool_status_roundtrip() -> None:
    sock = Path(tempfile.gettempdir()) / f"wayfinder-runner-mcp-{time.time_ns()}.sock"
    daemon = _FakeDaemon()
    server = RunnerControlServer(sock_path=sock, daemon=daemon)
    daemon.control = server
    server.start()
    try:
        out = _run(runner(action="daemon_status", sock_path=str(sock)))
        assert out["ok"] is True
        assert out["result"]["started"] is True

        out = _run(runner(action="status", sock_path=str(sock)))
        assert out["ok"] is True
        assert out["result"]["hello"] == "world"

        out = _run(runner(action="job_runs", sock_path=str(sock), name="job", limit=5))
        assert out["ok"] is True
        assert out["result"]["runs"][0]["run_id"] == 1

        out = _run(runner(action="run_report", sock_path=str(sock), run_id=1))
        assert out["ok"] is True
        assert out["result"]["run"]["run_id"] == 1

        out = _run(runner(action="delete_job", sock_path=str(sock), name="job"))
        assert out["ok"] is True
        assert out["result"]["deleted"] is True

        out = _run(runner(action="daemon_stop", sock_path=str(sock)))
        assert out["ok"] is True
        assert out["result"]["stopped"] is True
    finally:
        server.stop()


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)
