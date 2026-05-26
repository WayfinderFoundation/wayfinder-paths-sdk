"""Shells-mode bridge: push lockfile state to vault-backend so the Shells UI's
per-machine inventory matches disk in real time. No-op outside an OpenCode
instance, mirroring the `wallets.load_remote_wallets` pattern.

Called by `wayfinder path install` and `wayfinder path activate` after each
modifies `.wayfinder/paths.lock.json`. The BE-side polling daemon still acts
as the catch-all (uninstall, external edits); this path is the zero-latency
fast lane for the most common mutations.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from wayfinder_paths.core.config import (
    get_opencode_instance_id,
    is_opencode_instance,
)
from wayfinder_paths.paths.client import PathsApiClient, PathsApiError
from wayfinder_paths.paths.heartbeat import (
    _find_wayfinder_dir,
    _load_lockfile,
    _normalized_lock_paths,
)

_HOST = "opencode"


@dataclass(frozen=True)
class ShellsInventorySyncResult:
    status: str
    reason: str
    attempted: int = 0
    upserted: int = 0
    deleted: int = 0
    trigger: str = ""


def _collect(lock: dict[str, Any]) -> list[dict[str, Any]]:
    """Lockfile → BE payload. Must stay in sync with the polling daemon's
    collector at vault-backend `services/scripts/inventory_sync_daemon.py`."""
    items: list[dict[str, Any]] = []
    for slug, entry in _normalized_lock_paths(lock).items():
        if not isinstance(entry, dict):
            continue
        version = str(entry.get("version") or "").strip()
        if not version:
            continue
        activation = entry.get("activation")
        enabled = (
            isinstance(activation, dict)
            and str(activation.get("host") or "").strip() == _HOST
        )
        items.append(
            {
                "slug": str(slug).strip(),
                "version": version,
                "host": _HOST,
                "enabled": enabled,
            }
        )
    return items


def sync_shells_inventory(
    *,
    trigger: str,
    cwd: Path | None = None,
    client: PathsApiClient | None = None,
) -> ShellsInventorySyncResult:
    """Self-gated: returns immediately when not inside a Fly OpenCode instance,
    so local CLI users / non-Shells consumers pay zero cost. Errors are caught
    and returned via the result; never raised."""
    if not is_opencode_instance():
        return ShellsInventorySyncResult(
            status="skipped", reason="not_in_opencode_instance", trigger=trigger
        )

    try:
        app_name = get_opencode_instance_id()
    except RuntimeError:
        return ShellsInventorySyncResult(
            status="skipped", reason="missing_instance_id", trigger=trigger
        )

    state_dir = _find_wayfinder_dir(start=cwd)
    lockfile_present = state_dir is not None
    paths = _collect(_load_lockfile(state_dir)) if state_dir else []

    sync_client = client or PathsApiClient()
    try:
        response = sync_client.submit_shells_inventory_sync(
            app_name=app_name,
            lockfile_present=lockfile_present,
            paths=paths,
        )
    except PathsApiError as exc:
        logger.debug("Shells inventory sync skipped after API error: {}", exc)
        return ShellsInventorySyncResult(
            status="error",
            reason="request_failed",
            attempted=len(paths),
            trigger=trigger,
        )

    return ShellsInventorySyncResult(
        status="recorded",
        reason="sent",
        attempted=len(paths),
        upserted=int(response.get("upserted") or 0),
        deleted=int(response.get("deleted") or 0),
        trigger=trigger,
    )
