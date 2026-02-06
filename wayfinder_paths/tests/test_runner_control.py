from __future__ import annotations

import tempfile
import time
from pathlib import Path

from wayfinder_paths.runner.client import RunnerControlClient
from wayfinder_paths.runner.control import RunnerControlServer


class _FakeDaemon:
    def ctl_status(self) -> dict:
        return {"ok": True, "result": {"hello": "world"}}

    def ctl_shutdown(self) -> dict:
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
        return {"ok": True}

    def ctl_resume_job(self, **_kw) -> dict:  # type: ignore[no-untyped-def]
        return {"ok": True}

    def ctl_run_once(self, **_kw) -> dict:  # type: ignore[no-untyped-def]
        return {"ok": True, "result": {"run_id": 123}}

    def ctl_delete_job(self, **_kw) -> dict:  # type: ignore[no-untyped-def]
        return {"ok": True, "result": {"deleted": True}}


def test_runner_control_roundtrip(tmp_path: Path) -> None:
    # macOS has a short AF_UNIX path limit; tmp_path can be too long. Use /tmp.
    sock = Path(tempfile.gettempdir()) / f"wayfinder-runner-{time.time_ns()}.sock"
    server = RunnerControlServer(sock_path=sock, daemon=_FakeDaemon())
    server.start()
    try:
        client = RunnerControlClient(sock_path=sock)

        # Be robust to thread scheduling delays: wait until we can successfully
        # roundtrip a request (not just until the socket file appears).
        deadline = time.time() + 2.0
        resp = None
        while time.time() < deadline:
            resp = client.call("status")
            if resp.get("ok") is True:
                break
            time.sleep(0.02)
        assert resp is not None
        assert resp.get("ok") is True, resp
        assert resp["result"]["hello"] == "world"

        resp = client.call("job_runs", {"name": "job", "limit": 5})
        assert resp.get("ok") is True, resp
        assert resp["result"]["runs"][0]["run_id"] == 1

        resp = client.call("run_report", {"run_id": 1, "tail_bytes": 1000})
        assert resp.get("ok") is True, resp
        assert resp["result"]["run"]["run_id"] == 1

        resp = client.call("delete_job", {"name": "job"})
        assert resp.get("ok") is True, resp
        assert resp["result"]["deleted"] is True

        resp = client.call("shutdown")
        assert resp.get("ok") is True, resp
        assert resp["result"]["shutdown"] is True
    finally:
        server.stop()
