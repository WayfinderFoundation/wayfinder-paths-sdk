from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType


def _repo_root() -> Path:
    cur = Path(__file__).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def strategy_bases() -> list[Path]:
    root = _repo_root()
    runs = Path(os.getenv("WAYFINDER_RUNS_DIR") or ".wayfinder_runs")
    if not runs.is_absolute():
        runs = root / runs
    return [root / "wayfinder_paths" / "strategies", runs / "strategies"]


def find_strategy_dir(name: str) -> Path | None:
    for base in strategy_bases():
        candidate = base / name
        if (candidate / "manifest.yaml").exists():
            return candidate
    return None


def load_strategy_module(strategy_name: str) -> tuple[ModuleType, Path]:
    """Load a strategy's python module from either the built-in tree or `.wayfinder_runs/strategies/`.

    Returns (module, strategy_dir). The strategy_dir is needed so callers can read
    the manifest from the right location.
    """
    strat_dir = find_strategy_dir(strategy_name)
    if strat_dir is None:
        raise FileNotFoundError(f"Missing manifest.yaml for strategy: {strategy_name}")

    builtin_root = _repo_root() / "wayfinder_paths" / "strategies"
    if strat_dir.is_relative_to(builtin_root):
        module = importlib.import_module(
            f"wayfinder_paths.strategies.{strategy_name}.strategy"
        )
        return importlib.reload(module), strat_dir

    file_path = strat_dir / "strategy.py"
    mod_name = f"_wayfinder_runs_strategy_{strategy_name}"
    spec = importlib.util.spec_from_file_location(
        mod_name,
        file_path,
        submodule_search_locations=[str(strat_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load strategy module at {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module, strat_dir
