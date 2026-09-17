"""Narrow, persistent migration of legacy hosted PublicNode overrides.

Kept independent of SDK imports so stage-one setup can load it before MCP starts.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

LEGACY_HOSTED_RPCS = {
    "1": "https://ethereum-rpc.publicnode.com",
    "8453": "https://base-rpc.publicnode.com",
    "42161": "https://arbitrum-one-rpc.publicnode.com",
}
_RETRY_AFTER = 60.0
_VALIDATION_TIMEOUT = 10.0
_lock = threading.Lock()
_failed_attempts: dict[tuple[Path, str], float] = {}


class LegacyRPCConfigurationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "Saved legacy RPC settings need recovery, but the managed gateway "
            "could not be validated. No settings were changed. Retry the read "
            "after one minute; if this persists, contact support. Do not edit "
            "config.json or repeat a submitted transaction."
        )


def _legacy_override(chain: str, value: Any) -> bool:
    expected = LEGACY_HOSTED_RPCS.get(chain)
    return expected is not None and (value == expected or value == [expected])


def _validate_gateway(config: dict[str, Any], chain: str, deadline: float) -> None:
    import httpx

    system = config.get("system", {})
    base = system.get("api_base_url", "https://wayfinder.ai/api/v1").rstrip("/")
    key = system.get("api_key") or os.environ.get("WAYFINDER_API_KEY")
    if not key:
        raise LegacyRPCConfigurationError()

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Managed RPC validation timed out")
        # HTTPX budgets connect/read/write/pool phases separately.
        return seconds / 4

    with httpx.Client(headers={"X-API-KEY": key}, follow_redirects=False) as client:
        url = f"{base}/blockchain/rpc/{chain}"
        response = client.get(f"{url}/count/", timeout=remaining())
        response.raise_for_status()
        if int(response.json()["size"]) < 1:
            raise LegacyRPCConfigurationError()
        for method in ("eth_chainId", "eth_blockNumber"):
            response = client.post(
                f"{url}/0/",
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": []},
                timeout=remaining(),
            )
            response.raise_for_status()
            value = int(response.json()["result"], 16)
            if (method == "eth_chainId" and value != int(chain)) or value <= 0:
                raise LegacyRPCConfigurationError()


def _replace_config(path: Path, original: bytes, config: dict[str, Any]) -> None:
    metadata = path.stat()
    # Backups contain API credentials; never inherit a permissive source mode.
    backup_fd, _ = tempfile.mkstemp(
        prefix=f"{path.name}.before-managed-rpc-", suffix=".bak", dir=path.parent
    )
    with os.fdopen(backup_fd, "wb") as backup:
        backup.write(original)
        backup.flush()
        os.fsync(backup.fileno())
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode))
            os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
            json.dump(config, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def recover_legacy_rpc_overrides(
    config: dict[str, Any], config_path: Path, *, chain_id: int | None = None
) -> None:
    """Repair exact legacy settings only; raise a safe diagnostic if unavailable.

    Custom/fork overrides (including process-local ones) are not migrated. The
    persistent file and caller's in-memory mapping must both match the legacy value.
    """
    if not os.environ.get("OPENCODE_INSTANCE_ID"):
        return
    import fcntl

    mapping = config.get("strategy", {}).get("rpc_urls", {})
    candidates = [
        chain
        for chain in LEGACY_HOSTED_RPCS
        if (chain_id is None or str(chain_id) == chain)
        and _legacy_override(chain, mapping.get(chain))
    ]
    if not candidates:
        return
    path = config_path.resolve()
    with _lock:
        try:
            with path.with_name(f".{path.name}.rpc-recovery.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                original = path.read_bytes()
                saved = json.loads(original)
                saved_mapping = saved.get("strategy", {}).get("rpc_urls", {})
                # Another process may have completed the same migration already.
                for chain in candidates:
                    if chain not in saved_mapping:
                        mapping.pop(chain, None)
                candidates = [
                    chain
                    for chain in candidates
                    if _legacy_override(chain, saved_mapping.get(chain))
                ]
                if not candidates:
                    return
                now = time.monotonic()
                if any(
                    now - _failed_attempts.get((path, chain), -float("inf"))
                    < _RETRY_AFTER
                    for chain in candidates
                ):
                    raise LegacyRPCConfigurationError()
                deadline = now + _VALIDATION_TIMEOUT
                for chain in candidates:
                    _failed_attempts[(path, chain)] = now
                    _validate_gateway(saved, chain, deadline)
                if path.read_bytes() != original:
                    raise LegacyRPCConfigurationError()
                for chain in candidates:
                    saved_mapping.pop(chain)
                _replace_config(path, original, saved)
                for chain in candidates:
                    mapping.pop(chain, None)
                    _failed_attempts.pop((path, chain), None)
        except LegacyRPCConfigurationError:
            # Preserve the original retry deadline, even under repeated tool calls.
            for chain in candidates:
                _failed_attempts.setdefault((path, chain), time.monotonic())
            raise
        except Exception as exc:
            # HTTP libraries are lazy-loaded for the hosted-only migration. Never
            # expose their exception text: RPC URLs can contain credentials.
            for chain in candidates:
                _failed_attempts[(path, chain)] = time.monotonic()
            raise LegacyRPCConfigurationError() from exc
