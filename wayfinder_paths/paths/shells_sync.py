"""Shells-mode bridge: push lockfile state to vault-backend so the Shells UI's
per-machine inventory matches disk in real time. No-op outside an OpenCode
instance, mirroring the `wallets.load_remote_wallets` pattern.

Called by path CLI mutations after they modify `.wayfinder/paths.lock.json`.
The caller reports the runtime effect so vault-backend can reconcile disk state
without guessing whether OpenCode needs to reload.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

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
_RUNTIME_RELOAD_INTENT_ENV = "WAYFINDER_SHELLS_RUNTIME_RELOAD_INTENT"
_SKIP_SYNC_ENV = "WAYFINDER_SKIP_SHELLS_INVENTORY_SYNC"
ShellsRuntimeReloadIntent = Literal["fresh", "preserve", "restart"]


@dataclass(frozen=True)
class ShellsInventorySyncResult:
    status: str
    reason: str
    attempted: int = 0
    upserted: int = 0
    deleted: int = 0
    trigger: str = ""
    runtime_reload_intent: ShellsRuntimeReloadIntent = "preserve"
    runtime_reload_status: str = ""


def _runtime_reload_intent(
    requested: ShellsRuntimeReloadIntent,
) -> ShellsRuntimeReloadIntent:
    override = os.getenv(_RUNTIME_RELOAD_INTENT_ENV, "").strip().lower()
    if override in {"fresh", "preserve", "restart"}:
        return cast(ShellsRuntimeReloadIntent, override)
    return requested


def _sync_suppressed() -> bool:
    return os.getenv(_SKIP_SYNC_ENV, "").strip().lower() in {"1", "true", "yes"}


def _collect(lock: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert the canonical or legacy install lock to the backend payload."""
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
    runtime_reload_intent: ShellsRuntimeReloadIntent = "preserve",
    changed_slugs: list[str] | None = None,
    cwd: Path | None = None,
    client: PathsApiClient | None = None,
) -> ShellsInventorySyncResult:
    """Self-gated: returns immediately when not inside a Fly OpenCode instance,
    so local CLI users / non-Shells consumers pay zero cost. Errors are caught
    and returned via the result; never raised."""
    reload_intent = _runtime_reload_intent(runtime_reload_intent)
    if _sync_suppressed():
        return ShellsInventorySyncResult(
            status="skipped",
            reason="suppressed",
            trigger=trigger,
            runtime_reload_intent=reload_intent,
        )

    if not is_opencode_instance():
        return ShellsInventorySyncResult(
            status="skipped",
            reason="not_in_opencode_instance",
            trigger=trigger,
            runtime_reload_intent=reload_intent,
        )

    try:
        app_name = get_opencode_instance_id()
    except RuntimeError:
        return ShellsInventorySyncResult(
            status="skipped",
            reason="missing_instance_id",
            trigger=trigger,
            runtime_reload_intent=reload_intent,
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
            trigger=trigger,
            source={"fresh": "boot", "preserve": "managed", "restart": "direct"}[
                reload_intent
            ],
            runtime_reload_intent=reload_intent,
            changed_slugs=changed_slugs or [],
        )
    except PathsApiError as exc:
        logger.debug("Shells inventory sync skipped after API error: {}", exc)
        return ShellsInventorySyncResult(
            status="error",
            reason="request_failed",
            attempted=len(paths),
            trigger=trigger,
            runtime_reload_intent=reload_intent,
        )

    runtime_reload_status = str(response.get("runtime_reload_status") or "")
    if not runtime_reload_status and response.get("runtime_restarted") is True:
        runtime_reload_status = "restarted"

    return ShellsInventorySyncResult(
        status="recorded",
        reason="sent",
        attempted=len(paths),
        upserted=int(response.get("upserted") or 0),
        deleted=int(response.get("deleted") or 0),
        trigger=trigger,
        runtime_reload_intent=reload_intent,
        runtime_reload_status=runtime_reload_status,
    )
