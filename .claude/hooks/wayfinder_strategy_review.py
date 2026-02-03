#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    # .claude/hooks/<this file> -> repo root
    return Path(__file__).resolve().parents[2]


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


# Read-only actions that don't require confirmation
READ_ONLY_ACTIONS = {"status", "analyze", "snapshot", "policy", "quote"}

# Fund-moving actions that require confirmation
FUND_MOVING_ACTIONS = {"deposit", "update", "withdraw", "exit"}


def main() -> None:
    sys.path.insert(0, str(_repo_root()))

    payload = _load_payload()
    name = _tool_name(payload)
    if name not in {"mcp__wayfinder__run_strategy", "run_strategy"}:
        return

    tool_input = _tool_input(payload)
    action = str(tool_input.get("action", "")).strip()
    strategy = str(tool_input.get("strategy", "")).strip() or "(unknown)"

    # Read-only actions pass through without confirmation
    if action in READ_ONLY_ACTIONS:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }
        print(json.dumps(out))
        return

    # Fund-moving actions require confirmation
    if action in FUND_MOVING_ACTIONS:
        amount = tool_input.get("amount")

        if action == "deposit":
            if amount is not None:
                summary = f"Deposit ${amount:.2f} into {strategy}"
            else:
                summary = f"Deposit into {strategy} (amount not specified)"
        elif action == "update":
            summary = f"Update/rebalance {strategy}"
        elif action == "withdraw":
            if amount is not None:
                summary = f"Withdraw ${amount:.2f} from {strategy}"
            else:
                summary = f"Full withdrawal from {strategy} (close all positions)"
        elif action == "exit":
            summary = f"Transfer remaining balances from {strategy} to main wallet"
        else:
            summary = f"{action.capitalize()} on {strategy}"

        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": summary,
            }
        }
        print(json.dumps(out))
        return

    # Unknown action - ask for confirmation
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": f"Unknown action '{action}' on {strategy}",
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
