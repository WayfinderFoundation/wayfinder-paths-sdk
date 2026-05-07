from __future__ import annotations

from pathlib import Path
from typing import Any

from wayfinder_paths.mcp.utils import (
    catch_errors,
    err,
    ok,
    read_text_excerpt,
    read_yaml,
    repo_root,
)


def _describe_dir(base: Path, name: str) -> dict[str, Any] | None:
    target = base / name
    manifest_path = target / "manifest.yaml"
    if not manifest_path.exists():
        return None
    out: dict[str, Any] = {"name": name, "manifest": read_yaml(manifest_path)}
    readme = read_text_excerpt(target / "README.md")
    if readme:
        out["readme_excerpt"] = readme
    return out


def _describe_all(base: Path) -> list[dict[str, Any]]:
    if not base.exists():
        return []
    items: list[dict[str, Any]] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        described = _describe_dir(base, child.name)
        if described:
            items.append(described)
    return items


@catch_errors
async def core_get_adapters_and_strategies(name: str | None = None) -> dict[str, Any]:
    """List adapters and strategies with their manifests and README excerpts.

    No args → full catalog of every adapter and strategy with manifest + readme excerpt.
    Pass `name` to filter to a single adapter or strategy (matches across both directories).
    """
    root = repo_root()
    adapters_base = root / "wayfinder_paths" / "adapters"
    strategies_base = root / "wayfinder_paths" / "strategies"

    if name:
        adapter = _describe_dir(adapters_base, name)
        strategy = _describe_dir(strategies_base, name)
        if not adapter and not strategy:
            return err("not_found", f"Unknown adapter or strategy: {name}")
        return ok(
            {
                "adapters": [adapter] if adapter else [],
                "strategies": [strategy] if strategy else [],
            }
        )

    return ok(
        {
            "adapters": _describe_all(adapters_base),
            "strategies": _describe_all(strategies_base),
        }
    )
