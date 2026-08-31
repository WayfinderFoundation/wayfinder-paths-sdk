"""Containment checks for job-owned evidence artifacts."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path


def verify_job_evidence_refs(
    root: Path,
    refs: Iterable[str],
    *,
    allowed_roots: Sequence[str],
    now: datetime | None = None,
    max_age: timedelta | None = None,
) -> list[str]:
    """Return canonical refs to existing files inside approved job subtrees."""
    job_root = root.resolve()
    allowed = [(job_root / relative).resolve() for relative in allowed_roots]
    current = _aware(now or datetime.now(UTC))
    verified: list[str] = []
    for value in refs:
        relative = Path(str(value))
        if relative.is_absolute():
            continue
        path = (job_root / relative).resolve()
        if not any(path.is_relative_to(base) for base in allowed):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        if max_age is not None:
            age = current - datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            if age > max_age:
                continue
        canonical = path.relative_to(job_root).as_posix()
        if canonical not in verified:
            verified.append(canonical)
    return verified


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
