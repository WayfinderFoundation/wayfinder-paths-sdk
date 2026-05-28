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
    get_token_balance,
)
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.core.utils.units import from_erc20_raw
from wayfinder_paths.core.utils.wallets import get_wallet_signing_callback
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.tools.quotes import unwrap_brap_quote_response
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

    all_quotes, _, quote_count = unwrap_brap_quote_response(quote_data)

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


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _quote_approval_spender(
    best_quote: dict[str, Any], swap_tx: dict[str, Any]
) -> str | None:
    spender = (
        best_quote.get("approvalAddress")
        or best_quote.get("approval_address")
        or best_quote.get("spender")
        or swap_tx.get("to")
    )
    return str(spender) if spender else None


def _approve_amount(best_quote: dict[str, Any], fallback_amount: int) -> int:
    return _int_or_none(
        best_quote.get("input_amount")
        or best_quote.get("inputAmount")
        or best_quote.get("amount1")
        or best_quote.get("amount")
    ) or int(fallback_amount)


def _classify_swap_error(message: str | None) -> dict[str, str] | None:
    text = (message or "").lower()
    if not text:
        return None
    if "transfer amount exceeds allowance" in text:
        return {
            "code": "allowance_insufficient",
            "hint": "The approved allowance was not usable by the route spender.",
        }
    if "transfer_from_failed" in text or "transferfromfailed" in text:
        return {
            "code": "transfer_from_failed",
            "hint": "The route failed while pulling the source token from the wallet.",
        }
    if "allowance" in text:
        return {
            "code": "allowance_error",
            "hint": "The route could not verify or use ERC20 allowance.",
        }
    return None


def _failure(
    *,
    code: str,
    stage: str,
    message: str,
    spender: str | None = None,
    required_allowance_raw: int | None = None,
    observed_allowance_raw: int | None = None,
    quote_provider: str | None = None,
    hint: str | None = None,
    raw_error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "stage": stage,
        "message": message,
    }
    if spender is not None:
        payload["spender"] = spender
    if required_allowance_raw is not None:
        payload["required_allowance_raw"] = required_allowance_raw
    if observed_allowance_raw is not None:
        payload["observed_allowance_raw"] = observed_allowance_raw
    if quote_provider is not None:
        payload["quote_provider"] = quote_provider
    if hint is not None:
        payload["hint"] = hint
    if raw_error is not None:
        payload["raw_error"] = sanitize_for_json(raw_error)
    return payload


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
    user before running this. Same-chain swaps wait for the source receipt; cross-chain
    swaps additionally wait for the destination bridge leg to settle (via the
    BRAP wait-bridge-execution endpoint). Pass `wait_for_receipt=False` for
    fire-and-forget broadcast (skips both waits).

    Args:
        wallet_label: Wallet label.
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

    balance = await get_token_balance(from_token_addr, int(from_chain_id), sender)
    if balance < amount_raw:
        symbol = from_meta["symbol"] or "tokens"
        return err(
            "insufficient_balance",
            f"Wallet has {from_erc20_raw(balance, decimals):.6f} {symbol}, "
            f"need {from_erc20_raw(amount_raw, decimals):.6f}.",
            {
                "sender": sender,
                "chain_id": int(from_chain_id),
                "token_address": from_token_addr,
                "have_raw": balance,
                "need_raw": amount_raw,
            },
        )

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

    if not isinstance(quote_data, dict):
        return err("quote_error", "Quote response was not an object")
    _, best_quote, _ = unwrap_brap_quote_response(quote_data)
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

    spender = _quote_approval_spender(best_quote, swap_tx)
    need = _approve_amount(best_quote, amount_raw)
    spender_checksum = to_checksum_address(str(spender)) if spender else None

    if (
        from_token_addr.lower() != ZERO_ADDRESS.lower()
        and spender_checksum
        and need > 0
    ):
        try:
            ok_allow, approval_tx = await _ensure_allowance(
                sign_callback=sign_callback,
                chain_id=int(from_chain_id),
                token_address=from_token_addr,
                owner=to_checksum_address(sender),
                spender=spender_checksum,
                amount=need,
            )
        except Exception as exc:  # noqa: BLE001
            classified = _classify_swap_error(str(exc)) or {
                "code": "allowance_read_failed",
                "hint": "The token did not return a standard ERC20 allowance response.",
            }
            response["status"] = "failed"
            response["failure"] = _failure(
                code=classified["code"],
                stage="allowance",
                message="Could not prepare ERC20 approval for the swap.",
                spender=spender_checksum,
                required_allowance_raw=need,
                observed_allowance_raw=0,
                quote_provider=str(best_quote.get("provider") or ""),
                hint=classified.get("hint"),
                raw_error=str(exc),
            )
            response["raw"] = _compact_quote(quote_data, best_quote)
            return ok(response)

        if approval_tx:
            response["effects"]["approval"] = approval_tx
            response["effects"].setdefault("approvals", []).append(approval_tx)
        if not ok_allow:
            response["status"] = "failed"
            response["failure"] = _failure(
                code="approval_not_visible_yet",
                stage="approval",
                message="Approval was submitted, but allowance was not visible before swap execution.",
                spender=spender_checksum,
                required_allowance_raw=need,
                quote_provider=str(best_quote.get("provider") or ""),
                hint="Wait briefly, then ask the user to confirm a fresh quote.",
            )
            response["raw"] = _compact_quote(quote_data, best_quote)
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
        error_text = str(sent.get("error") or "")
        classified = _classify_swap_error(error_text)
        if classified:
            response["failure"] = _failure(
                code=classified["code"],
                stage="swap_broadcast",
                message="Swap route failed while pulling the source token.",
                spender=spender_checksum,
                required_allowance_raw=need,
                quote_provider=str(best_quote.get("provider") or ""),
                hint=classified.get("hint"),
                raw_error=error_text,
            )

    bridge_tracking = best_quote.get("bridge_tracking")
    if sent_ok and wait_for_receipt and bridge_tracking:
        try:
            bridge_result = await BRAP_CLIENT.wait_for_bridge_execution(
                bridge_tracking=bridge_tracking,
                tx_hash=sent["txn_hash"],
            )
            response["effects"]["bridge"] = bridge_result
            if not bridge_result.get("is_success"):
                status = "failed"
        except Exception as exc:  # noqa: BLE001
            response["effects"]["bridge"] = {
                "state": "pending",
                "error": sanitize_for_json(str(exc)),
            }
            status = "submitted"

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
        wallet_label: Wallet label.
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

    balance = await get_token_balance(token_address, int(resolved_chain_id), sender)
    if balance < amount_raw:
        symbol = token_meta["symbol"] or "tokens"
        return err(
            "insufficient_balance",
            f"Wallet has {from_erc20_raw(balance, decimals):.6f} {symbol}, "
            f"need {from_erc20_raw(amount_raw, decimals):.6f}.",
            {
                "sender": sender,
                "chain_id": int(resolved_chain_id),
                "token_address": token_address,
                "have_raw": balance,
                "need_raw": amount_raw,
            },
        )

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
