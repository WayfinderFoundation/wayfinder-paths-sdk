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

_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _iter_files(root: Path, *, ignore_dirs: set[str]) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(
            [
                d
                for d in dirnames
                if d not in ignore_dirs and not (rel_dir == Path(".") and d == "dist")
            ]
        )
        for filename in sorted(filenames):
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

        files = sorted(
            _iter_files(pack_dir, ignore_dirs=ignore),
            key=lambda path: path.relative_to(pack_dir).as_posix(),
        )
        if not files:
            raise PackBuildError("No files found to bundle")

        cls._write_archive(root=pack_dir, files=files, out_path=out_path)

        sha = _sha256_file(out_path)
        return BuiltPack(bundle_path=out_path, bundle_sha256=sha, manifest=manifest)

    @classmethod
    def build_source_archive(
        cls,
        *,
        pack_dir: Path,
        out_path: Path,
        ignore_dirs: set[str] | None = None,
    ) -> Path:
        pack_dir = pack_dir.resolve()
        if not pack_dir.exists():
            raise PackBuildError(f"Pack directory not found: {pack_dir}")

        ignore = set(ignore_dirs or _DEFAULT_IGNORE_DIRS)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        files = sorted(
            _iter_files(pack_dir, ignore_dirs=ignore),
            key=lambda path: path.relative_to(pack_dir).as_posix(),
        )
        if not files:
            raise PackBuildError("No files found to archive")

        cls._write_archive(root=pack_dir, files=files, out_path=out_path)
        return out_path

    @staticmethod
    def _write_archive(*, root: Path, files: list[Path], out_path: Path) -> None:
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in files:
                rel = file_path.relative_to(root).as_posix()
                info = zipfile.ZipInfo(rel, date_time=_ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o644 << 16
                zf.writestr(info, file_path.read_bytes())
