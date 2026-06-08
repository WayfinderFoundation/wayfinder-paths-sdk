from __future__ import annotations

from pathlib import Path
from typing import Any

from click.testing import CliRunner

from wayfinder_paths.runner import cli as runner_cli_module
from wayfinder_paths.runner.cli import runner_cli


class _FakeClient:
    def __init__(self, calls: list[tuple[str, dict[str, Any] | None]]) -> None:
        self._calls = calls

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._calls.append((method, params))
        return {"ok": True, "result": {"method": method}}


def test_add_job_cli_accepts_interval(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []
    monkeypatch.setattr(
        runner_cli_module,
        "get_runner_paths",
        lambda: type("Paths", (), {"sock_path": Path("runner.sock")})(),
    )
    monkeypatch.setattr(runner_cli_module, "_client", lambda _sock: _FakeClient(calls))

    result = CliRunner().invoke(
        runner_cli,
        [
            "add-job",
            "--name",
            "job",
            "--type",
            "script",
            "--script-path",
            ".wayfinder_runs/job.py",
            "--interval",
            "60",
        ],
    )

    assert result.exit_code == 0
    assert calls[0][0] == "add_job"
    assert calls[0][1]["interval_seconds"] == 60
    assert "cron_expr" not in calls[0][1]


def test_add_job_cli_accepts_cron(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []
    monkeypatch.setattr(
        runner_cli_module,
        "get_runner_paths",
        lambda: type("Paths", (), {"sock_path": Path("runner.sock")})(),
    )
    monkeypatch.setattr(runner_cli_module, "_client", lambda _sock: _FakeClient(calls))

    result = CliRunner().invoke(
        runner_cli,
        [
            "add-job",
            "--name",
            "job",
            "--type",
            "script",
            "--script-path",
            ".wayfinder_runs/job.py",
            "--cron",
            "0 9 * * 1-5",
            "--timezone",
            "America/Toronto",
        ],
    )

    assert result.exit_code == 0
    assert calls[0][1]["cron_expr"] == "0 9 * * 1-5"
    assert calls[0][1]["timezone"] == "America/Toronto"
    assert "interval_seconds" not in calls[0][1]


def test_add_job_cli_rejects_missing_schedule() -> None:
    result = CliRunner().invoke(
        runner_cli,
        [
            "add-job",
            "--name",
            "job",
            "--type",
            "script",
            "--script-path",
            ".wayfinder_runs/job.py",
        ],
    )

    assert result.exit_code != 0
    assert "exactly one" in result.output
