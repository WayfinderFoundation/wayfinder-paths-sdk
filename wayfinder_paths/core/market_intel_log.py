"""Append-only market intelligence log helpers for Wayfinder scripts."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_LOG_NAME = "market_intel_log.jsonl"
SCHEMA_VERSION = "wf.market_intel_log.v1"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _runs_dir() -> Path:
    return Path(os.getenv("WAYFINDER_RUNS_DIR") or ".wayfinder_runs")


def _log_path(path: str | Path | None = None) -> Path:
    if path is None:
        return _runs_dir() / DEFAULT_LOG_NAME
    candidate = Path(path)
    if candidate.suffix:
        return candidate
    return candidate / DEFAULT_LOG_NAME


def append_log(entry: dict[str, Any], path: str | Path | None = None) -> dict[str, Any]:
    """Append a market intelligence log entry and return the persisted entry."""
    log_path = _log_path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    persisted = dict(entry)
    persisted.setdefault("schemaVersion", SCHEMA_VERSION)
    persisted.setdefault(
        "id", f"wf-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    )
    persisted.setdefault("createdAt", _utc_now())
    persisted.setdefault("safeToReuseWithoutRehydration", False)
    persisted.setdefault("mustRehydrate", [])
    persisted.setdefault("artifactRefs", [])
    persisted.setdefault("sources", [])
    persisted.setdefault("outcome", None)

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(persisted, sort_keys=True, separators=(",", ":")))
        handle.write("\n")
    return persisted


def _subject_matches(entry: dict[str, Any], subject: dict[str, Any] | None) -> bool:
    if not subject:
        return True
    entry_subject = entry.get("subject") or {}
    return all(entry_subject.get(key) == value for key, value in subject.items())


def search_log(
    subject: dict[str, Any] | None = None,
    kind: str | None = None,
    limit: int = 20,
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return recent matching log entries, newest first."""
    log_path = _log_path(path)
    if not log_path.exists():
        return []

    matches: list[dict[str, Any]] = []
    with log_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            entry = json.loads(line)
            if kind is not None and entry.get("kind") != kind:
                continue
            if not _subject_matches(entry, subject):
                continue
            matches.append(entry)

    capped_limit = max(int(limit), 0)
    if capped_limit == 0:
        return []
    return list(reversed(matches[-capped_limit:]))


def latest_for_subject(
    subject: dict[str, Any],
    kind: str | None = None,
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return the newest entry matching a subject and optional kind."""
    matches = search_log(subject=subject, kind=kind, limit=1, path=path)
    return matches[0] if matches else None


def update_outcome(
    entry_id: str,
    outcome: dict[str, Any],
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Append an outcome update linked to a prior log entry."""
    prior = next(
        (
            entry
            for entry in search_log(limit=10_000, path=path)
            if entry.get("id") == entry_id
        ),
        None,
    )
    outcome_entry = {
        "producer": "wayfinder",
        "kind": "outcome_update",
        "subject": (prior or {}).get("subject", {}),
        "observedAt": _utc_now(),
        "summary": f"Outcome update for {entry_id}",
        "outcome": {"entryId": entry_id, **outcome},
    }
    return append_log(outcome_entry, path=path)


def freshness_check(
    entry: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return freshness metadata for a log entry."""
    checked_at = now or datetime.now(UTC)
    expires_at = _parse_time(entry.get("expiresAt"))
    expired = expires_at is None or expires_at <= checked_at
    safe_without_rehydration = bool(entry.get("safeToReuseWithoutRehydration"))
    return {
        "isFresh": not expired,
        "expired": expired,
        "safeToReuseWithoutRehydration": safe_without_rehydration and not expired,
        "mustRehydrate": list(entry.get("mustRehydrate") or []),
        "reuseMode": "audit_only" if expired else "assumption_seed",
        "checkedAt": checked_at.replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }
