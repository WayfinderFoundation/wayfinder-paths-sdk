from __future__ import annotations

from pathlib import Path

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
