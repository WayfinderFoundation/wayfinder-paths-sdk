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


def _resolve_builder(name: str) -> _PreviewBuilder | None:
    short = name.removeprefix("mcp__wayfinder__")
    return _BUILDERS.get(short)


async def main() -> None:
    payload = _load_payload()
    name = _tool_name(payload)
    if not name:
        return
    builder = _resolve_builder(name)
    if builder is None:
        return

    preview = await builder(_tool_input(payload))
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
