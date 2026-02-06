from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def find_repo_root(*, start: Path | None = None) -> Path:
    cur = (start or Path.cwd()).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return cur


@dataclass(frozen=True)
class RunnerPaths:
    repo_root: Path
    runner_dir: Path
    db_path: Path
    logs_dir: Path
    sock_path: Path


def get_runner_paths(*, repo_root: Path | None = None) -> RunnerPaths:
    root = (repo_root or find_repo_root()).resolve()
    runner_dir_override = os.environ.get("WAYFINDER_RUNNER_DIR") or os.environ.get(
        "WAYFINDER_RUNNER_STATE_DIR"
    )
    if runner_dir_override:
        rd = Path(runner_dir_override).expanduser()
        if not rd.is_absolute():
            rd = root / rd
        runner_dir = rd.resolve()
    else:
        runner_dir = root / ".wayfinder" / "runner"
    return RunnerPaths(
        repo_root=root,
        runner_dir=runner_dir,
        db_path=runner_dir / "state.db",
        logs_dir=runner_dir / "logs",
        sock_path=runner_dir / "runner.sock",
    )
