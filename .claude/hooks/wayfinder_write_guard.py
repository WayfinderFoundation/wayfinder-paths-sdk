#!/usr/bin/env python3

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import wayfinder_hook_utils as hook_utils


def _extract_paths(tool_input: dict[str, Any]) -> list[str]:
    out: set[str] = set()

    def add(path: Any) -> None:
        if isinstance(path, str) and path.strip():
            out.add(path.strip())

    for key in ("file_path", "path", "filepath", "filePath"):
        add(tool_input.get(key))

    maybe_paths = tool_input.get("paths")
    if isinstance(maybe_paths, list):
        for p in maybe_paths:
            add(p)

    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for e in edits:
            if isinstance(e, dict):
                for key in ("file_path", "path", "filepath", "filePath"):
                    add(e.get(key))

    files = tool_input.get("files")
    if isinstance(files, list):
        for f in files:
            if isinstance(f, dict):
                for key in ("file_path", "path", "filepath", "filePath"):
                    add(f.get(key))

    return sorted(out)


def _resolve_paths(repo_root: Path, raw_paths: Iterable[str]) -> list[Path]:
    resolved: list[Path] = []
    for raw in raw_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = repo_root / p
        resolved.append(p.resolve(strict=False))
    return resolved


def _is_allowed_runs_write(runs_root: Path, path: Path) -> bool:
    readme = (runs_root / "README.md").resolve(strict=False)
    scratch_root = (runs_root / ".scratch").resolve(strict=False)
    library_root = (runs_root / "library").resolve(strict=False)

    if path == readme:
        return True

    if path.is_relative_to(scratch_root):
        return True

    if path.is_relative_to(library_root):
        return True

    return False


def main() -> None:
    payload = hook_utils.load_payload()
    name = hook_utils.tool_name(payload)
    if name not in {
        "Write",
        "Edit",
        "MultiEdit",
        "WriteTool",
        "EditTool",
        "MultiEditTool",
    }:
        return

    tool_input = hook_utils.tool_input(payload)
    raw_paths = _extract_paths(tool_input)
    if not raw_paths:
        return

    repo_root = hook_utils.repo_root()
    runs_root = hook_utils.runs_root(repo_root)
    runs_root_str = str(runs_root)

    resolved_paths = _resolve_paths(repo_root, raw_paths)
    for p in resolved_paths:
        if not p.is_relative_to(runs_root):
            continue

        if _is_allowed_runs_write(runs_root, p):
            continue

        decision = os.getenv("WAYFINDER_WRITE_GUARD_MODE", "deny").strip().lower()
        if decision not in {"ask", "deny"}:
            decision = "deny"

        try:
            p_display = str(p.relative_to(repo_root))
        except ValueError:
            p_display = str(p)

        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": (
                    "Keep `.wayfinder_runs/` clean: write session scripts into "
                    "`$WAYFINDER_SCRATCH_DIR` (auto-deleted on SessionEnd) or promote "
                    "keepers into `$WAYFINDER_LIBRARY_DIR/<protocol>/...`. "
                    f"Blocked: `{p_display}` (runs_root={runs_root_str})."
                ),
            }
        }
        print(json.dumps(out))
        return

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
