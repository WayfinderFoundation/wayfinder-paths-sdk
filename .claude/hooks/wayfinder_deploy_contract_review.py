#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
from typing import Any


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
    if name not in {"mcp__wayfinder__deploy_contract", "deploy_contract"}:
        return

    ti = _tool_input(payload)

    wallet_label = ti.get("wallet_label", "(unknown)")
    source_path = ti.get("source_path")
    contract_name = ti.get("contract_name", "(unknown)")
    chain_id = ti.get("chain_id", "(unknown)")
    verify = ti.get("verify", True)

    source_line = (
        f"source_path: {source_path}"
        if source_path
        else "source_path: (inline source_code)"
    )

    summary = (
        "DEPLOY CONTRACT\n"
        f"wallet_label: {wallet_label}\n"
        f"{source_line}\n"
        f"contract_name: {contract_name}\n"
        f"chain_id: {chain_id}\n"
        f"verify: {verify}"
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
