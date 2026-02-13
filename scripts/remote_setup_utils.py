#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

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


# ---------------------------------------------------------------------------
# Shared helpers used across setup.py, stage1, and stage2
# ---------------------------------------------------------------------------


def run_cmd(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    parsed = json.loads(path.read_text())
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{path} must be a JSON object at the top level.")
    return parsed


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def discover_strategies(repo_root: Path = REPO_ROOT) -> list[str]:
    strategies_dir = repo_root / "wayfinder_paths" / "strategies"
    if not strategies_dir.exists():
        return []
    return sorted(
        d.name for d in strategies_dir.iterdir() if (d / "strategy.py").exists()
    )


def ensure_config(
    *,
    api_key: str | None,
    config_path: Path | None = None,
    template_path: Path | None = None,
) -> None:
    if config_path is None:
        config_path = REPO_ROOT / "config.json"
    if template_path is None:
        template_path = REPO_ROOT / "config.example.json"

    _cfg = load_core_config_module()
    if config_path.exists():
        config = read_json(config_path) or {}
    else:
        config = read_json(template_path) or {}

    system = config.get("system")
    if not isinstance(system, dict):
        system = {}
    if api_key:
        system["api_key"] = api_key
    system.setdefault("api_base_url", "https://strategies.wayfinder.ai/api/v1")
    config["system"] = system

    if "strategy" not in config:
        template = read_json(template_path) or {}
        if isinstance(template.get("strategy"), dict):
            config["strategy"] = template["strategy"]

    _cfg.write_config_json(config_path, config)
    print(f"Wrote {config_path}")


def ensure_mcp_json(*, config_path: Path, repo_root: Path = REPO_ROOT) -> None:
    mcp_path = repo_root / ".mcp.json"
    mcp = read_json(mcp_path)
    if not mcp:
        raise RuntimeError("Missing .mcp.json (expected in repo root).")

    mcp_servers = mcp.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
        mcp["mcpServers"] = mcp_servers

    wayfinder = mcp_servers.get("wayfinder")
    if not isinstance(wayfinder, dict):
        wayfinder = {}
        mcp_servers["wayfinder"] = wayfinder

    wayfinder["command"] = "poetry"
    wayfinder["args"] = ["run", "python", "-m", "wayfinder_paths.mcp.server"]

    env = wayfinder.get("env")
    if not isinstance(env, dict):
        env = {}
    try:
        env["WAYFINDER_CONFIG_PATH"] = str(config_path.relative_to(repo_root))
    except ValueError:
        env["WAYFINDER_CONFIG_PATH"] = str(config_path)
    wayfinder["env"] = env

    write_json(mcp_path, mcp)
    print(f"Updated {mcp_path}")
