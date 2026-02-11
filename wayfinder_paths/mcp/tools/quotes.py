from __future__ import annotations

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


def _unwrap_brap_quote_response(
    data: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, int]:
    """
    BRAP quote responses have historically appeared in two shapes:

    1) {"quotes": [...], "best_quote": {...}}
    2) {"quotes": {"all_quotes": [...], "best_quote": {...}, "quote_count": N}}

    This helper normalizes both to (all_quotes, best_quote, quote_count).
    """
    if not isinstance(data, dict):
        return [], None, 0

    raw_quotes = data.get("quotes")
    best_quote = data.get("best_quote")

    if isinstance(raw_quotes, list) or isinstance(best_quote, dict):
        all_quotes = raw_quotes if isinstance(raw_quotes, list) else []
        best = best_quote if isinstance(best_quote, dict) else None
        return all_quotes, best, len(all_quotes)

    # Legacy/nested payload under `quotes`
    if isinstance(raw_quotes, dict):
        all_quotes = raw_quotes.get("all_quotes") or raw_quotes.get("quotes") or []
        if not isinstance(all_quotes, list):
            all_quotes = []
        best = raw_quotes.get("best_quote")
        best_out = best if isinstance(best, dict) else None

        quote_count = raw_quotes.get("quote_count")
        try:
            quote_count_i = int(quote_count)
        except (TypeError, ValueError):
            quote_count_i = len(all_quotes)

        return all_quotes, best_out, quote_count_i

    return [], None, 0


async def quote_swap(
    *,
    wallet_label: str,
    from_token: str,
    to_token: str,
    amount: str,
    slippage_bps: int = 50,
    recipient: str | None = None,
    include_calldata: bool = False,
) -> dict[str, Any]:
    w = find_wallet_by_label(wallet_label)
    if not w:
        return err("not_found", f"Unknown wallet_label: {wallet_label}")
    sender = normalize_address(w.get("address"))
    if not sender:
        return err("invalid_wallet", f"Wallet {wallet_label} missing address")

    try:
        from_meta = await TokenResolver.resolve_token_meta(from_token)
        to_meta = await TokenResolver.resolve_token_meta(to_token)
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

    all_quotes, best_quote, quote_count = _unwrap_brap_quote_response(data)

    providers: list[str] = []
    seen: set[str] = set()
    for q in all_quotes:
        if not isinstance(q, dict):
            continue
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
        calldata = best_quote.get("calldata")
        calldata_len = len(calldata) if calldata else 0

        best_out = {
            "provider": best_quote.get("provider"),
            "input_amount": best_quote.get("input_amount"),
            "output_amount": best_quote.get("output_amount"),
            "input_amount_usd": best_quote.get("input_amount_usd"),
            "output_amount_usd": best_quote.get("output_amount_usd"),
            "gas_estimate": best_quote.get("gas_estimate"),
            "fee_estimate": best_quote.get("fee_estimate"),
            "native_input": best_quote.get("native_input"),
            "native_output": best_quote.get("native_output"),
            "error": best_quote.get("error"),
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
            best_out["calldata_len"] = calldata_len

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
