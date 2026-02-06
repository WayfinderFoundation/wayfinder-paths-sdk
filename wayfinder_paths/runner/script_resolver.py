from __future__ import annotations

import os
from pathlib import Path

from wayfinder_paths.runner.paths import RunnerPaths


def runs_root(paths: RunnerPaths) -> Path:
    candidate = (os.getenv("WAYFINDER_RUNS_DIR") or ".wayfinder_runs").strip()
    p = Path(candidate)
    if not p.is_absolute():
        p = paths.repo_root / p
    return p.resolve(strict=False)


def resolve_script_path(paths: RunnerPaths, script_path: str) -> Path:
    raw = str(script_path).strip()
    if not raw:
        raise ValueError("script_path is required")

    p = Path(raw)
    if not p.is_absolute():
        p = paths.repo_root / p
    resolved = p.resolve(strict=False)

    root = runs_root(paths)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"script_path must be inside the local runs directory ({root})"
        ) from exc

    if not resolved.exists():
        raise FileNotFoundError(f"Script file not found: {resolved}")

    if resolved.suffix.lower() != ".py":
        raise ValueError("Only .py scripts are supported")

    return resolved
