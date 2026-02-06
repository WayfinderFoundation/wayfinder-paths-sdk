#!/usr/bin/env python3

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import wayfinder_hook_utils as hook_utils


def _scratch_dir(repo_root: Path, payload: dict[str, Any]) -> Path | None:
    env_scratch = os.getenv("WAYFINDER_SCRATCH_DIR", "").strip()
    if env_scratch:
        return hook_utils.resolve_repo_path(repo_root, env_scratch)

    session_id = hook_utils.find_session_id(
        payload,
        env_keys=("WAYFINDER_SESSION_ID", "CLAUDE_SESSION_ID", "SESSION_ID"),
    )
    if not session_id:
        return None

    runs_root = hook_utils.runs_root(repo_root)
    return (runs_root / ".scratch" / session_id).resolve(strict=False)


def main() -> None:
    payload = hook_utils.load_payload()
    repo_root = hook_utils.repo_root()
    scratch_dir = _scratch_dir(repo_root, payload)
    if not scratch_dir:
        return

    runs_root = hook_utils.runs_root(repo_root)
    allowed_root = (runs_root / ".scratch").resolve(strict=False)
    if not scratch_dir.is_relative_to(allowed_root):
        # Safety: never delete outside `.wayfinder_runs/.scratch/...`
        return

    try:
        shutil.rmtree(scratch_dir)
    except OSError:
        return


if __name__ == "__main__":
    main()
