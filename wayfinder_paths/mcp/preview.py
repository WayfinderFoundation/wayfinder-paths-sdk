from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from wayfinder_paths.core.constants.hyperliquid import (
    ARBITRUM_USDC_TOKEN_ID,
    HYPE_FEE_WALLET,
    HYPERLIQUID_BRIDGE_ADDRESS,
)
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import AVANTIS_AVUSDC, BASE_USDC
from wayfinder_paths.core.constants.polymarket import (
    POLYGON_USDC_ADDRESS,
    POLYGON_USDC_E_ADDRESS,
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


def build_adapter_execute_preview(tool_input: dict[str, Any]) -> dict[str, Any]:
    req = tool_input if isinstance(tool_input, dict) else {}
    if not req:
        return {"summary": "ADAPTER_EXECUTE missing parameters."}

    adapter = str(req.get("adapter") or "").strip()
    method = str(req.get("method") or "").strip()
    wallet_label = req.get("wallet_label")
    main_wallet_label = req.get("main_wallet_label")
    strategy_wallet_label = req.get("strategy_wallet_label")

    label_for_address = None
    if isinstance(strategy_wallet_label, str) and strategy_wallet_label.strip():
        label_for_address = strategy_wallet_label.strip()
    elif isinstance(main_wallet_label, str) and main_wallet_label.strip():
        label_for_address = main_wallet_label.strip()
    elif isinstance(wallet_label, str) and wallet_label.strip():
        label_for_address = wallet_label.strip()

    w = find_wallet_by_label(label_for_address) if label_for_address else None
    sender = normalize_address((w or {}).get("address")) if w else None

    args = req.get("args")
    kwargs = req.get("kwargs")

    header = "ADAPTER_EXECUTE\n"
    base = (
        f"adapter: {adapter or '(missing)'}\n"
        f"method: {method or '(missing)'}\n"
        f"wallet_label: {wallet_label}\n"
        f"main_wallet_label: {main_wallet_label}\n"
        f"strategy_wallet_label: {strategy_wallet_label}\n"
        f"address: {sender or '(unknown)'}\n"
        f"args: {args}\n"
        f"kwargs: {kwargs}"
    )
    return {"summary": header + base}


def build_polymarket_execute_preview(tool_input: dict[str, Any]) -> dict[str, Any]:
    req = tool_input if isinstance(tool_input, dict) else {}
    if not req:
        return {
            "summary": "POLYMARKET_EXECUTE missing parameters.",
            "recipient_mismatch": False,
        }

    action = str(req.get("action") or "").strip()
    wallet_label = str(req.get("wallet_label") or "").strip()
    w = find_wallet_by_label(wallet_label) if wallet_label else None
    sender = normalize_address((w or {}).get("address")) if w else None

    recipient = None
    if action == "bridge_deposit":
        recipient = normalize_address(req.get("recipient_address"))
    if action == "bridge_withdraw":
        recipient = normalize_address(req.get("recipient_addr"))

    mismatch = bool(sender and recipient and sender.lower() != recipient.lower())

    header = "POLYMARKET_EXECUTE\n"
    base = (
        f"action: {action or '(missing)'}\n"
        f"wallet_label: {wallet_label or '(missing)'}\n"
        f"address: {sender or '(unknown)'}"
    )

    if action == "bridge_deposit":
        details = (
            "\n\nCONVERT (token → USDC.e)\n"
            "route: BRAP swap preferred; Polymarket Bridge fallback\n"
            f"polymarket_collateral_usdce: {POLYGON_USDC_E_ADDRESS}\n"
            f"polygon_native_usdc: {POLYGON_USDC_ADDRESS}\n"
            f"from_chain_id: {req.get('from_chain_id')}\n"
            f"from_token_address: {req.get('from_token_address')}\n"
            f"amount: {req.get('amount')}\n"
            f"recipient_address: {req.get('recipient_address')}\n"
            "note: If you already have USDC.e on Polygon, you can trade without running bridge_deposit."
        )
        return {"summary": header + base + details, "recipient_mismatch": mismatch}

    if action == "bridge_withdraw":
        details = (
            "\n\nCONVERT (USDC.e → token)\n"
            "route: BRAP swap preferred; Polymarket Bridge fallback\n"
            f"polymarket_collateral_usdce: {POLYGON_USDC_E_ADDRESS}\n"
            f"polygon_native_usdc: {POLYGON_USDC_ADDRESS}\n"
            f"amount_usdce: {req.get('amount_usdce')}\n"
            f"to_chain_id: {req.get('to_chain_id')}\n"
            f"to_token_address: {req.get('to_token_address')}\n"
            f"recipient_addr: {req.get('recipient_addr')}"
        )
        return {"summary": header + base + details, "recipient_mismatch": mismatch}

    if action in {"buy", "sell", "close_position"}:
        details = (
            "\n\nTRADE\n"
            f"market_slug: {req.get('market_slug')}\n"
            f"outcome: {req.get('outcome')}\n"
            f"token_id: {req.get('token_id')}\n"
            f"amount_usdc: {req.get('amount_usdc')}\n"
            f"shares: {req.get('shares')}"
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

    return {"summary": header + base, "recipient_mismatch": mismatch}
