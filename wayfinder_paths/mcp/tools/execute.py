from __future__ import annotations

from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.clients.BRAPClient import BRAP_CLIENT
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.utils.etherscan import get_etherscan_transaction_link
from wayfinder_paths.core.utils.token_resolver import TokenResolver
from wayfinder_paths.core.utils.tokens import (
    build_send_transaction,
    ensure_allowance,
)
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.core.utils.wallets import get_wallet_signing_callback
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    catch_errors,
    err,
    normalize_address,
    ok,
    parse_amount_to_raw,
    sanitize_for_json,
)


def _compact_quote(
    quote_data: dict[str, Any], best_quote: dict[str, Any] | None
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    # BRAP quotes may appear as either:
    # 1) {"quotes": [...], "best_quote": {...}}
    # 2) {"quotes": {"all_quotes": [...], "best_quote": {...}, "quote_count": N}}
    all_quotes: list[dict[str, Any]] = []
    raw_quotes = quote_data.get("quotes", [])
    quote_count = None

    if isinstance(raw_quotes, list):
        all_quotes = [q for q in raw_quotes if isinstance(q, dict)]
    elif isinstance(raw_quotes, dict):
        nested = raw_quotes.get("all_quotes") or raw_quotes.get("quotes") or []
        if isinstance(nested, list):
            all_quotes = [q for q in nested if isinstance(q, dict)]
        qc = raw_quotes.get("quote_count")
        try:
            quote_count = int(qc) if qc is not None else None
        except (TypeError, ValueError):
            quote_count = None

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

    if providers:
        result["providers"] = providers
    result["quote_count"] = quote_count if quote_count is not None else len(all_quotes)

    if isinstance(best_quote, dict):
        result["best"] = {
            "provider": best_quote.get("provider"),
            "input_amount": best_quote.get("input_amount"),
            "output_amount": best_quote.get("output_amount"),
            "input_usd": best_quote.get("input_amount_usd"),
            "output_usd": best_quote.get("output_amount_usd"),
        }
        fee = best_quote.get("fee_estimate")
        if isinstance(fee, dict):
            result["best"]["fee_usd"] = fee.get("fee_total_usd")
        quote_inner = best_quote.get("quote", {})
        if isinstance(quote_inner, dict):
            route = quote_inner.get("route", [])
            if isinstance(route, list):
                result["best"]["route"] = [
                    r.get("protocol")
                    for r in route
                    if isinstance(r, dict) and r.get("protocol")
                ]
            steps = quote_inner.get("includedSteps", [])
            if isinstance(steps, list) and not result["best"].get("route"):
                result["best"]["route"] = [
                    s.get("tool")
                    for s in steps
                    if isinstance(s, dict) and s.get("tool")
                ]

    return result


async def _broadcast(
    sign_callback,
    tx: dict[str, Any],
    *,
    chain_id: int,
    wait_for_receipt: bool = False,
    confirmations: int = 0,
) -> tuple[bool, dict[str, Any]]:
    try:
        txn_hash = await send_transaction(
            tx,
            sign_callback,
            wait_for_receipt=wait_for_receipt,
            confirmations=confirmations,
        )
        result: dict[str, Any] = {
            "txn_hash": txn_hash,
            "chain_id": chain_id,
            "confirmation_waited": wait_for_receipt,
            "confirmations": confirmations if wait_for_receipt else 0,
        }
        explorer_link = get_etherscan_transaction_link(chain_id, txn_hash)
        if explorer_link:
            result["explorer_url"] = explorer_link
        return True, result
    except Exception as e:
        return False, {"error": sanitize_for_json(str(e)), "chain_id": chain_id}


async def _ensure_allowance(
    *,
    sign_callback,
    chain_id: int,
    token_address: str,
    owner: str,
    spender: str,
    amount: int,
) -> tuple[bool, dict[str, Any] | None]:
    sent_ok, txn_hash = await ensure_allowance(
        token_address=token_address,
        owner=owner,
        spender=spender,
        amount=amount,
        chain_id=chain_id,
        signing_callback=sign_callback,
        confirmations=0,
    )
    if not txn_hash:
        return sent_ok, None
    result: dict[str, Any] = {"txn_hash": txn_hash, "chain_id": chain_id}
    explorer_link = get_etherscan_transaction_link(chain_id, txn_hash)
    if explorer_link:
        result["explorer_url"] = explorer_link
    return sent_ok, result


def _annotate_profile(
    *,
    address: str,
    label: str,
    protocol: str,
    action: str,
    tool: str,
    status: str,
    chain_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    store = WalletProfileStore.default()
    store.annotate_safe(
        address=address,
        label=label,
        protocol=protocol,
        action=action,
        tool=tool,
        status=status,
        chain_id=chain_id,
        details=details,
    )


@catch_errors
async def onchain_swap(
    *,
    wallet_label: str,
    from_token: str,
    to_token: str,
    amount: str,
    slippage_bps: int = 50,
    recipient: str | None = None,
    wait_for_receipt: bool = True,
    receipt_confirmations: int = 0,
) -> dict[str, Any]:
    """Broadcast a cross-chain / cross-DEX swap via BRAP.

    **Always quote first** — call `onchain_quote_swap` and confirm route + output with the
    user before running this. Waits for the receipt by default and returns
    `status="confirmed"`; pass `wait_for_receipt=False` for fire-and-forget broadcast
    on slow chains where the MCP client may time out.

    Args:
        wallet_label: Required — config.json wallet label (e.g. "main").
        from_token: Source token id, address-id, or symbol query.
        to_token: Destination token id, address-id, or symbol query.
        amount: Human-units string (e.g. "1000" or "0.5"), not wei.
        slippage_bps: Slippage cap in basis points (50 = 0.5%, default).
        recipient: Destination address (defaults to sender).
        wait_for_receipt: Synchronous receipt wait. Default true.
        receipt_confirmations: Confirmations to wait for when `wait_for_receipt=true`.

    Returns:
        `{status: "submitted"|"confirmed"|"failed", sender, recipient, effects: {approval?, swap}, raw}`.
    """
    if not wallet_label.strip():
        return err("invalid_request", "wallet_label is required")
    if slippage_bps < 0:
        return err("invalid_request", "slippage_bps must be >= 0")

    sign_callback, sender = await get_wallet_signing_callback(wallet_label)
    rcpt = normalize_address(recipient) or sender
    response: dict[str, Any] = {
        "sender": sender,
        "recipient": rcpt,
        "effects": {},
    }

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
            {
                "from_token_address": from_token_addr,
                "to_token_address": to_token_addr,
            },
        )

    decimals = int(from_meta.get("decimals") or 18)
    try:
        amount_raw = parse_amount_to_raw(amount, decimals)
    except ValueError as exc:
        return err("invalid_amount", str(exc))

    slippage = max(0.0, float(int(slippage_bps)) / 10_000.0)
    try:
        quote_data = await BRAP_CLIENT.get_quote(
            from_token=from_token_addr,
            to_token=to_token_addr,
            from_chain=from_chain_id,
            to_chain=to_chain_id,
            from_wallet=sender,
            from_amount=str(amount_raw),
            slippage=slippage,
        )
    except Exception as exc:  # noqa: BLE001
        return err("quote_error", str(exc))

    best_quote = None
    if isinstance(quote_data, dict):
        if isinstance(quote_data.get("best_quote"), dict):
            best_quote = quote_data.get("best_quote")
        else:
            quotes_block = quote_data.get("quotes")
            if isinstance(quotes_block, dict) and isinstance(
                quotes_block.get("best_quote"), dict
            ):
                best_quote = quotes_block.get("best_quote")

    if not isinstance(best_quote, dict):
        return err("quote_error", "No best_quote returned", {"quote": quote_data})

    calldata = best_quote.get("calldata") or {}
    if not isinstance(calldata, dict) or not calldata:
        return err(
            "quote_error", "best_quote missing calldata", {"best_quote": best_quote}
        )

    swap_tx = dict(calldata)
    swap_tx["chainId"] = int(from_chain_id)
    swap_tx["from"] = to_checksum_address(sender)
    if "value" in swap_tx:
        swap_tx["value"] = int(swap_tx["value"])

    spender = swap_tx.get("to")
    approve_amount = (
        best_quote.get("input_amount")
        or best_quote.get("inputAmount")
        or best_quote.get("amount1")
        or best_quote.get("amount")
    )

    if (
        from_token_addr.lower() != ZERO_ADDRESS.lower()
        and spender
        and approve_amount is not None
    ):
        try:
            need = int(approve_amount)
        except Exception:
            need = int(amount_raw)
        ok_allow, approval_tx = await _ensure_allowance(
            sign_callback=sign_callback,
            chain_id=int(from_chain_id),
            token_address=from_token_addr,
            owner=to_checksum_address(sender),
            spender=to_checksum_address(str(spender)),
            amount=need,
        )
        if approval_tx:
            response["effects"]["approval"] = approval_tx
        if not ok_allow:
            response["status"] = "failed"
            response["raw"] = _compact_quote(quote_data, None)
            return ok(response)

    sent_ok, sent = await _broadcast(
        sign_callback,
        swap_tx,
        chain_id=int(from_chain_id),
        wait_for_receipt=wait_for_receipt,
        confirmations=receipt_confirmations,
    )
    response["effects"]["swap"] = sent

    status = "confirmed" if sent_ok and wait_for_receipt else "submitted"
    if not sent_ok:
        status = "failed"
    response["status"] = status
    response["raw"] = _compact_quote(quote_data, best_quote)

    _annotate_profile(
        address=sender,
        label=wallet_label,
        protocol="brap",
        action="swap",
        tool="onchain_swap",
        status=status,
        chain_id=int(from_chain_id),
        details={
            "from_token": from_token,
            "to_token": to_token,
            "amount": amount,
        },
    )

    return ok(response)


@catch_errors
async def onchain_send(
    *,
    wallet_label: str,
    token: str,
    recipient: str,
    amount: str,
    chain_id: int | None = None,
    wait_for_receipt: bool = True,
    receipt_confirmations: int = 0,
) -> dict[str, Any]:
    """Broadcast an ERC-20 or native token transfer.

    Waits for the receipt by default and returns `status="confirmed"`; pass
    `wait_for_receipt=False` for fire-and-forget broadcast on slow chains where the MCP
    client may time out.

    Args:
        wallet_label: Required — config.json wallet label.
        token: Token id, address-id, symbol query, or `"native"`.
        recipient: Destination address. Required.
        amount: Human-units string (e.g. "5" for 5 USDC), not wei.
        chain_id: Required when `token="native"`; ignored otherwise.
        wait_for_receipt: Synchronous receipt wait. Default true.
        receipt_confirmations: Confirmations to wait for when `wait_for_receipt=true`.

    Returns:
        `{status: "submitted"|"confirmed"|"failed", sender, recipient, effects: {send_native|send_erc20}, raw}`.
    """
    if not wallet_label.strip():
        return err("invalid_request", "wallet_label is required")
    token_q = token.strip()
    if not token_q:
        return err("invalid_request", "token is required")
    if token_q.lower() == "native" and chain_id is None:
        return err("invalid_request", "chain_id is required when token='native'")

    sign_callback, sender = await get_wallet_signing_callback(wallet_label)
    rcpt = normalize_address(recipient)
    if not rcpt:
        return err("invalid_request", "recipient address is required")

    response: dict[str, Any] = {
        "sender": sender,
        "recipient": rcpt,
        "effects": {},
    }

    try:
        token_meta = await TokenResolver.resolve_token_meta(token_q, chain_id=chain_id)
    except Exception as exc:  # noqa: BLE001
        return err("token_error", str(exc))

    token_address = str(token_meta.get("address") or "").strip()
    resolved_chain_id = token_meta.get("chain_id")
    if not token_address or resolved_chain_id is None:
        return err(
            "invalid_token",
            "Token missing address/chain_id",
            {"token": token_meta},
        )
    decimals = int(token_meta.get("decimals") or 18)
    is_native = token_address.lower() == ZERO_ADDRESS.lower()

    try:
        amount_raw = parse_amount_to_raw(amount, decimals)
    except ValueError as exc:
        return err("invalid_amount", str(exc))

    transaction = await build_send_transaction(
        from_address=sender,
        to_address=rcpt,
        token_address=token_address,
        chain_id=int(resolved_chain_id),
        amount=int(amount_raw),
    )

    sent_ok, sent = await _broadcast(
        sign_callback,
        transaction,
        chain_id=int(resolved_chain_id),
        wait_for_receipt=wait_for_receipt,
        confirmations=receipt_confirmations,
    )
    label = "send_native" if is_native else "send_erc20"
    response["effects"][label] = sent

    status = "confirmed" if sent_ok and wait_for_receipt else "submitted"
    if not sent_ok:
        status = "failed"
    response["status"] = status
    response["raw"] = {"transaction": transaction, "token": token_meta}

    _annotate_profile(
        address=sender,
        label=wallet_label,
        protocol="balance",
        action=label,
        tool="onchain_send",
        status=status,
        chain_id=int(resolved_chain_id),
        details={"recipient": rcpt, "amount": amount, "token": token_q},
    )

    return ok(response)
