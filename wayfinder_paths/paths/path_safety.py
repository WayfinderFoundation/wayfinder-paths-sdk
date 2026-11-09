from __future__ import annotations

import re
from pathlib import Path

from wayfinder_paths.paths.manifest import _is_valid_version

APPROVED_RUNTIME_PACKAGE = "wayfinder-paths"

_SAFE_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template"}
_SECRET_FILENAMES = {
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
}
_SECRET_SUFFIXES = {".key", ".p12", ".pem"}


def unsafe_bundle_path_reason(path: Path) -> str | None:
    """Return why *path* is unsafe to package, or ``None`` when it is safe."""
    if path.is_symlink():
        return "symlink"

    lowered = path.name.lower()
    if (
        lowered == ".env"
        or (lowered.startswith(".env.") and lowered not in _SAFE_ENV_TEMPLATES)
        or lowered in _SECRET_FILENAMES
        or path.suffix.lower() in _SECRET_SUFFIXES
    ):
        return "secret-like file"
    return None


def resolve_contained_path(root: Path, value: str, *, label: str) -> Path:
    """Resolve a required relative path without allowing it to escape *root*."""
    raw = str(value).strip()
    candidate = Path(raw)
    if not raw:
        raise ValueError(f"{label} is required")
    if candidate.is_absolute():
        raise ValueError(f"{label} must be relative: {raw}")

    root_resolved = root.resolve()
    resolved = (root_resolved / candidate).resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside {root_resolved}: {raw}") from exc
    return resolved


# Matches what the registry's slugify produces: lower-case letters, digits, "_" and
# "-", starting and ending with a letter or digit, no "--". No dots or slashes,
# so a slug can never name a parent directory or another URL segment.
_PATH_SLUG_RE = re.compile(r"[a-z0-9]+(?:(?:_+|_*-_*)[a-z0-9]+)*", re.ASCII)
_PATH_SLUG_MAX_LENGTH = 255
_PATH_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_!-]*", re.ASCII)


def validate_path_slug(slug: object) -> str:
    """Return *slug* if it is a registry slug safe to put in a URL or a path."""
    if (
        not isinstance(slug, str)
        or len(slug) > _PATH_SLUG_MAX_LENGTH
        or not _PATH_SLUG_RE.fullmatch(slug)
    ):
        raise ValueError(
            f"Invalid path slug: {slug!r} (use lower-case letters, digits, '_' and "
            "'-', starting and ending with a letter or digit, no '--', at most "
            f"{_PATH_SLUG_MAX_LENGTH} characters)"
        )
    return slug


def validate_path_version(version: object) -> str:
    """Return *version* if it is a release version safe to put in a URL or a path."""
    if (
        not isinstance(version, str)
        or not _PATH_VERSION_RE.fullmatch(version)
        or ".." in version
        or not _is_valid_version(version)
    ):
        raise ValueError(f"Invalid path version: {version!r}")
    return version
