from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class PackHooksError(Exception):
    pass


@dataclass(frozen=True)
class PackHooksInstallReport:
    config_path: Path
    changed: bool
    hooks: list[str]


_WAYFINDER_HOOKS = [
    {
        "id": "wayfinder-pack-fmt",
        "name": "wayfinder pack fmt",
        "entry": "wayfinder pack fmt --path .",
        "language": "system",
        "pass_filenames": False,
        "stages": ["pre-commit"],
    },
    {
        "id": "wayfinder-pack-doctor",
        "name": "wayfinder pack doctor --check",
        "entry": "wayfinder pack doctor --check --path .",
        "language": "system",
        "pass_filenames": False,
        "stages": ["pre-commit"],
    },
    {
        "id": "wayfinder-pack-preview",
        "name": "wayfinder pack preview --check",
        "entry": "wayfinder pack preview --check --path .",
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
        raise PackHooksError(f"Failed to parse {path}") from exc

    if not isinstance(parsed, dict):
        raise PackHooksError(f"{path} must be a YAML object")
    if "repos" in parsed and not isinstance(parsed["repos"], list):
        raise PackHooksError(f"{path} repos must be a list")

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
                raise PackHooksError("local repo hooks entry must be a list")
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


def install_pack_hooks(*, pack_dir: Path) -> PackHooksInstallReport:
    pack_dir = pack_dir.resolve()
    if not pack_dir.exists():
        raise PackHooksError(f"Pack directory not found: {pack_dir}")
    if not pack_dir.is_dir():
        raise PackHooksError(f"Pack path must be a directory: {pack_dir}")

    config_path = pack_dir / ".pre-commit-config.yaml"
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

    return PackHooksInstallReport(
        config_path=config_path,
        changed=changed,
        hooks=[hook["id"] for hook in _WAYFINDER_HOOKS],
    )
