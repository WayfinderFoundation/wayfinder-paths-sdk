from __future__ import annotations

import os
from pathlib import Path

from wayfinder_paths.runner.paths import get_runner_paths


def test_get_runner_paths_default_location(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("WAYFINDER_RUNNER_DIR", raising=False)
    monkeypatch.delenv("WAYFINDER_RUNNER_STATE_DIR", raising=False)

    paths = get_runner_paths(repo_root=tmp_path)
    assert paths.runner_dir == (tmp_path / ".wayfinder" / "runner")
    assert paths.db_path.name == "state.db"
    assert paths.sock_path.name == "runner.sock"


def test_get_runner_paths_env_override_relative(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("WAYFINDER_RUNNER_DIR", "custom/runner")
    paths = get_runner_paths(repo_root=tmp_path)
    assert paths.runner_dir == (tmp_path / "custom" / "runner").resolve()


def test_get_runner_paths_env_override_absolute(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    abs_dir = (tmp_path / "abs-runner").resolve()
    monkeypatch.setenv("WAYFINDER_RUNNER_STATE_DIR", str(abs_dir))
    paths = get_runner_paths(repo_root=tmp_path)
    assert paths.runner_dir == abs_dir
    assert os.fspath(paths.sock_path).startswith(os.fspath(abs_dir))
