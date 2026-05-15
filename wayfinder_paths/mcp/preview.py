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


async def build_hyperliquid_execute_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    # hyperliquid_execute uses direct parameters, not a 'request' wrapper
    req = tool_input if isinstance(tool_input, dict) else {}
    if not req:
        return {"summary": "HYPERLIQUID_EXECUTE missing parameters."}

    action = str(req.get("action") or "").strip()
    wallet_label = str(req.get("wallet_label") or "").strip()
    w = await find_wallet_by_label(wallet_label) if wallet_label else None
    sender = normalize_address((w or {}).get("address")) if w else None

    asset_name = req.get("asset_name")
    asset_id = req.get("asset_id")

    header = "HYPERLIQUID_EXECUTE\n"
    base = (
        f"action: {action or '(missing)'}\n"
        f"wallet_label: {wallet_label}\n"
        f"address: {sender or '(unknown)'}\n"
        f"asset_name: {asset_name}\n"
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
            f"builder_wallet: {HYPE_FEE_WALLET}"
        )
        return {"summary": header + base + details}

    if action == "place_trigger_order":
        tpsl_val = req.get("tpsl")
        tpsl_label = "TAKE-PROFIT" if tpsl_val == "tp" else "STOP-LOSS"
        is_market_trigger = req.get("is_market_trigger", True)
        trigger_kind = "market" if is_market_trigger else "limit"
        details = (
            f"\n\n{tpsl_label} ({trigger_kind} trigger)\n"
            f"tpsl: {tpsl_val}\n"
            f"is_buy: {req.get('is_buy')}\n"
            f"trigger_price: {req.get('trigger_price')}\n"
            f"size: {req.get('size')}\n"
            f"is_market_trigger: {is_market_trigger}\n"
            f"limit_price: {req.get('price')}\n"
            f"builder_wallet: {HYPE_FEE_WALLET}"
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

    return {"summary": header + base}


async def _pm_preview_base(tool_input: dict[str, Any], header: str) -> tuple[str, str]:
    wallet_label = str(tool_input.get("wallet_label") or "").strip()
    w = await find_wallet_by_label(wallet_label) if wallet_label else None
    sender = normalize_address((w or {}).get("address")) if w else None
    return header, (
        f"wallet_label: {wallet_label or '(missing)'}\naddress: {sender or '(unknown)'}"
    )


async def build_polymarket_deposit_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _pm_preview_base(
        tool_input, "POLYMARKET_FUND_DEPOSIT_WALLET\n"
    )
    details = f"\n\nFUND DEPOSIT WALLET\namount (pUSD): {tool_input.get('amount')}"
    return {"summary": header + base + details}


async def build_polymarket_withdraw_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _pm_preview_base(
        tool_input, "POLYMARKET_WITHDRAW_DEPOSIT_WALLET\n"
    )
    amount = tool_input.get("amount")
    details = (
        "\n\nWITHDRAW DEPOSIT WALLET\n"
        f"amount (pUSD): {amount if amount is not None else '(drain full balance)'}"
    )
    return {"summary": header + base + details}


async def build_polymarket_place_market_order_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _pm_preview_base(tool_input, "POLYMARKET_PLACE_MARKET_ORDER\n")
    details = (
        "\n\nMARKET ORDER\n"
        f"market_slug: {tool_input.get('market_slug')}\n"
        f"outcome: {tool_input.get('outcome')}\n"
        f"token_id: {tool_input.get('token_id')}\n"
        f"side: {tool_input.get('side')}\n"
        f"amount_collateral: {tool_input.get('amount_collateral')}\n"
        f"shares: {tool_input.get('shares')}\n"
        f"max_slippage_pct: {tool_input.get('max_slippage_pct')} (None = adapter default 2%)"
    )
    return {"summary": header + base + details}


async def build_polymarket_place_limit_order_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _pm_preview_base(tool_input, "POLYMARKET_PLACE_LIMIT_ORDER\n")
    details = (
        "\n\nLIMIT ORDER\n"
        f"token_id: {tool_input.get('token_id')}\n"
        f"side: {tool_input.get('side')}\n"
        f"price: {tool_input.get('price')}\n"
        f"size: {tool_input.get('size')}\n"
        f"post_only: {tool_input.get('post_only')}"
    )
    return {"summary": header + base + details}


async def build_polymarket_cancel_order_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _pm_preview_base(tool_input, "POLYMARKET_CANCEL_ORDER\n")
    details = f"\n\nCANCEL ORDER\norder_id: {tool_input.get('order_id')}"
    return {"summary": header + base + details}


async def build_polymarket_redeem_positions_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _pm_preview_base(tool_input, "POLYMARKET_REDEEM_POSITIONS\n")
    details = f"\n\nREDEEM\ncondition_id: {tool_input.get('condition_id')}"
    return {"summary": header + base + details}


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
