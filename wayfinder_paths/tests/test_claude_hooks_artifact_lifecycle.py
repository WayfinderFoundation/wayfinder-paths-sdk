from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _hook_path(name: str) -> Path:
    path = _repo_root() / ".claude" / "hooks" / name
    assert path.exists(), f"missing hook: {path}"
    return path


def _run_hook(
    hook_script: Path, payload: dict[str, Any], *, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(hook_script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )


def _clean_env(*, base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base or os.environ)
    for key in (
        "WAYFINDER_RUNS_DIR",
        "WAYFINDER_SCRATCH_DIR",
        "WAYFINDER_LIBRARY_DIR",
        "WAYFINDER_SESSION_ID",
        "WAYFINDER_WRITE_GUARD_MODE",
        "CLAUDE_ENV_FILE",
        "CLAUDE_SESSION_ID",
        "SESSION_ID",
    ):
        env.pop(key, None)
    return env


def test_session_start_creates_scratch_and_exports_env(tmp_path: Path) -> None:
    hook = _hook_path("wayfinder_session_start.py")

    runs_root = (tmp_path / "runs").resolve()
    env_file = (tmp_path / "claude.env").resolve()

    env = _clean_env()
    env["WAYFINDER_RUNS_DIR"] = str(runs_root)
    env["CLAUDE_ENV_FILE"] = str(env_file)

    result = _run_hook(hook, {"session_id": "test-session"}, env=env)

    out = json.loads(result.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "WAYFINDER_SCRATCH_DIR" in ctx
    assert "WAYFINDER_LIBRARY_DIR" in ctx

    scratch_dir = runs_root / ".scratch" / "test-session"
    library_dir = runs_root / "library"
    assert scratch_dir.is_dir()
    assert library_dir.is_dir()

    index = json.loads((scratch_dir / "index.json").read_text(encoding="utf-8"))
    assert index["session_id"] == "test-session"
    assert index["scratch_dir"] == str(scratch_dir)
    assert index["library_dir"] == str(library_dir)

    env_text = env_file.read_text(encoding="utf-8")
    assert "export WAYFINDER_SESSION_ID=" in env_text
    assert "export WAYFINDER_SCRATCH_DIR=" in env_text
    assert "export WAYFINDER_LIBRARY_DIR=" in env_text


@pytest.mark.parametrize(
    "relative_path",
    [
        ".scratch/test-session/a.py",
        "library/hyperliquid/b.py",
        "README.md",
    ],
)
def test_write_guard_allows_runs_subdirs(tmp_path: Path, relative_path: str) -> None:
    hook = _hook_path("wayfinder_write_guard.py")

    runs_root = (tmp_path / "runs").resolve()
    target = (runs_root / relative_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    env = _clean_env()
    env["WAYFINDER_RUNS_DIR"] = str(runs_root)

    payload = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    result = _run_hook(hook, payload, env=env)

    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_write_guard_denies_top_level_runs_writes(tmp_path: Path) -> None:
    hook = _hook_path("wayfinder_write_guard.py")

    runs_root = (tmp_path / "runs").resolve()
    target = (runs_root / "top_level.py").resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    env = _clean_env()
    env["WAYFINDER_RUNS_DIR"] = str(runs_root)

    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    result = _run_hook(hook, payload, env=env)

    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Blocked" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_session_end_deletes_scratch_dir(tmp_path: Path) -> None:
    hook = _hook_path("wayfinder_session_end.py")

    runs_root = (tmp_path / "runs").resolve()
    scratch_dir = (runs_root / ".scratch" / "test-session").resolve()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    (scratch_dir / "index.json").write_text("{}", encoding="utf-8")

    env = _clean_env()
    env["WAYFINDER_RUNS_DIR"] = str(runs_root)
    env["WAYFINDER_SCRATCH_DIR"] = str(scratch_dir)

    result = _run_hook(hook, {"session_id": "test-session"}, env=env)
    assert result.stdout.strip() == ""
    assert not scratch_dir.exists()
