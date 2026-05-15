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
    build_polymarket_cancel_order_preview,
    build_polymarket_fund_deposit_wallet_preview,
    build_polymarket_place_limit_order_preview,
    build_polymarket_place_market_order_preview,
    build_polymarket_redeem_positions_preview,
    build_polymarket_withdraw_deposit_wallet_preview,
)

_PreviewBuilder = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_BUILDERS: dict[str, _PreviewBuilder] = {
    "polymarket_fund_deposit_wallet": build_polymarket_fund_deposit_wallet_preview,
    "polymarket_withdraw_deposit_wallet": build_polymarket_withdraw_deposit_wallet_preview,
    "polymarket_place_market_order": build_polymarket_place_market_order_preview,
    "polymarket_place_limit_order": build_polymarket_place_limit_order_preview,
    "polymarket_cancel_order": build_polymarket_cancel_order_preview,
    "polymarket_redeem_positions": build_polymarket_redeem_positions_preview,
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
