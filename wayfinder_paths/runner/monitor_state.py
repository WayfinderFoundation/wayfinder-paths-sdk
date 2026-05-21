from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from wayfinder_paths.runner.paths import get_runner_paths

_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._=-]+")


def _safe_segment(value: str | None, *, default: str) -> str:
    raw = str(value or "").strip() or default
    safe = _SAFE_SEGMENT_RE.sub("_", raw).strip("._")
    return safe or default


def monitor_state_path(name: str | None = None) -> Path:
    """Return a durable per-job monitor state path under the runner directory."""
    runner_dir = get_runner_paths().runner_dir
    namespace = _safe_segment(
        os.environ.get("WAYFINDER_KV_NAMESPACE")
        or os.environ.get("WAYFINDER_JOB_NAME"),
        default="default",
    )
    state_name = _safe_segment(name, default="state")
    if not state_name.endswith(".json"):
        state_name = f"{state_name}.json"
    return runner_dir / "job_state" / namespace / state_name


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write JSON to a durable state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=path.name,
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_monitor_state(
    name: str | None = None, default: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Read a monitor state object, returning `default` when it does not exist."""
    path = monitor_state_path(name)
    if not path.exists():
        return dict(default or {})
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"monitor state must be a JSON object: {path}")
    return payload


def write_monitor_state(
    name: str | dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> Path:
    """Write a monitor state object and return the path used."""
    if payload is None and isinstance(name, dict):
        payload = name
        name = None
    if payload is None:
        raise ValueError("payload is required")
    path = monitor_state_path(name if isinstance(name, str) else None)
    atomic_write_json(path, payload)
    return path
