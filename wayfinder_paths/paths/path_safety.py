from __future__ import annotations

from pathlib import Path

APPROVED_RUNTIME_PACKAGE = "wayfinder-paths"


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
