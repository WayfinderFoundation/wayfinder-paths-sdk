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
    build_hyperliquid_cancel_order_preview,
    build_hyperliquid_deposit_preview,
    build_hyperliquid_place_limit_order_preview,
    build_hyperliquid_place_market_order_preview,
    build_hyperliquid_place_trigger_order_preview,
    build_hyperliquid_update_leverage_preview,
    build_hyperliquid_withdraw_preview,
)

_PreviewBuilder = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_BUILDERS: dict[str, _PreviewBuilder] = {
    "hyperliquid_place_market_order": build_hyperliquid_place_market_order_preview,
    "hyperliquid_place_limit_order": build_hyperliquid_place_limit_order_preview,
    "hyperliquid_place_trigger_order": build_hyperliquid_place_trigger_order_preview,
    "hyperliquid_cancel_order": build_hyperliquid_cancel_order_preview,
    "hyperliquid_update_leverage": build_hyperliquid_update_leverage_preview,
    "hyperliquid_deposit": build_hyperliquid_deposit_preview,
    "hyperliquid_withdraw": build_hyperliquid_withdraw_preview,
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
