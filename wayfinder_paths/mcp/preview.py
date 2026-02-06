from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from wayfinder_paths.core.constants.hyperliquid import (
    ARBITRUM_USDC_TOKEN_ID,
    HYPE_FEE_WALLET,
    HYPERLIQUID_BRIDGE_ADDRESS,
)
from wayfinder_paths.mcp.utils import (
    find_wallet_by_label,
    normalize_address,
    read_text_excerpt,
    repo_root,
)


def build_execution_preview(tool_input: dict[str, Any]) -> dict[str, Any]:
    req = tool_input.get("request") if isinstance(tool_input, dict) else None
    if not isinstance(req, dict):
        return {
            "summary": "Execute request missing 'request' object.",
            "recipient_mismatch": False,
        }

    kind = str(req.get("kind") or "").strip()
    wallet_label = str(req.get("wallet_label") or "").strip()
    w = find_wallet_by_label(wallet_label) if wallet_label else None
    sender = normalize_address((w or {}).get("address")) if w else None

    recipient = normalize_address(req.get("recipient"))
    if kind == "swap":
        recipient = recipient or sender
        summary = (
            "EXECUTE swap\n"
            f"wallet_label: {wallet_label}\n"
            f"from_token: {req.get('from_token')}\n"
            f"to_token: {req.get('to_token')}\n"
            f"amount: {req.get('amount')}\n"
            f"slippage_bps: {req.get('slippage_bps')}\n"
            f"sender: {sender or '(unknown)'}\n"
            f"recipient: {recipient or '(unknown)'}"
        )
    elif kind == "hyperliquid_deposit":
        recipient = normalize_address(HYPERLIQUID_BRIDGE_ADDRESS)
        summary = (
            "EXECUTE hyperliquid_deposit (Bridge2)\n"
            f"wallet_label: {wallet_label}\n"
            f"token: {ARBITRUM_USDC_TOKEN_ID}\n"
            f"amount: {req.get('amount')}\n"
            "chain_id: 42161\n"
            f"sender: {sender or '(unknown)'}\n"
            f"recipient: {recipient or '(missing)'}"
        )
    elif kind == "send":
        summary = (
            "EXECUTE send\n"
            f"wallet_label: {wallet_label}\n"
            f"token: {req.get('token')}\n"
            f"amount: {req.get('amount')}\n"
            f"chain_id: {req.get('chain_id')}\n"
            f"sender: {sender or '(unknown)'}\n"
            f"recipient: {recipient or '(missing)'}"
        )
    else:
        summary = f"EXECUTE {kind or '(unknown kind)'}\nwallet_label: {wallet_label}"

    mismatch = bool(sender and recipient and sender.lower() != recipient.lower())
    if kind == "hyperliquid_deposit":
        mismatch = False  # deposit recipient is fixed; mismatch is expected
    return {"summary": summary, "recipient_mismatch": mismatch}


def build_run_script_preview(tool_input: dict[str, Any]) -> dict[str, Any]:
    ti = tool_input if isinstance(tool_input, dict) else {}
    path_raw = ti.get("script_path") or ti.get("path")
    args = ti.get("args") if isinstance(ti.get("args"), list) else []

    if not isinstance(path_raw, str) or not path_raw.strip():
        return {"summary": "RUN_SCRIPT missing script_path."}

    root = repo_root()
    p = Path(path_raw)
    if not p.is_absolute():
        p = root / p
    resolved = p.resolve(strict=False)

    rel = str(resolved)
    try:
        rel = str(resolved.relative_to(root))
    except Exception:
        pass

    sha = None
    try:
        if resolved.exists():
            sha = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except Exception:
        sha = None

    excerpt = read_text_excerpt(resolved, max_chars=1200) if resolved.exists() else None

    summary = (
        "RUN_SCRIPT (executes local python)\n"
        f"script_path: {rel}\n"
        f"args: {args or []}\n"
        f"script_sha256: {(sha[:12] + '…') if sha else '(unavailable)'}"
    )
    if excerpt:
        summary += "\n\n" + excerpt
    else:
        summary += "\n\n(no script contents available)"

    return {"summary": summary}


def build_hyperliquid_execute_preview(tool_input: dict[str, Any]) -> dict[str, Any]:
    # hyperliquid_execute uses direct parameters, not a 'request' wrapper
    req = tool_input if isinstance(tool_input, dict) else {}
    if not req:
        return {"summary": "HYPERLIQUID_EXECUTE missing parameters."}

    action = str(req.get("action") or "").strip()
    wallet_label = str(req.get("wallet_label") or "").strip()
    w = find_wallet_by_label(wallet_label) if wallet_label else None
    sender = normalize_address((w or {}).get("address")) if w else None

    coin = req.get("coin")
    asset_id = req.get("asset_id")

    header = "HYPERLIQUID_EXECUTE\n"
    base = (
        f"action: {action or '(missing)'}\n"
        f"wallet_label: {wallet_label}\n"
        f"address: {sender or '(unknown)'}\n"
        f"coin: {coin}\n"
        f"asset_id: {asset_id}"
    )

    if action == "place_order":
        details = (
            "\n\nORDER\n"
            f"order_type: {req.get('order_type')}\n"
            f"is_buy: {req.get('is_buy')}\n"
            f"size: {req.get('size')}\n"
            f"usd_amount: {req.get('usd_amount')}\n"
            f"usd_amount_kind: {req.get('usd_amount_kind')}\n"
            f"price: {req.get('price')}\n"
            f"slippage: {req.get('slippage')}\n"
            f"reduce_only: {req.get('reduce_only')}\n"
            f"cloid: {req.get('cloid')}\n"
            f"leverage: {req.get('leverage')}\n"
            f"is_cross: {req.get('is_cross')}\n"
            f"builder_wallet: {HYPE_FEE_WALLET}\n"
            f"builder_fee_tenths_bp: {req.get('builder_fee_tenths_bp') or '(from config/default)'}"
        )
        return {"summary": header + base + details}

    if action == "cancel_order":
        details = (
            "\n\nCANCEL\n"
            f"order_id: {req.get('order_id')}\n"
            f"cancel_cloid: {req.get('cancel_cloid')}"
        )
        return {"summary": header + base + details}

    if action == "update_leverage":
        details = (
            "\n\nLEVERAGE\n"
            f"leverage: {req.get('leverage')}\n"
            f"is_cross: {req.get('is_cross')}"
        )
        return {"summary": header + base + details}

    if action == "withdraw":
        details = f"\n\nWITHDRAW\namount_usdc: {req.get('amount_usdc')}"
        return {"summary": header + base + details}

    if action == "spot_to_perp_transfer":
        details = f"\n\nTRANSFER SPOT → PERP\nusd_amount: {req.get('usd_amount')}"
        return {"summary": header + base + details}

    if action == "perp_to_spot_transfer":
        details = f"\n\nTRANSFER PERP → SPOT\nusd_amount: {req.get('usd_amount')}"
        return {"summary": header + base + details}

    return {"summary": header + base}
