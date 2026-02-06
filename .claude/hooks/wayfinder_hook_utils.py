#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    # `.claude/hooks/<this file>` -> repo root
    return Path(__file__).resolve().parents[2]


def load_payload() -> dict[str, Any]:
    try:
        obj = json.load(sys.stdin)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def tool_name(payload: dict[str, Any]) -> str | None:
    return clean_str(payload.get("tool_name") or payload.get("name"))


def tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    ti = payload.get("tool_input") or payload.get("input") or {}
    return ti if isinstance(ti, dict) else {}


def sanitize_session_id(session_id: str) -> str:
    # Session IDs are used as folder names; avoid path traversal or weird chars.
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", session_id.strip())
    safe = safe.strip("._-") or "unknown"
    return safe[:120]


def find_session_id(
    payload: dict[str, Any], *, env_keys: tuple[str, ...]
) -> str | None:
    for key in ("session_id", "sessionId"):
        raw = clean_str(payload.get(key))
        if raw:
            return sanitize_session_id(raw)

    session_obj = payload.get("session")
    if isinstance(session_obj, dict):
        for key in ("id", "session_id", "sessionId"):
            raw = clean_str(session_obj.get(key))
            if raw:
                return sanitize_session_id(raw)

    for key in env_keys:
        raw = clean_str(os.getenv(key, ""))
        if raw:
            return sanitize_session_id(raw)

    return None


def runs_root(repo_root: Path) -> Path:
    candidate = (os.getenv("WAYFINDER_RUNS_DIR") or ".wayfinder_runs").strip()
    p = Path(candidate)
    if not p.is_absolute():
        p = repo_root / p
    return p.resolve(strict=False)


def resolve_repo_path(repo_root: Path, raw_path: str) -> Path:
    p = Path(raw_path)
    if not p.is_absolute():
        p = repo_root / p
    return p.resolve(strict=False)


def rel_display(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def sh_quote(value: str) -> str:
    # Safe for: `export KEY='value'` in sh-compatible shells.
    return "'" + value.replace("'", "'\"'\"'") + "'"


def append_env_exports(env_file: Path, exports: dict[str, str]) -> None:
    env_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"export {k}={sh_quote(v)}\n" for k, v in exports.items()]
    with env_file.open("a", encoding="utf-8") as f:
        f.writelines(lines)
