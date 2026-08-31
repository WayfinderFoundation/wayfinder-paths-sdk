"""Helpers for freezing executable job bundles."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


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
