"""Helpers for freezing executable job bundles."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def copy_job_bundle(
    source: Path,
    destination: Path,
    *,
    existing_ok: bool = False,
) -> None:
    """Atomically copy the files that define an executable job bundle."""
    required = (source / "job.yaml", source / "workspace")
    if not required[0].is_file() or not required[1].is_dir():
        raise FileNotFoundError("job bundle requires job.yaml and workspace/")
    if destination.exists():
        if existing_ok:
            return
        raise FileExistsError(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        shutil.copy2(required[0], temporary / "job.yaml")
        shutil.copytree(required[1], temporary / "workspace")
        execution_spec = source / "execution_spec.json"
        if execution_spec.is_file():
            shutil.copy2(execution_spec, temporary / execution_spec.name)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def resolve_bundle_script_entrypoint(bundle: Path, job_data: Mapping[str, Any]) -> Path:
    """Resolve an entrypoint after its workspace was copied into a bundle.

    Job YAML may retain a repo-relative ``.wayfinder/jobs/.../workspace`` path
    or an absolute path from the source box. Both name the copied workspace,
    not a path that should still exist in the benchmark controller.
    """
    raw = str((job_data.get("script_loop") or {}).get("entrypoint") or "").strip()
    if not raw:
        raise FileNotFoundError("bundle script_loop.entrypoint is missing")
    path = Path(raw)
    if "workspace" in path.parts:
        index = max(
            position for position, part in enumerate(path.parts) if part == "workspace"
        )
        return bundle / "workspace" / Path(*path.parts[index + 1 :])
    if not path.is_absolute():
        return bundle / path
    raise ValueError("absolute bundle entrypoint is outside workspace")
