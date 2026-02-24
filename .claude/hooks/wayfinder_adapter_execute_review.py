#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wayfinder_paths.mcp.preview import build_adapter_execute_preview


def _load_payload() -> dict[str, Any]:
    try:
        obj = json.load(sys.stdin)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _tool_name(payload: dict[str, Any]) -> str | None:
    name = payload.get("tool_name") or payload.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    ti = payload.get("tool_input") or payload.get("input") or {}
    return ti if isinstance(ti, dict) else {}


def main() -> None:
    payload = _load_payload()
    name = _tool_name(payload)
    if name not in {"mcp__wayfinder__adapter_execute", "adapter_execute"}:
        return

    tool_input = _tool_input(payload)
    preview = build_adapter_execute_preview(tool_input)
    summary = (
        str(preview.get("summary") or "").strip() or "Review adapter_execute() request."
    )

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": summary,
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
