from __future__ import annotations

import asyncio
from typing import Any

from wayfinder_paths.core.clients.BRAPClient import BRAP_CLIENT
from wayfinder_paths.core.utils.token_resolver import TokenResolver
from wayfinder_paths.mcp.utils import (
    catch_errors,
    err,
    leg_for_chain,
    load_wallet_ring,
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
            quote_count_i = (
                int(quote_count) if quote_count is not None else len(all_quotes)
            )
        except (TypeError, ValueError):
            quote_count_i = len(all_quotes)

        return all_quotes, best_out, quote_count_i

    return [], None, 0


@catch_errors
async def onchain_quote_swap(
    *,
    wallet_label: str,
    from_token: str,
    to_token: str,
    amount: str,
    slippage_bps: int = 50,
    recipient: str | None = None,
    include_calldata: bool = False,
    allow_unverified_output: bool = False,
) -> dict[str, Any]:
    """Quote a BRAP cross-chain/cross-DEX swap without broadcasting.

    Mandatory before `onchain_swap`: verifies the resolved token symbols, addresses, and
    chains match intent, surfaces the best route, output, and fees, and returns a
    ready-to-use `suggested_swap_request` payload.

    Args:
        wallet_label: Sender wallet (config.json label).
        from_token / to_token: Token id (`<coingecko_id>-<chain_code>`), address id
            (`<chain_code>_<address>`), or symbol query.
        amount: Decimal human-units string (e.g. "1000.0" USDC or "0.5" ETH),
            not wei. Must include a decimal point; integer-looking strings like
            "1000" are rejected.
        slippage_bps: Slippage cap in basis points (50 = 0.50%).
        recipient: Optional destination override. Defaults to the destination-chain
            leg of the same wallet ring.
        include_calldata: Include the raw tx calldata in the response (off by default to keep
            payload small; only the `len` is reported when false).
        allow_unverified_output: Override a protected-identity safety block. Set
            true only after the user explicitly confirms the exact destination
            contract and acknowledges that it is not a canonical asset.

    Returns:
        `{preview, quote: {best_quote, quote_count, providers}, suggested_swap_request, ...}`.
        `preview` flags `⚠ RECIPIENT DIFFERS FROM SENDER` when applicable.
    """
    ring = await load_wallet_ring(wallet_label)
    if not ring:
        return err("not_found", f"Unknown wallet_label: {wallet_label}")

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

    # Cross-chain swaps send from the source-chain leg and land on the
    # destination-chain leg of the same wallet ring (e.g. EVM→Solana pays out to
    # the ring's SVM address). Same-chain swaps resolve both to the one leg;
    # missing a chain-specific leg falls back to the default (EVM) leg.
    from_leg = leg_for_chain(ring, from_chain_id) or ring[0]
    to_leg = leg_for_chain(ring, to_chain_id) or ring[0]
    sender = normalize_address(from_leg.get("address"))
    if not sender:
        return err("invalid_wallet", f"Wallet {wallet_label} missing address")

    rcpt = normalize_address(recipient) or normalize_address(to_leg.get("address"))
    if not rcpt:
        return err(
            "invalid_wallet",
            f"Wallet {wallet_label} has no destination address for chain {to_chain_id}",
        )
    slip = _slippage_float(slippage_bps)

    try:
        data = await BRAP_CLIENT.get_quote(
            from_token=from_token_addr,
            to_token=to_token_addr,
            from_chain=from_chain_id,
            to_chain=to_chain_id,
            from_wallet=sender,
            to_wallet=rcpt,
            from_amount=str(amount_raw),
            slippage=slip,
            allow_unverified_output=allow_unverified_output,
        )
    except Exception as exc:  # noqa: BLE001
        return err("quote_error", str(exc))

    all_quotes, best_quote, quote_count = _unwrap_brap_quote_response(data)
    if not best_quote:
        errors = data.get("errors") if isinstance(data, dict) else None
        return err(
            "quote_rejected" if errors else "no_route",
            "The route was rejected by token-output safety checks."
            if errors
            else "No route is available for this swap.",
            {"errors": errors or []},
        )

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
        tx_data: dict[str, Any] = best_quote.get("calldata") or {}
        calldata = tx_data.get("data")

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
            "safety_warnings": best_quote.get("safety_warnings"),
            "output_validation": best_quote.get("output_validation"),
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
        "suggested_swap_request": {
            "wallet_label": wallet_label,
            "from_token": from_token_id,
            "to_token": to_token_id,
            "amount": str(amount),
            "slippage_bps": int(slippage_bps),
            "recipient": rcpt,
            "allow_unverified_output": allow_unverified_output,
        },
    }
    return ok(result)
