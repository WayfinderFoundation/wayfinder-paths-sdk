from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from wayfinder_paths.core.constants.hyperliquid import HYPE_FEE_WALLET
from wayfinder_paths.mcp.utils import (
    find_wallet_by_label,
    normalize_address,
    read_text_excerpt,
    repo_root,
)


async def build_execution_preview(tool_input: dict[str, Any]) -> dict[str, Any]:
    req = tool_input.get("request") if isinstance(tool_input, dict) else None
    if not isinstance(req, dict):
        return {
            "summary": "Execute request missing 'request' object.",
            "recipient_mismatch": False,
        }

    kind = str(req.get("kind") or "").strip()
    wallet_label = str(req.get("wallet_label") or "").strip()
    w = await find_wallet_by_label(wallet_label) if wallet_label else None
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


async def _hl_preview_base(req: dict[str, Any], header: str) -> tuple[str, str]:
    wallet_label = str(req.get("wallet_label") or "").strip()
    w = await find_wallet_by_label(wallet_label) if wallet_label else None
    sender = normalize_address((w or {}).get("address")) if w else None
    asset_name = req.get("asset_name")
    base_lines = [
        f"wallet_label: {wallet_label}",
        f"address: {sender or '(unknown)'}",
    ]
    if asset_name is not None:
        base_lines.append(f"asset_name: {asset_name}")
    return header, "\n".join(base_lines)


async def build_hyperliquid_place_order_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _hl_preview_base(tool_input, "HYPERLIQUID_PLACE_ORDER\n")
    details = (
        "\n\nORDER\n"
        f"order_type: {tool_input.get('order_type')}\n"
        f"is_buy: {tool_input.get('is_buy')}\n"
        f"size: {tool_input.get('size')}\n"
        f"usd_amount: {tool_input.get('usd_amount')}\n"
        f"usd_amount_kind: {tool_input.get('usd_amount_kind')}\n"
        f"price: {tool_input.get('price')}\n"
        f"slippage: {tool_input.get('slippage')}\n"
        f"reduce_only: {tool_input.get('reduce_only')}\n"
        f"cloid: {tool_input.get('cloid')}\n"
        f"leverage: {tool_input.get('leverage')}\n"
        f"is_cross: {tool_input.get('is_cross')}\n"
        f"builder_wallet: {HYPE_FEE_WALLET}"
    )
    return {"summary": header + base + details}


async def build_hyperliquid_place_trigger_order_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _hl_preview_base(
        tool_input, "HYPERLIQUID_PLACE_TRIGGER_ORDER\n"
    )
    tpsl_val = tool_input.get("tpsl")
    tpsl_label = "TAKE-PROFIT" if tpsl_val == "tp" else "STOP-LOSS"
    is_market_trigger = tool_input.get("is_market_trigger", True)
    trigger_kind = "market" if is_market_trigger else "limit"
    details = (
        f"\n\n{tpsl_label} ({trigger_kind} trigger)\n"
        f"tpsl: {tpsl_val}\n"
        f"is_buy: {tool_input.get('is_buy')}\n"
        f"trigger_price: {tool_input.get('trigger_price')}\n"
        f"size: {tool_input.get('size')}\n"
        f"is_market_trigger: {is_market_trigger}\n"
        f"limit_price: {tool_input.get('price')}\n"
        f"builder_wallet: {HYPE_FEE_WALLET}"
    )
    return {"summary": header + base + details}


async def build_hyperliquid_cancel_order_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _hl_preview_base(tool_input, "HYPERLIQUID_CANCEL_ORDER\n")
    details = (
        "\n\nCANCEL\n"
        f"order_id: {tool_input.get('order_id')}\n"
        f"cancel_cloid: {tool_input.get('cancel_cloid')}"
    )
    return {"summary": header + base + details}


async def build_hyperliquid_update_leverage_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _hl_preview_base(tool_input, "HYPERLIQUID_UPDATE_LEVERAGE\n")
    details = (
        "\n\nLEVERAGE\n"
        f"leverage: {tool_input.get('leverage')}\n"
        f"is_cross: {tool_input.get('is_cross')}"
    )
    return {"summary": header + base + details}


async def build_hyperliquid_deposit_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _hl_preview_base(tool_input, "HYPERLIQUID_DEPOSIT\n")
    details = f"\n\nDEPOSIT\namount_usdc: {tool_input.get('amount_usdc')}"
    return {"summary": header + base + details}


async def build_hyperliquid_withdraw_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _hl_preview_base(tool_input, "HYPERLIQUID_WITHDRAW\n")
    details = f"\n\nWITHDRAW\namount_usdc: {tool_input.get('amount_usdc')}"
    return {"summary": header + base + details}


async def build_polymarket_execute_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    req = tool_input if isinstance(tool_input, dict) else {}
    if not req:
        return {
            "summary": "POLYMARKET_EXECUTE missing parameters.",
            "recipient_mismatch": False,
        }

    action = str(req.get("action") or "").strip()
    wallet_label = str(req.get("wallet_label") or "").strip()
    w = await find_wallet_by_label(wallet_label) if wallet_label else None
    sender = normalize_address((w or {}).get("address")) if w else None

    header = "POLYMARKET_EXECUTE\n"
    base = (
        f"action: {action or '(missing)'}\n"
        f"wallet_label: {wallet_label or '(missing)'}\n"
        f"address: {sender or '(unknown)'}"
    )

    if action == "place_market_order":
        details = (
            "\n\nMARKET ORDER\n"
            f"market_slug: {req.get('market_slug')}\n"
            f"outcome: {req.get('outcome')}\n"
            f"token_id: {req.get('token_id')}\n"
            f"side: {req.get('side')}\n"
            f"amount_collateral: {req.get('amount_collateral')}\n"
            f"shares: {req.get('shares')}\n"
            f"max_slippage_pct: {req.get('max_slippage_pct')} (None = adapter default 2%)"
        )
        return {"summary": header + base + details, "recipient_mismatch": False}

    if action == "place_limit_order":
        details = (
            "\n\nLIMIT ORDER\n"
            f"token_id: {req.get('token_id')}\n"
            f"side: {req.get('side')}\n"
            f"price: {req.get('price')}\n"
            f"size: {req.get('size')}\n"
            f"post_only: {req.get('post_only')}"
        )
        return {"summary": header + base + details, "recipient_mismatch": False}

    if action == "cancel_order":
        details = f"\n\nCANCEL ORDER\norder_id: {req.get('order_id')}"
        return {"summary": header + base + details, "recipient_mismatch": False}

    if action == "redeem_positions":
        details = f"\n\nREDEEM\ncondition_id: {req.get('condition_id')}"
        return {"summary": header + base + details, "recipient_mismatch": False}

    return {"summary": header + base, "recipient_mismatch": False}


async def build_contract_execute_preview(tool_input: dict[str, Any]) -> dict[str, Any]:
    req = tool_input if isinstance(tool_input, dict) else {}
    if not req:
        return {"summary": "CONTRACT_EXECUTE missing parameters."}

    wallet_label = str(req.get("wallet_label") or "").strip()
    w = await find_wallet_by_label(wallet_label) if wallet_label else None
    sender = normalize_address((w or {}).get("address")) if w else None

    chain_id = req.get("chain_id")
    contract_address = normalize_address(req.get("contract_address"))
    fn = str(req.get("function_signature") or req.get("function_name") or "").strip()

    args = req.get("args")
    value_wei = req.get("value_wei")
    wait_for_receipt = req.get("wait_for_receipt", True)

    if req.get("abi_path"):
        abi_hint = f"abi_path: {req.get('abi_path')}"
    elif req.get("abi") is not None:
        abi_hint = "abi: (inline)"
    else:
        abi_hint = "abi: (missing)"

    summary = (
        "CONTRACT_EXECUTE\n"
        f"wallet_label: {wallet_label or '(missing)'}\n"
        f"sender: {sender or '(unknown)'}\n"
        f"chain_id: {chain_id}\n"
        f"contract_address: {contract_address or '(missing)'}\n"
        f"function: {fn or '(missing)'}\n"
        f"args: {args if args is not None else []}\n"
        f"value_wei: {value_wei if value_wei is not None else 0}\n"
        f"wait_for_receipt: {wait_for_receipt}\n"
        f"{abi_hint}"
    )
    return {"summary": summary}
