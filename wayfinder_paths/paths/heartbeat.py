from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.paths.client import PathsApiClient, PathsApiError

_LOCKFILE_NAME = "paths.lock.json"
_LEGACY_LOCKFILE_NAME = "packs.lock.json"
_STATE_FILENAME = "paths-heartbeat.json"
_LEGACY_STATE_FILENAME = "packs-heartbeat.json"
_DEFAULT_COOLDOWN = timedelta(hours=24)


@dataclass(frozen=True)
class PathHeartbeatResult:
    status: str
    reason: str
    attempted: int = 0
    sent: int = 0
    trigger: str = ""


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _find_wayfinder_dir(*, start: Path | None = None) -> Path | None:
    cur = (start or Path.cwd()).resolve()
    for parent in [cur, *cur.parents]:
        state_dir = parent / ".wayfinder"
        if (state_dir / _LOCKFILE_NAME).exists() or (
            state_dir / _LEGACY_LOCKFILE_NAME
        ).exists():
            return state_dir
    return None


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text()) or {}
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _has_explicit_paths_api_target() -> bool:
    system = CONFIG.get("system", {}) if isinstance(CONFIG, dict) else {}
    return bool(
        os.environ.get("WAYFINDER_PATHS_API_URL")
        or system.get("paths_api_base_url")
        or system.get("api_base_url")
    )


def _normalized_lock_paths(lock: dict[str, Any]) -> dict[str, Any]:
    paths = lock.get("paths")
    if isinstance(paths, dict):
        return paths

    legacy_paths = lock.get("packs")
    if isinstance(legacy_paths, dict):
        return legacy_paths

    return {}


def _load_lockfile(state_dir: Path) -> dict[str, Any]:
    lock_path = state_dir / _LOCKFILE_NAME
    if lock_path.exists():
        return _load_json(lock_path)
    return _load_json(state_dir / _LEGACY_LOCKFILE_NAME)


def _load_state(state_dir: Path) -> dict[str, Any]:
    state_path = state_dir / _STATE_FILENAME
    if state_path.exists():
        return _load_json(state_path)
    return _load_json(state_dir / _LEGACY_STATE_FILENAME)


def _collect_installed_path_heartbeats(lock: dict[str, Any]) -> list[dict[str, str]]:
    paths = _normalized_lock_paths(lock)

    heartbeats: list[dict[str, str]] = []
    for slug, entry in paths.items():
        if not isinstance(entry, dict):
            continue
        installation_id = str(entry.get("installation_id") or "").strip()
        heartbeat_token = str(entry.get("heartbeat_token") or "").strip()
        if not installation_id or not heartbeat_token:
            continue
        heartbeats.append(
            {
                "slug": str(slug).strip(),
                "installation_id": installation_id,
                "heartbeat_token": heartbeat_token,
                "status": "active",
            }
        )
    return heartbeats


def maybe_heartbeat_installed_paths(
    *,
    trigger: str,
    cwd: Path | None = None,
    cooldown: timedelta = _DEFAULT_COOLDOWN,
    client: PathsApiClient | None = None,
    now: datetime | None = None,
) -> PathHeartbeatResult:
    if not _has_explicit_paths_api_target():
        return PathHeartbeatResult(status="skipped", reason="paths_api_not_configured")

    state_dir = _find_wayfinder_dir(start=cwd)
    if state_dir is None:
        return PathHeartbeatResult(status="skipped", reason="lockfile_not_found")

    lock = _load_lockfile(state_dir)
    heartbeats = _collect_installed_path_heartbeats(lock)
    if not heartbeats:
        return PathHeartbeatResult(status="skipped", reason="no_installations")

    current_time = now or _now_utc()
    state_path = state_dir / _STATE_FILENAME
    state = _load_state(state_dir)
    last_success_at = _parse_timestamp(state.get("last_success_at"))
    if last_success_at and (current_time - last_success_at) < cooldown:
        return PathHeartbeatResult(
            status="skipped",
            reason="cooldown_active",
            attempted=len(heartbeats),
            trigger=trigger,
        )

    batch_client = client or PathsApiClient()
    try:
        response = batch_client.submit_batch_install_heartbeats(
            heartbeats=heartbeats,
            source=trigger,
        )
    except PathsApiError as exc:
        logger.debug("Installed-path heartbeat skipped after API error: {}", exc)
        return PathHeartbeatResult(
            status="error",
            reason="request_failed",
            attempted=len(heartbeats),
            trigger=trigger,
        )

    results = response.get("results")
    sent = 0
    if isinstance(results, list):
        sent = sum(
            1
            for item in results
            if isinstance(item, dict) and item.get("status") == "recorded"
        )

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "schemaVersion": "0.1",
                "last_success_at": current_time.isoformat(),
                "last_trigger": trigger,
                "attempted": len(heartbeats),
                "sent": sent,
            },
            indent=2,
        )
        + "\n"
    )
    return PathHeartbeatResult(
        status="recorded",
        reason="sent",
        attempted=len(heartbeats),
        sent=sent,
        trigger=trigger,
    )
