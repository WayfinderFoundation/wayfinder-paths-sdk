from __future__ import annotations

import json
from pathlib import Path

import pytest

from wayfinder_paths.runner.monitor_state import (
    atomic_write_json,
    monitor_state_path,
    read_monitor_state,
    write_monitor_state,
)


def test_monitor_state_path_uses_runner_dir_and_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner_dir = tmp_path / "runner"
    monkeypatch.setenv("WAYFINDER_RUNNER_DIR", str(runner_dir))
    monkeypatch.setenv("WAYFINDER_KV_NAMESPACE", "hype-sol-funding")

    assert monitor_state_path("latest") == (
        runner_dir / "job_state" / "hype-sol-funding" / "latest.json"
    )


def test_monitor_state_path_falls_back_to_project_runner_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WAYFINDER_RUNNER_DIR", raising=False)
    monkeypatch.delenv("WAYFINDER_RUNNER_STATE_DIR", raising=False)
    monkeypatch.delenv("WAYFINDER_KV_NAMESPACE", raising=False)
    monkeypatch.delenv("WAYFINDER_JOB_NAME", raising=False)

    assert monitor_state_path("latest") == (
        tmp_path / ".wayfinder" / "runner" / "job_state" / "default" / "latest.json"
    )


def test_write_and_read_monitor_state_round_trips_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAYFINDER_RUNNER_DIR", str(tmp_path / "runner"))
    monkeypatch.setenv("WAYFINDER_KV_NAMESPACE", "funding-monitor")

    path = write_monitor_state("last-alert", {"sent": True, "count": 2})

    assert (
        path
        == tmp_path / "runner" / "job_state" / "funding-monitor" / "last-alert.json"
    )
    assert read_monitor_state("last-alert") == {"sent": True, "count": 2}
    assert list(path.parent.glob("*.tmp")) == []


def test_read_monitor_state_returns_default_for_missing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAYFINDER_RUNNER_DIR", str(tmp_path / "runner"))

    assert read_monitor_state("missing", default={"seeded": False}) == {"seeded": False}


def test_monitor_state_helpers_support_default_state_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAYFINDER_RUNNER_DIR", str(tmp_path / "runner"))
    monkeypatch.setenv("WAYFINDER_KV_NAMESPACE", "funding-monitor")

    path = write_monitor_state({"seeded": True})

    assert path == tmp_path / "runner" / "job_state" / "funding-monitor" / "state.json"
    assert read_monitor_state(default={"seeded": False}) == {"seeded": True}


def test_read_monitor_state_rejects_non_object_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAYFINDER_RUNNER_DIR", str(tmp_path / "runner"))
    path = monitor_state_path("bad")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    with pytest.raises(ValueError, match="monitor state must be a JSON object"):
        read_monitor_state("bad")


def test_monitor_state_path_sanitizes_namespace_and_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAYFINDER_RUNNER_DIR", str(tmp_path / "runner"))
    monkeypatch.setenv("WAYFINDER_KV_NAMESPACE", "../../unsafe namespace")

    assert monitor_state_path("../alert state") == (
        tmp_path / "runner" / "job_state" / "unsafe_namespace" / "alert_state.json"
    )


def test_atomic_write_json_replaces_existing_payload(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    atomic_write_json(path, {"old": True})
    atomic_write_json(path, {"new": True})

    assert json.loads(path.read_text(encoding="utf-8")) == {"new": True}
    assert list(tmp_path.glob("*.tmp")) == []


def test_agent_docs_reference_monitor_state_helper() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    docs = [
        repo_root / ".opencode" / "agents" / "wayfinder.md",
        repo_root / "CLAUDE.md",
    ]

    for path in docs:
        text = path.read_text(encoding="utf-8")
        assert "wayfinder_paths.runner.monitor_state" in text
        assert "$WAYFINDER_RUNNER_DIR/job_state/$WAYFINDER_KV_NAMESPACE/" in text
