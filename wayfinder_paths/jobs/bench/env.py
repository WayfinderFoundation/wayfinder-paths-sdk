"""Small deterministic filesystem/runtime helpers for benchmark runs."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from pathlib import Path
from typing import Any

from wayfinder_paths.runner.monitor_state import atomic_write_json


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    atomic_write_json(path, value, default=str)


def load_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return loaded


def sandbox_relative(value: Any, *, root: Path) -> Any:
    """Rewrite sandbox-absolute paths to the production-style relative form."""
    resolved = str(root.resolve())
    prefix = f"{resolved}/"
    if isinstance(value, dict):
        return {key: sandbox_relative(child, root=root) for key, child in value.items()}
    if isinstance(value, list):
        return [sandbox_relative(child, root=root) for child in value]
    if isinstance(value, str):
        return value.replace(prefix, "").replace(resolved, ".")
    return value


def git_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
