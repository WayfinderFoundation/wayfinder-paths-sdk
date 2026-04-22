from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class PathHooksError(Exception):
    pass


@dataclass(frozen=True)
class PathHooksInstallReport:
    config_path: Path
    changed: bool
    hooks: list[str]


_WAYFINDER_HOOKS = [
    {
        "id": "wayfinder-path-fmt",
        "name": "wayfinder path fmt",
        "entry": "wayfinder path fmt --path .",
        "language": "system",
        "pass_filenames": False,
        "stages": ["pre-commit"],
    },
    {
        "id": "wayfinder-path-doctor",
        "name": "wayfinder path doctor --check",
        "entry": "wayfinder path doctor --check --path .",
        "language": "system",
        "pass_filenames": False,
        "stages": ["pre-commit"],
    },
    {
        "id": "wayfinder-path-preview",
        "name": "wayfinder path preview --check",
        "entry": "wayfinder path preview --check --path .",
        "language": "system",
        "pass_filenames": False,
        "stages": ["pre-push"],
    },
]


def _load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"repos": []}

    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise PathHooksError(f"Failed to parse {path}") from exc

    if not isinstance(parsed, dict):
        raise PathHooksError(f"{path} must be a YAML object")
    if "repos" in parsed and not isinstance(parsed["repos"], list):
        raise PathHooksError(f"{path} repos must be a list")

    parsed.setdefault("repos", [])
    return parsed


def _ensure_local_repo(config: dict[str, Any]) -> dict[str, Any]:
    repos = config.setdefault("repos", [])
    for repo in repos:
        if isinstance(repo, dict) and repo.get("repo") == "local":
            hooks = repo.get("hooks")
            if hooks is None:
                repo["hooks"] = []
            elif not isinstance(hooks, list):
                raise PathHooksError("local repo hooks entry must be a list")
            return repo

    repo = {"repo": "local", "hooks": []}
    repos.append(repo)
    return repo


def _upsert_hook(hooks: list[dict[str, Any]], hook: dict[str, Any]) -> bool:
    hook_id = hook["id"]
    for idx, existing in enumerate(hooks):
        if isinstance(existing, dict) and existing.get("id") == hook_id:
            if existing == hook:
                return False
            hooks[idx] = hook
            return True
    hooks.append(hook)
    return True


def install_path_hooks(*, path_dir: Path) -> PathHooksInstallReport:
    path_dir = path_dir.resolve()
    if not path_dir.exists():
        raise PathHooksError(f"Path directory not found: {path_dir}")
    if not path_dir.is_dir():
        raise PathHooksError(f"Path directory must be a directory: {path_dir}")

    config_path = path_dir / ".pre-commit-config.yaml"
    config = _load_config(config_path)
    local_repo = _ensure_local_repo(config)
    hooks = local_repo["hooks"]
    assert isinstance(hooks, list)

    changed = False
    for hook in _WAYFINDER_HOOKS:
        changed = _upsert_hook(hooks, hook) or changed

    rendered = yaml.safe_dump(config, sort_keys=False, allow_unicode=False)
    normalized = rendered.rstrip() + "\n"
    current = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if current != normalized:
        config_path.write_text(normalized, encoding="utf-8")
        changed = True

    return PathHooksInstallReport(
        config_path=config_path,
        changed=changed,
        hooks=[hook["id"] for hook in _WAYFINDER_HOOKS],
    )
