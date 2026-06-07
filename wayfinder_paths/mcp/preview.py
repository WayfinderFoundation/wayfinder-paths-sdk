from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.core.constants.hyperliquid import HYPE_FEE_WALLET
from wayfinder_paths.core.constants.polymarket import (
    POLYGON_CHAIN_ID,
    POLYGON_P_USDC_PROXY_ADDRESS,
)
from wayfinder_paths.core.utils.tokens import get_token_balance
from wayfinder_paths.mcp.polymarket_order import (
    as_float,
    normalize_pm_execution_summary,
    normalize_pm_side,
    validate_pm_market_order_size,
)
from wayfinder_paths.mcp.utils import (
    find_wallet_by_label,
    normalize_address,
    read_text_excerpt,
    repo_root,
)


async def _onchain_preview_base(
    tool_input: dict[str, Any], header: str
) -> tuple[str, str, str | None]:
    wallet_label = str(tool_input.get("wallet_label") or "").strip()
    w = await find_wallet_by_label(wallet_label) if wallet_label else None
    sender = normalize_address((w or {}).get("address")) if w else None
    base = (
        f"wallet_label: {wallet_label or '(missing)'}\nsender: {sender or '(unknown)'}"
    )
    return header, base, sender


async def build_onchain_swap_preview(tool_input: dict[str, Any]) -> dict[str, Any]:
    header, base, sender = await _onchain_preview_base(tool_input, "ONCHAIN_SWAP\n")
    recipient = normalize_address(tool_input.get("recipient")) or sender
    details = (
        "\n\nSWAP\n"
        f"from_token: {tool_input.get('from_token')}\n"
        f"to_token: {tool_input.get('to_token')}\n"
        f"amount: {tool_input.get('amount')}\n"
        f"slippage_bps: {tool_input.get('slippage_bps')}\n"
        f"recipient: {recipient or '(unknown)'}"
    )
    mismatch = bool(sender and recipient and sender.lower() != recipient.lower())
    return {"summary": header + base + details, "recipient_mismatch": mismatch}


async def build_onchain_send_preview(tool_input: dict[str, Any]) -> dict[str, Any]:
    header, base, sender = await _onchain_preview_base(tool_input, "ONCHAIN_SEND\n")
    recipient = normalize_address(tool_input.get("recipient"))
    details = (
        "\n\nSEND\n"
        f"token: {tool_input.get('token')}\n"
        f"amount: {tool_input.get('amount')}\n"
        f"chain_id: {tool_input.get('chain_id')}\n"
        f"recipient: {recipient or '(missing)'}"
    )
    mismatch = bool(sender and recipient and sender.lower() != recipient.lower())
    return {"summary": header + base + details, "recipient_mismatch": mismatch}


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


async def build_hyperliquid_place_market_order_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _hl_preview_base(
        tool_input, "HYPERLIQUID_PLACE_MARKET_ORDER\n"
    )
    details = (
        "\n\nMARKET ORDER (IOC)\n"
        f"is_buy: {tool_input.get('is_buy')}\n"
        f"size: {tool_input.get('size')}\n"
        f"usd_amount: {tool_input.get('usd_amount')}\n"
        f"slippage: {tool_input.get('slippage')}\n"
        f"reduce_only: {tool_input.get('reduce_only')}\n"
        f"cloid: {tool_input.get('cloid')}\n"
        f"builder_wallet: {HYPE_FEE_WALLET}"
    )
    return {"summary": header + base + details}


async def build_hyperliquid_place_limit_order_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _hl_preview_base(tool_input, "HYPERLIQUID_PLACE_LIMIT_ORDER\n")
    details = (
        "\n\nLIMIT ORDER (GTC)\n"
        f"is_buy: {tool_input.get('is_buy')}\n"
        f"price: {tool_input.get('price')}\n"
        f"size: {tool_input.get('size')}\n"
        f"usd_amount: {tool_input.get('usd_amount')}\n"
        f"reduce_only: {tool_input.get('reduce_only')}\n"
        f"cloid: {tool_input.get('cloid')}\n"
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


async def build_hyperliquid_deposit_usdc_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _hl_preview_base(tool_input, "HYPERLIQUID_DEPOSIT\n")
    details = f"\n\nDEPOSIT\namount_usdc: {tool_input.get('amount_usdc')}"
    return {"summary": header + base + details}


async def build_hyperliquid_withdraw_usdc_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base = await _hl_preview_base(tool_input, "HYPERLIQUID_WITHDRAW\n")
    details = f"\n\nWITHDRAW\namount_usdc: {tool_input.get('amount_usdc')}"
    return {"summary": header + base + details}


async def _pm_preview_base(
    tool_input: dict[str, Any], header: str
) -> tuple[str, str, str | None]:
    wallet_label = str(tool_input.get("wallet_label") or "").strip()
    w = await find_wallet_by_label(wallet_label) if wallet_label else None
    sender = normalize_address((w or {}).get("address")) if w else None
    return (
        header,
        f"wallet_label: {wallet_label or '(missing)'}\naddress: {sender or '(unknown)'}",
        sender,
    )


def _fmt_pm_value(value: Any, *, suffix: str = "", precision: int = 6) -> str:
    numeric = as_float(value)
    if numeric is None:
        return "unknown"
    text = f"{numeric:.{precision}f}".rstrip("0").rstrip(".")
    return f"{text}{suffix}"


def _pm_market_question(market: dict[str, Any]) -> str:
    return str(
        market.get("question")
        or market.get("title")
        or market.get("slug")
        or "(unknown market)"
    )


async def _pm_deposit_wallet_balance_line(deposit_wallet: str | None) -> str:
    if not deposit_wallet:
        return "deposit pUSD balance: unknown"
    try:
        balance_raw = await get_token_balance(
            POLYGON_P_USDC_PROXY_ADDRESS,
            POLYGON_CHAIN_ID,
            deposit_wallet,
            block_identifier="latest",
        )
        return f"deposit pUSD balance: {_fmt_pm_value(balance_raw / 1_000_000, suffix=' pUSD')}"
    except Exception as exc:  # noqa: BLE001
        return f"deposit pUSD balance: unavailable ({exc})"


async def _pm_market_order_quote_preview(
    *,
    tool_input: dict[str, Any],
    sender: str | None,
) -> list[str]:
    lines: list[str] = []
    try:
        side = normalize_pm_side(tool_input.get("side"))
        sizing = validate_pm_market_order_size(
            side=side,
            buy_amount_pusd=tool_input.get("buy_amount_pusd"),
            sell_amount_shares=tool_input.get("sell_amount_shares"),
        )
    except Exception as exc:  # noqa: BLE001
        return [f"INVALID SIZE: {exc}"]

    if not sender:
        return ["PREVIEW HYDRATION WARNING: wallet label could not be resolved"]

    config = dict(CONFIG)
    config["main_wallet"] = {"address": sender}
    config["strategy_wallet"] = {"address": sender}
    adapter = PolymarketAdapter(config=config, wallet_address=sender)
    try:
        deposit_wallet = adapter.deposit_wallet_address()
        lines.append(f"deposit wallet: {deposit_wallet}")
        lines.append(await _pm_deposit_wallet_balance_line(deposit_wallet))

        market_slug = str(tool_input.get("market_slug") or "").strip()
        token_id = str(tool_input.get("token_id") or "").strip()
        outcome = tool_input.get("outcome", "YES")

        if market_slug:
            ok_market, market = await adapter.get_market_by_slug(market_slug)
            if ok_market and isinstance(market, dict):
                lines.append(f"market: {_pm_market_question(market)}")
                lines.append(f"market_slug: {market_slug}")
                lines.append(f"outcome: {outcome}")
                ok_tid, resolved_token = adapter.resolve_clob_token_id(
                    market=market, outcome=outcome
                )
                if ok_tid:
                    token_id = str(resolved_token)
                    lines.append(f"resolved token_id: {token_id}")
                else:
                    lines.append(f"MARKET RESOLUTION FAILED: {resolved_token}")
            else:
                lines.append(f"MARKET RESOLUTION FAILED: {market}")
        elif token_id:
            lines.append("market: not hydrated (token_id provided directly)")
            lines.append(f"token_id: {token_id}")
        else:
            lines.append(
                "MARKET RESOLUTION FAILED: token_id or market_slug is required"
            )

        if not token_id:
            lines.append("QUOTE UNAVAILABLE: no resolved token_id")
            return lines

        ok_quote, quote = await adapter.quote_market_order(
            token_id=token_id,
            side=side,
            amount=sizing["adapter_amount"],
        )
        if not ok_quote or not isinstance(quote, dict):
            lines.append(f"QUOTE UNAVAILABLE: {quote}")
            return lines

        summary = normalize_pm_execution_summary(
            side=side,
            sizing=sizing,
            quote=quote,
        )
        if summary["status"] != "filled":
            lines.append(
                "INSUFFICIENT DEPTH / PARTIAL FILL: "
                f"status={summary['status']}, fillRatio={_fmt_pm_value(summary['fillRatio'])}"
            )

        lines.extend(
            [
                f"avg price: {_fmt_pm_value(summary['avgPrice'])}",
                f"best price: {_fmt_pm_value(quote.get('best_price'))}",
                f"worst price: {_fmt_pm_value(quote.get('worst_price'))}",
                f"depth: {'fully fillable' if quote.get('fully_fillable') else 'partial'}, "
                f"levels consumed: {quote.get('levels_consumed')}",
                f"price impact: {_fmt_pm_value(quote.get('price_impact_bps'), suffix=' bps')}",
                f"fill ratio: {_fmt_pm_value(summary['fillRatio'])}",
            ]
        )
        if side == "BUY":
            lines.extend(
                [
                    f"expected pUSD spent: {_fmt_pm_value(summary['collateralSpent'], suffix=' pUSD')}",
                    f"expected shares: {_fmt_pm_value(summary['sharesFilled'])}",
                ]
            )
        else:
            lines.extend(
                [
                    f"shares to sell: {_fmt_pm_value(summary['requestedShares'])}",
                    f"expected pUSD received: {_fmt_pm_value(summary['collateralReceived'], suffix=' pUSD')}",
                ]
            )
        return lines
    except Exception as exc:  # noqa: BLE001
        lines.append(f"PREVIEW HYDRATION WARNING: {exc}")
        return lines
    finally:
        await adapter.close()


async def build_polymarket_deposit_pusd_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base, _sender = await _pm_preview_base(
        tool_input, "POLYMARKET_FUND_DEPOSIT_WALLET\n"
    )
    details = f"\n\nFUND DEPOSIT WALLET\namount (pUSD): {tool_input.get('amount')}"
    return {"summary": header + base + details}


async def build_polymarket_withdraw_pusd_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base, _sender = await _pm_preview_base(
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
    header, base, sender = await _pm_preview_base(
        tool_input, "POLYMARKET_PLACE_MARKET_ORDER\n"
    )
    side = str(tool_input.get("side") or "").upper()
    if side == "BUY":
        size_line = f"BUY spend: {tool_input.get('buy_amount_pusd')} pUSD"
    elif side == "SELL":
        size_line = f"SELL size: {tool_input.get('sell_amount_shares')} shares"
    else:
        size_line = (
            "size: (missing; BUY uses buy_amount_pusd, SELL uses sell_amount_shares)"
        )
    details = (
        "\n\nMARKET ORDER\n"
        f"market_slug: {tool_input.get('market_slug')}\n"
        f"outcome: {tool_input.get('outcome')}\n"
        f"token_id: {tool_input.get('token_id')}\n"
        f"side: {tool_input.get('side')}\n"
        f"{size_line}\n"
        f"slippage cap: {tool_input.get('max_slippage_pct') if tool_input.get('max_slippage_pct') is not None else PolymarketAdapter.DEFAULT_MAX_SLIPPAGE_PCT}%"
    )
    quote_lines = await _pm_market_order_quote_preview(
        tool_input=tool_input,
        sender=sender,
    )
    if quote_lines:
        details += "\n\nEXECUTION PREVIEW\n" + "\n".join(quote_lines)
    return {"summary": header + base + details}


async def build_polymarket_place_limit_order_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base, _sender = await _pm_preview_base(
        tool_input, "POLYMARKET_PLACE_LIMIT_ORDER\n"
    )
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
    header, base, _sender = await _pm_preview_base(
        tool_input, "POLYMARKET_CANCEL_ORDER\n"
    )
    details = f"\n\nCANCEL ORDER\norder_id: {tool_input.get('order_id')}"
    return {"summary": header + base + details}


async def build_polymarket_redeem_positions_preview(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    header, base, _sender = await _pm_preview_base(
        tool_input, "POLYMARKET_REDEEM_POSITIONS\n"
    )
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
