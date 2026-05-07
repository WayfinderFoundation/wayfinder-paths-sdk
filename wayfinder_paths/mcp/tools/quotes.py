from __future__ import annotations

import asyncio
from typing import Any

from wayfinder_paths.core.clients.BRAPClient import BRAP_CLIENT
from wayfinder_paths.core.utils.token_resolver import TokenResolver
from wayfinder_paths.mcp.utils import (
    err,
    find_wallet_by_label,
    normalize_address,
    ok,
    parse_amount_to_raw,
)


def _slippage_float(slippage_bps: int) -> float:
    return max(0.0, float(int(slippage_bps)) / 10_000.0)


async def onchain_quote_swap(
    *,
    wallet_label: str,
    from_token: str,
    to_token: str,
    amount: str,
    slippage_bps: int = 50,
    recipient: str | None = None,
    include_calldata: bool = False,
) -> dict[str, Any]:
    """Quote a BRAP cross-chain/cross-DEX swap without broadcasting.

    Mandatory before `core_execute(kind="swap", ...)`: verifies the resolved token symbols,
    addresses, and chains match intent, surfaces the best route, output, and fees, and returns
    a ready-to-use `suggested_execute_request` payload.

    Args:
        wallet_label: Sender wallet (config.json label).
        from_token / to_token: Token id (`<coingecko_id>-<chain_code>`), address id
            (`<chain_code>_<address>`), or symbol query.
        amount: Wei string of the input amount (use `to_erc20_raw(human, decimals)` to convert).
            Note: NOT human units — this is the raw on-chain amount.
        slippage_bps: Slippage cap in basis points (50 = 0.50%).
        recipient: Destination address (defaults to sender).
        include_calldata: Include the raw tx calldata in the response (off by default to keep
            payload small; only the `len` is reported when false).

    Returns:
        `{preview, quote: {best_quote, quote_count, providers}, suggested_execute_request, ...}`.
        `preview` flags `⚠ RECIPIENT DIFFERS FROM SENDER` when applicable.
    """
    w = await find_wallet_by_label(wallet_label)
    if not w:
        return err("not_found", f"Unknown wallet_label: {wallet_label}")
    sender = normalize_address(w.get("address"))
    if not sender:
        return err("invalid_wallet", f"Wallet {wallet_label} missing address")

    try:
        from_meta, to_meta = await asyncio.gather(
            TokenResolver.resolve_token_meta(from_token),
            TokenResolver.resolve_token_meta(to_token),
        )
    except Exception as exc:  # noqa: BLE001
        return err("token_error", str(exc))

    from_chain_id = from_meta.get("chain_id")
    to_chain_id = to_meta.get("chain_id")
    from_token_addr = str(from_meta.get("address") or "").strip() or None
    to_token_addr = str(to_meta.get("address") or "").strip() or None
    if from_chain_id is None or to_chain_id is None:
        return err(
            "invalid_token",
            "Could not resolve chain_id for one or more tokens",
            {"from_chain_id": from_chain_id, "to_chain_id": to_chain_id},
        )
    if not from_token_addr or not to_token_addr:
        return err(
            "invalid_token",
            "Could not resolve token address for one or more tokens",
            {"from_token_address": from_token_addr, "to_token_address": to_token_addr},
        )

    decimals = int(from_meta.get("decimals") or 18)
    try:
        amount_raw = parse_amount_to_raw(amount, decimals)
    except ValueError as exc:
        return err("invalid_amount", str(exc))

    rcpt = normalize_address(recipient) or sender
    slip = _slippage_float(slippage_bps)

    try:
        data = await BRAP_CLIENT.get_quote(
            from_token=from_token_addr,
            to_token=to_token_addr,
            from_chain=from_chain_id,
            to_chain=to_chain_id,
            from_wallet=sender,
            from_amount=str(amount_raw),
            slippage=slip,
        )
    except Exception as exc:  # noqa: BLE001
        return err("quote_error", str(exc))

    all_quotes = [dict(q) for q in data["quotes"]]
    best_quote = data["best_quote"]
    quote_count = data.get("quote_count", len(all_quotes))

    providers: list[str] = []
    seen: set[str] = set()
    for q in all_quotes:
        p = q.get("provider")
        if not p:
            continue
        p_str = str(p)
        if p_str in seen:
            continue
        seen.add(p_str)
        providers.append(p_str)

    best_out: dict[str, Any] | None = None
    if isinstance(best_quote, dict):
        tx_data: dict[str, Any] = best_quote.get("calldata") or {}
        calldata = tx_data.get("data")

        best_out = {
            "provider": best_quote.get("provider"),
            "bridge_tracking": best_quote.get("bridge_tracking"),
            "input_amount": best_quote.get("input_amount"),
            "output_amount": best_quote.get("output_amount"),
            "input_amount_usd": best_quote.get("input_amount_usd"),
            "output_amount_usd": best_quote.get("output_amount_usd"),
            "gas_estimate": best_quote.get("gas_estimate"),
            "fee_estimate": best_quote.get("fee_estimate"),
            "native_input": best_quote.get("native_input"),
            "native_output": best_quote.get("native_output"),
        }

        # Strip data fields from wrap/unwrap transactions to reduce response size
        wrap_tx = best_quote.get("wrap_transaction")
        if isinstance(wrap_tx, dict):
            best_out["wrap_transaction"] = {
                k: v for k, v in wrap_tx.items() if k != "data"
            }
        unwrap_tx = best_quote.get("unwrap_transaction")
        if isinstance(unwrap_tx, dict):
            best_out["unwrap_transaction"] = {
                k: v for k, v in unwrap_tx.items() if k != "data"
            }

        if include_calldata:
            best_out["calldata"] = calldata
        else:
            best_out["calldata_len"] = len(calldata) if calldata else 0

    preview = (
        f"Swap {amount} {from_meta.get('symbol')} → {to_meta.get('symbol')} "
        f"(chain {from_chain_id} → {to_chain_id}). "
        f"Sender={sender} Recipient={rcpt}. Slippage={slip:.2%}."
    )
    if rcpt.lower() != sender.lower():
        preview = "⚠ RECIPIENT DIFFERS FROM SENDER\n" + preview

    from_token_id = from_meta.get("token_id") or from_token
    to_token_id = to_meta.get("token_id") or to_token

    result = {
        "preview": preview,
        "quote": {
            "best_quote": best_out,
            "quote_count": quote_count,
            "providers": providers,
        },
        "from_token": from_meta.get("symbol"),
        "to_token": to_meta.get("symbol"),
        "amount": str(amount),
        "slippage_bps": int(slippage_bps),
        "suggested_execute_request": {
            "kind": "swap",
            "wallet_label": wallet_label,
            "from_token": from_token_id,
            "to_token": to_token_id,
            "amount": str(amount),
            "slippage_bps": int(slippage_bps),
            "recipient": rcpt,
        },
    }

    return ok(result)


async def onchain_bridge_status(
    *,
    bridge_tracking: dict[str, Any] | None = None,
    tx_hash: str | None = None,
    provider: str | None = None,
    from_chain: int | None = None,
    to_chain: int | None = None,
    bridge: str | None = None,
    protocol: str | None = None,
    order_id: str | None = None,
) -> dict[str, Any]:
    """Fetch normalized BRAP bridge execution status for a submitted swap.

    Pass the `bridge_tracking` object returned by `onchain_quote_swap` plus the
    source transaction hash when `requires_source_tx_hash=true`.
    """
    if bridge_tracking is not None and not isinstance(bridge_tracking, dict):
        return err("invalid_request", "bridge_tracking must be an object")

    try:
        status = await BRAP_CLIENT.get_bridge_execution_status(
            bridge_tracking=bridge_tracking,
            tx_hash=tx_hash,
            provider=provider,
            from_chain=from_chain,
            to_chain=to_chain,
            bridge=bridge,
            protocol=protocol,
            order_id=order_id,
        )
    except Exception as exc:  # noqa: BLE001
        return err("bridge_status_error", str(exc))

    next_action = "poll_again"
    if status.get("is_finished"):
        next_action = "done" if status.get("is_success") else "review_failure"

    return ok(
        {
            "bridge_status": status,
            "next_action": next_action,
            "next_poll_seconds": status.get("next_poll_seconds"),
        }
    )
