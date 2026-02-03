from __future__ import annotations

import json
from typing import Any

from wayfinder_paths.mcp.utils import read_text_excerpt, read_yaml, repo_root


async def list_adapters() -> str:
    root = repo_root()
    base = root / "wayfinder_paths" / "adapters"
    if not base.exists():
        return json.dumps({"error": f"Directory not found: {base}"})

    items: list[dict[str, Any]] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.yaml"
        if not manifest_path.exists():
            continue
        manifest = read_yaml(manifest_path)
        items.append(
            {
                "name": child.name,
                "entrypoint": manifest.get("entrypoint"),
                "capabilities": manifest.get("capabilities", []),
                "dependencies": manifest.get("dependencies", []),
            }
        )

    return json.dumps({"adapters": items}, indent=2)


async def list_strategies() -> str:
    root = repo_root()
    base = root / "wayfinder_paths" / "strategies"
    if not base.exists():
        return json.dumps({"error": f"Directory not found: {base}"})

    items: list[dict[str, Any]] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.yaml"
        if not manifest_path.exists():
            continue
        manifest = read_yaml(manifest_path)
        adapters = manifest.get("adapters", [])
        items.append(
            {
                "name": child.name,
                "status": manifest.get("status", "stable"),
                "entrypoint": manifest.get("entrypoint"),
                "adapters": adapters if isinstance(adapters, list) else [],
                "permissions_policy_present": bool(
                    isinstance(manifest.get("permissions"), dict)
                    and (manifest.get("permissions") or {}).get("policy")
                ),
            }
        )

    return json.dumps({"strategies": items}, indent=2)


async def describe_adapter(name: str) -> str:
    root = repo_root()
    target = root / "wayfinder_paths" / "adapters" / name
    if not target.exists():
        return json.dumps({"error": f"Unknown adapter: {name}"})

    manifest_path = target / "manifest.yaml"
    if not manifest_path.exists():
        return json.dumps({"error": f"Missing manifest.yaml for adapter: {name}"})

    out: dict[str, Any] = {
        "name": name,
        "manifest": read_yaml(manifest_path),
    }
    readme = read_text_excerpt(target / "README.md")
    if readme:
        out["readme_excerpt"] = readme

    return json.dumps(out, indent=2)


async def describe_strategy(name: str) -> str:
    root = repo_root()
    target = root / "wayfinder_paths" / "strategies" / name
    if not target.exists():
        return json.dumps({"error": f"Unknown strategy: {name}"})

    manifest_path = target / "manifest.yaml"
    if not manifest_path.exists():
        return json.dumps({"error": f"Missing manifest.yaml for strategy: {name}"})

    out: dict[str, Any] = {
        "name": name,
        "manifest": read_yaml(manifest_path),
    }
    readme = read_text_excerpt(target / "README.md")
    if readme:
        out["readme_excerpt"] = readme

    return json.dumps(out, indent=2)
