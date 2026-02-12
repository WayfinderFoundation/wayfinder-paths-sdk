#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_core_config_module(repo_root: Path = REPO_ROOT) -> ModuleType:
    """Load `wayfinder_paths/core/config.py` without importing the package.

    This avoids importing `wayfinder_paths/__init__.py` (and third-party deps)
    before `poetry install` has been run on remote hosts.
    """
    config_path = repo_root / "wayfinder_paths" / "core" / "config.py"
    spec = importlib.util.spec_from_file_location(
        "_wayfinder_paths_core_config", config_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load config module: {config_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
