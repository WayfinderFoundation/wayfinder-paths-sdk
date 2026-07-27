from __future__ import annotations

import base64
from typing import Any

from eth_utils import to_checksum_address
from solders.transaction import VersionedTransaction

from wayfinder_paths.core.clients.BRAPClient import BRAP_CLIENT
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.utils.etherscan import get_etherscan_transaction_link
from wayfinder_paths.core.utils.svm import (
    get_solana_explorer_link,
    is_solana_chain,
)
from wayfinder_paths.core.utils.svm_tokens import (
    build_solana_send_transaction,
    get_solana_token_balance,
)
from wayfinder_paths.core.utils.svm_transaction import send_svm_versioned_transaction
from wayfinder_paths.core.utils.token_resolver import TokenResolver
from wayfinder_paths.core.utils.tokens import (
    build_send_transaction,
    ensure_allowance,
    get_token_balance,
)
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.core.utils.units import from_erc20_raw
from wayfinder_paths.core.utils.wallets import (
    find_wallet_leg_for_chain,
    get_wallet_signing_callback_for_chain,
    is_solana_enabled,
)
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


async def _broadcast_svm(
    sign_callback,
    serialized_transaction: str,
    *,
    chain_id: int,
    wait_for_confirmation: bool,
) -> tuple[bool, dict[str, Any]]:
    try:
        tx = VersionedTransaction.from_bytes(base64.b64decode(serialized_transaction))
        signature = await send_svm_versioned_transaction(
            tx,
            sign_callback,
            chain_id=chain_id,
            wait_for_confirmation=wait_for_confirmation,
        )
        return True, {
            "txn_hash": signature,
            "chain_id": chain_id,
            "confirmation_waited": wait_for_confirmation,
            "explorer_url": get_solana_explorer_link(signature),
        }
    except Exception as e:
        return False, {"error": sanitize_for_json(str(e)), "chain_id": chain_id}


def _tx_status(sent_ok: bool, waited: bool) -> str:
    if not sent_ok:
        return "failed"
    return "confirmed" if waited else "submitted"


async def _token_balance(token_address: str, chain_id: int, wallet_address: str) -> int:
    # get_token_balance is EVM-only (checksums the address); Solana is base58.
    if is_solana_chain(chain_id):
        return await get_solana_token_balance(wallet_address, token_address, chain_id)
    return await get_token_balance(token_address, chain_id, wallet_address)


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

    Quote and confirm first. `amount` is a decimal human-unit string with a
    decimal point, never wei. Recipient defaults to the wallet's destination-chain
    leg. Waiting covers the source receipt and any bridge leg; disable it only for
    fire-and-forget submission.
    """
    if not wallet_label.strip():
        return err("invalid_request", "wallet_label is required")
    if slippage_bps < 0:
        return err("invalid_request", "slippage_bps must be >= 0")

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
    if (
        is_solana_chain(from_chain_id) or is_solana_chain(to_chain_id)
    ) and not await is_solana_enabled():
        return err("solana_disabled", "Solana is not enabled for this account.")

    # Sign the source tx with the source-chain leg; default the recipient to the
    # ring's destination-chain leg (cross-chain lands on the paired leg).
    sign_callback, sender = await get_wallet_signing_callback_for_chain(
        wallet_label, from_chain_id
    )
    to_leg = await find_wallet_leg_for_chain(wallet_label, to_chain_id)
    dest_default = normalize_address(to_leg.get("address")) if to_leg else sender
    rcpt = normalize_address(recipient) or dest_default
    response: dict[str, Any] = {
        "sender": sender,
        "recipient": rcpt,
        "effects": {},
    }

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

    balance = await _token_balance(from_token_addr, int(from_chain_id), sender)
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
            to_wallet=rcpt,
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

    compact_quote = _compact_quote(quote_data, best_quote)

    # Solana-source routes carry a pre-built, unsigned v0 tx envelope instead of
    # EVM calldata — broadcast via SVM and skip checksum/allowance. Cross-chain
    # routes still wait for the destination bridge leg just like the EVM path.
    if is_solana_chain(from_chain_id):
        serialized = calldata.get("serializedTransaction")
        if not serialized:
            return err(
                "quote_error",
                "solana best_quote missing serializedTransaction",
                {"best_quote": best_quote},
            )
        sent_ok, sent = await _broadcast_svm(
            sign_callback,
            serialized,
            chain_id=int(from_chain_id),
            wait_for_confirmation=wait_for_receipt,
        )
        response["effects"]["swap"] = sent
        status = _tx_status(sent_ok, wait_for_receipt)

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
        response["raw"] = compact_quote
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

    swap_tx = dict(calldata)
    swap_tx["chainId"] = int(from_chain_id)
    swap_tx["from"] = to_checksum_address(sender)
    if "value" in swap_tx:
        swap_tx["value"] = int(swap_tx["value"])

    spender = (
        best_quote.get("approvalAddress")
        or best_quote.get("approval_address")
        or swap_tx.get("to")
    )
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

    status = _tx_status(sent_ok, wait_for_receipt)

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
    response["raw"] = compact_quote

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

    `amount` is a decimal human-unit string with a decimal point, never wei.
    `token="native"` requires `chain_id`; token ids otherwise determine the
    chain. Waiting is the default—disable it only for fire-and-forget submission.
    """
    if not wallet_label.strip():
        return err("invalid_request", "wallet_label is required")
    token_q = token.strip()
    if not token_q:
        return err("invalid_request", "token is required")
    if token_q.lower() == "native" and chain_id is None:
        return err("invalid_request", "chain_id is required when token='native'")

    rcpt = normalize_address(recipient)
    if not rcpt:
        return err("invalid_request", "recipient address is required")

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

    if is_solana_chain(int(resolved_chain_id)) and not await is_solana_enabled():
        return err("solana_disabled", "Solana is not enabled for this account.")

    # Sign with the leg for the resolved chain (SVM sends sign with the SVM leg).
    sign_callback, sender = await get_wallet_signing_callback_for_chain(
        wallet_label, int(resolved_chain_id)
    )
    response: dict[str, Any] = {
        "sender": sender,
        "recipient": rcpt,
        "effects": {},
    }

    try:
        amount_raw = parse_amount_to_raw(amount, decimals)
    except ValueError as exc:
        return err("invalid_amount", str(exc))

    balance = await _token_balance(token_address, int(resolved_chain_id), sender)
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

    label = "send_native" if is_native else "send_erc20"

    if is_solana_chain(int(resolved_chain_id)):
        # Solana transfer envelope: base58 recipient/mint (never checksummed).
        envelope = await build_solana_send_transaction(
            from_address=sender,
            to_address=rcpt,
            token_address=token_address,
            amount=int(amount_raw),
            chain_id=int(resolved_chain_id),
        )
        sent_ok, sent = await _broadcast_svm(
            sign_callback,
            envelope["serializedTransaction"],
            chain_id=int(resolved_chain_id),
            wait_for_confirmation=wait_for_receipt,
        )
        response["effects"][label] = sent
        status = _tx_status(sent_ok, wait_for_receipt)
        response["status"] = status
        response["raw"] = {"transaction": envelope, "token": token_meta}
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
    response["effects"][label] = sent

    status = _tx_status(sent_ok, wait_for_receipt)
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
