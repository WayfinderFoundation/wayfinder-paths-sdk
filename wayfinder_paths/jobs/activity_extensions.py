"""Pinned Path components as dependencies of an ordinary strategy job.

Pins establish code identity, not trust. Review executable code before attaching
it; this uses the same trusted-author model as freestyle strategy scripts.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.participation import ParticipationPort
from wayfinder_paths.jobs.store import JobStore


def pin_activity_extension(
    root: Path, request: dict[str, Any], *, store: JobStore
) -> tuple[str, dict[str, Any]]:
    from wayfinder_paths.jobs.paths_runtime import (
        bundle_sha256,
        resolve_install,
        tree_sha256,
    )
    from wayfinder_paths.paths.cli import _installed_path_dir
    from wayfinder_paths.paths.manifest import PathManifest

    alias, slug = str(request["alias"]), str(request["slug"])
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", alias):
        raise ValueError("invalid activity extension alias")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", slug):
        raise ValueError("invalid Path slug")
    entry, base, _ = resolve_install(
        store, install_dir=request.get("install_dir"), slug=slug
    )
    if entry is None:
        raise ValueError(f"Path {slug} is not installed")
    version = str(request.get("version") or entry.get("version") or "")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.+_-]{0,99}", version):
        raise ValueError("invalid Path version")
    path_dir = _installed_path_dir(
        base=base, slug=slug, version=version, entry=entry
    ).resolve()
    if not path_dir.is_dir() or any(p.is_symlink() for p in path_dir.rglob("*")):
        raise ValueError("extension must be an installed directory without symlinks")
    manifest = PathManifest.load(path_dir / "wfpath.yaml")
    if manifest.version != version or manifest.slug != slug:
        raise ValueError("extension manifest does not match requested identity")
    component = manifest.resolve_component(request.get("component"))
    if component.get("kind") != "activity":
        raise ValueError("extension component must declare kind: activity")
    component_path = str(component.get("path") or "")
    module = (path_dir / component_path).resolve()
    if (
        not module.is_relative_to(path_dir)
        or not module.is_file()
        or module.suffix != ".py"
    ):
        raise ValueError("activity component must be a Python module inside the Path")
    digest = bundle_sha256(path_dir)
    lock_digest = (
        entry.get("bundle_sha256") if entry.get("version") == version else None
    )
    if lock_digest and digest != lock_digest:
        raise ValueError("installed Path bundle does not match its lock")
    tree_digest = tree_sha256(path_dir)
    target = root / "workspace" / "src" / "activity_extensions" / alias
    if target.exists():
        raise FileExistsError(f"extension already pinned: {alias}")
    shutil.copytree(
        path_dir,
        target,
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "bundle.zip", "install-intent.json", "dist"
        ),
    )
    if tree_sha256(target) != tree_digest:
        raise ValueError("Path changed while copying the extension")
    return alias, {
        "slug": slug,
        "version": version,
        "bundle_sha256": digest,
        "tree_sha256": tree_digest,
        "directory": target.relative_to(root).as_posix(),
        "component": component["id"],
        "component_path": component_path,
        "capabilities": list(component.get("capabilities") or []),
    }


def load_activity_extension(
    root: Path,
    pin: dict[str, Any],
    *,
    capability: str,
    config: dict[str, Any],
    options: dict[str, Any],
) -> ParticipationPort:
    from wayfinder_paths.jobs.freestyle.runtime import load_tick_module
    from wayfinder_paths.jobs.paths_runtime import tree_sha256

    directory = (root / str(pin["directory"])).resolve()
    extension_root = (root / "workspace" / "src" / "activity_extensions").resolve()
    if (
        not extension_root.is_relative_to(root.resolve())
        or directory == extension_root
        or not directory.is_relative_to(extension_root)
    ):
        raise ValueError("extension path escapes its strategy workspace")
    if (
        any(p.is_symlink() for p in directory.rglob("*"))
        or tree_sha256(directory) != pin["tree_sha256"]
    ):
        raise ValueError(
            "activity extension revision changed; re-pin and review a proposal"
        )
    if capability not in pin.get("capabilities", []):
        raise ValueError("activity capability is not declared by the pinned Path")
    component = (directory / str(pin["component_path"])).resolve()
    if not component.is_relative_to(directory):
        raise ValueError("activity module escapes the pinned Path")
    factory = getattr(load_tick_module(component), "build_activity_adapter", None)
    if not callable(factory):
        raise ValueError(
            "activity component must define build_activity_adapter(config, options)"
        )
    adapter = factory(config, options)
    if any(
        not callable(getattr(adapter, name, None))
        for name in ("observe", "protect", "submit", "reconcile", "close")
    ):
        raise ValueError("activity adapter is missing a lifecycle method")
    if not isinstance(getattr(adapter, "supports_submit", None), bool):
        raise ValueError("activity adapter must declare supports_submit")
    return adapter
