#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

# Add repo root to path for wayfinder_paths imports
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wayfinder_hook_utils import load_payload, tool_input, tool_name

from wayfinder_paths.mcp.preview import (
    build_onchain_send_preview,
    build_onchain_swap_preview,
)

_PreviewBuilder = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_BUILDERS: dict[str, _PreviewBuilder] = {
    "onchain_swap": build_onchain_swap_preview,
    "onchain_send": build_onchain_send_preview,
}


async def main() -> None:
    payload = load_payload()
    name = tool_name(payload)
    if not name:
        return
    builder = _BUILDERS.get(name.removeprefix("mcp__wayfinder__"))
    if builder is None:
        return

    preview = await builder(tool_input(payload))
    summary = str(preview.get("summary") or "").strip() or f"Review {name} request."
    if preview.get("recipient_mismatch"):
        summary = "⚠ RECIPIENT DIFFERS FROM SENDER\n" + summary

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": summary,
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    asyncio.run(main())
