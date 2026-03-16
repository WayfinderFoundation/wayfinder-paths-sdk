from __future__ import annotations

import hashlib
import os
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from wayfinder_paths.packs.manifest import PackManifest, PackManifestError


class PackBuildError(Exception):
    pass


@dataclass(frozen=True)
class BuiltPack:
    bundle_path: Path
    bundle_sha256: str
    manifest: PackManifest


_DEFAULT_IGNORE_DIRS = {
    ".build",
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    ".wayfinder",
}


def _iter_files(root: Path, *, ignore_dirs: set[str]) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = [
            d
            for d in dirnames
            if d not in ignore_dirs and not (rel_dir == Path(".") and d == "dist")
        ]
        for filename in filenames:
            yield Path(dirpath) / filename


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class PackBuilder:
    MANIFEST_FILENAME = "wfpack.yaml"

    @classmethod
    def build(
        cls,
        *,
        pack_dir: Path,
        out_path: Path,
        ignore_dirs: set[str] | None = None,
    ) -> BuiltPack:
        pack_dir = pack_dir.resolve()
        if not pack_dir.exists():
            raise PackBuildError(f"Pack directory not found: {pack_dir}")

        manifest_path = pack_dir / cls.MANIFEST_FILENAME
        if not manifest_path.exists():
            raise PackBuildError(f"Missing {cls.MANIFEST_FILENAME} in {pack_dir}")

        try:
            manifest = PackManifest.load(manifest_path)
        except PackManifestError as exc:
            raise PackBuildError(str(exc)) from exc

        ignore = set(ignore_dirs or _DEFAULT_IGNORE_DIRS)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        files = list(_iter_files(pack_dir, ignore_dirs=ignore))
        if not files:
            raise PackBuildError("No files found to bundle")

        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in files:
                rel = file_path.relative_to(pack_dir).as_posix()
                zf.write(file_path, arcname=rel)

        sha = _sha256_file(out_path)
        return BuiltPack(bundle_path=out_path, bundle_sha256=sha, manifest=manifest)
